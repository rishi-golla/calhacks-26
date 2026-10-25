"""Agent recall: similar resolved cases from the shared Elastic Cloud `agent-memory` index.

SpacetimeDB stays the system of record for agent memory and precedents (versioned, revertible by a human, P-012).
This index is a mirror for similarity search: `sync()` copies active `agent_memory` and `precedent` rows into it and
deletes anything a human deactivated, so a revert also removes it from recall. A looping sync sends only what changed.
Elastic only ranks; `similar_cases` reads the ranked rows back from SpacetimeDB, so recall never shows a stale copy.
Recall is advisory: guarded workflows still decide every outcome.

Configuration (never in the repo):
  ELASTIC_CLOUD_URL                     the Elastic project endpoint
  ELASTIC_AGENT_MEMORY_READER_KEY       read-only API key for agents (index `agent-memory` only)
  ELASTIC_AGENT_MEMORY_WRITER_KEY       write key for the sync job
Unset -> recall returns nothing (fixture mode and tests are unaffected).

    python -m common.agent_recall --sync              # one pass
    python -m common.agent_recall --sync --loop 60    # keep mirroring every 60 s
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime

import httpx

log = logging.getLogger(__name__)
INDEX = os.getenv("AGENT_MEMORY_INDEX", "agent-memory")


def _endpoint() -> str:
    return os.getenv("ELASTIC_CLOUD_URL", "").rstrip("/")


def _headers(key_var: str) -> dict[str, str] | None:
    key = os.getenv(key_var, "")
    return {"Authorization": f"ApiKey {key}"} if key and _endpoint() else None


def enabled() -> bool:
    return _headers("ELASTIC_AGENT_MEMORY_READER_KEY") is not None


FIELDS = ("id", "kind", "agent", "carrier_id", "claim_type", "lane", "outcome", "source_ref", "case_id", "text",
          "version", "at")


async def similar_cases(*, carrier_id: str = "", claim_type: str = "", lane: str = "", skill: str = "",
                        text: str = "", limit: int = 5, actor: str = "validator", db=None) -> list[dict]:
    """Closest active lessons and precedents. Same carrier and claim type rank highest; text breaks ties.

    Elastic only ranks: it returns ids. The rows are then read from SpacetimeDB, so a lesson reverted since the last
    sync is dropped and text, outcome and version are always current. If SpacetimeDB can't be read, recall returns
    nothing rather than the mirror's possibly stale copy.
    """
    headers = _headers("ELASTIC_AGENT_MEMORY_READER_KEY")
    if headers is None:
        return []
    limit = max(1, min(limit, 10))
    should = []
    for field, value, boost in (("carrier_id", carrier_id, 4), ("claim_type", claim_type, 3), ("lane", lane, 1),
                                ("skill", skill, 1)):
        if value:
            should.append({"term": {field: {"value": value, "boost": boost}}})
    if text.strip():
        should.append({"match": {"text": {"query": text[:2000]}}})
    if not should:
        return []
    # Fetch twice the limit so rows dropped by the SpacetimeDB check don't shrink the answer.
    query = {"size": limit * 2, "_source": False,
             "query": {"bool": {"filter": [{"term": {"active": True}}], "should": should,
                                "minimum_should_match": 1}}}
    try:
        async with httpx.AsyncClient(timeout=3, headers=headers) as client:
            r = await client.post(f"{_endpoint()}/{INDEX}/_search", json=query)
            r.raise_for_status()
            ranked = [(h["_id"], round(h["_score"], 2)) for h in r.json()["hits"]["hits"]]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("agent recall unavailable: %s", type(e).__name__)
        return []
    current = await current_docs([doc_id for doc_id, _ in ranked], actor=actor, db=db)
    if current is None:
        return []
    cases = []
    for doc_id, score in ranked:
        doc = current.get(doc_id)
        if doc and doc["active"]:
            cases.append({**{k: doc[k] for k in FIELDS}, "score": score})
    return cases[:limit]


async def current_docs(doc_ids: list[str], *, actor: str = "validator", db=None) -> dict[str, dict] | None:
    """The SpacetimeDB rows behind index ids, as index docs; None when SpacetimeDB can't be read."""
    from common.spacetime_client import SpacetimeClient, SpacetimeError, _sql_string

    tables = {"memory": ("agent_memory", memory_doc), "precedent": ("precedent", precedent_doc)}
    wanted: dict[str, list[str]] = {}
    for doc_id in doc_ids:
        kind, _, row_id = doc_id.partition(":")
        if kind in tables and row_id:
            wanted.setdefault(kind, []).append(row_id)
    if not wanted:
        return {}
    db = db or SpacetimeClient(actor=actor)
    docs = {}
    try:
        for kind, row_ids in wanted.items():
            table, to_doc = tables[kind]
            rows = await db.query_rows(f"SELECT * FROM {table} WHERE "
                                       + " OR ".join(f"id = {_sql_string(i)}" for i in row_ids))
            for row in rows:
                if row.get("id") in row_ids:
                    doc = to_doc(row)
                    docs[doc["id"]] = doc
    except (SpacetimeError, httpx.HTTPError, KeyError) as e:
        log.warning("agent recall: SpacetimeDB unavailable (%s); returning nothing", type(e).__name__)
        return None
    return docs


# ---- sync: SpacetimeDB (system of record) -> Elastic (recall mirror) ----

def _loads(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def memory_doc(row: dict) -> dict:
    scope = _loads(row.get("scope"))
    return {"id": f"memory:{row['id']}", "kind": "memory", "agent": row.get("agent", ""),
            "carrier_id": scope.get("carrier_id") or "", "claim_type": scope.get("claim_type") or "",
            "lane": scope.get("lane") or "", "skill": scope.get("skill") or "", "outcome": "",
            "source_ref": row.get("source_ref", ""), "case_id": "", "text": row.get("text", ""),
            "active": bool(row.get("active")), "version": row.get("version") or 0, "at": row.get("created_at") or None}


def precedent_doc(row: dict) -> dict:
    pattern = _loads(row.get("pattern_json"))
    text = (f"{row.get('claim_type', '')} claim by {row.get('carrier_id', '')} on {row.get('lane', '')}: outcome "
            f"{row.get('outcome', '')}. Pattern: " + json.dumps({k: v for k, v in pattern.items()
                                                                 if k != "source_evidence"}))
    return {"id": f"precedent:{row['id']}", "kind": "precedent", "agent": "controller",
            "carrier_id": row.get("carrier_id", ""), "claim_type": row.get("claim_type", ""),
            "lane": row.get("lane", ""), "skill": "", "outcome": row.get("outcome", ""),
            "source_ref": row.get("source_case", ""), "case_id": row.get("source_case", ""), "text": text,
            "active": True, "version": 1, "at": row.get("as_of") or None}


BULK_CHUNK = 500
DELETED = "deleted"


def _fingerprint(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


async def sync(sent: dict[str, str] | None = None, *, db=None) -> dict:
    """One mirror pass. Inactive (reverted) memory is deleted from the index, never kept as stale recall.

    `sent` maps index ids to the fingerprint last written successfully. With it, a pass sends only new, changed and
    removed docs; a failed item is left out of `sent`, so the next pass retries it. Without it, every doc is sent.
    """
    headers = _headers("ELASTIC_AGENT_MEMORY_WRITER_KEY")
    if headers is None:
        raise SystemExit("set ELASTIC_CLOUD_URL and ELASTIC_AGENT_MEMORY_WRITER_KEY")
    from common.spacetime_client import SpacetimeClient

    sent = {} if sent is None else sent
    db = db or SpacetimeClient(actor="validator")   # reads only; both tables are public
    memories = await db.query_rows("SELECT * FROM agent_memory")
    precedents = await db.query_rows("SELECT * FROM precedent")
    docs = {d["id"]: d for d in [memory_doc(m) for m in memories] + [precedent_doc(p) for p in precedents]}
    ops = []   # (doc id, what `sent` records on success: a fingerprint, DELETED, or None to forget the id)
    for doc_id, d in docs.items():
        if d["active"]:
            if sent.get(doc_id) != (fp := _fingerprint(d)):
                ops.append((doc_id, fp, [{"index": {"_index": INDEX, "_id": doc_id}}, d]))
        elif sent.get(doc_id) != DELETED:
            ops.append((doc_id, DELETED, [{"delete": {"_index": INDEX, "_id": doc_id}}]))
    # Rows that vanished from SpacetimeDB leave the index too.
    ops += [(doc_id, None, [{"delete": {"_index": INDEX, "_id": doc_id}}])
            for doc_id, fp in sent.items() if doc_id not in docs and fp != DELETED]
    indexed = removed = errors = 0
    async with httpx.AsyncClient(timeout=60, headers=headers) as es:
        for start in range(0, len(ops), BULK_CHUNK):
            chunk = ops[start:start + BULK_CHUNK]
            body = "".join(json.dumps(line, default=str) + "\n" for _, _, lines in chunk for line in lines)
            r = await es.post(f"{_endpoint()}/_bulk", content=body, headers={"content-type": "application/x-ndjson"})
            r.raise_for_status()
            for (doc_id, outcome, lines), item in zip(chunk, r.json().get("items", []), strict=True):
                status = next(iter(item.values())).get("status", 200)
                deleting = "delete" in lines[0]
                if status < 300 or (deleting and status == 404):   # a delete of a missing doc is already done
                    if outcome is None:
                        sent.pop(doc_id, None)
                    else:
                        sent[doc_id] = outcome
                    removed += deleting
                    indexed += not deleting
                else:
                    errors += 1
    return {"memories": len(memories), "precedents": len(precedents), "indexed": indexed, "removed": removed,
            "unchanged": sum(1 for doc_id in docs if doc_id not in {op[0] for op in ops}),
            "errors": errors, "at": datetime.now(UTC).isoformat()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="repeat every N seconds")
    args = ap.parse_args()
    if not args.sync:
        ap.print_help()
        return
    sent: dict[str, str] = {}   # first pass sends everything; later passes send only changes
    while True:
        print(json.dumps(asyncio.run(sync(sent))), flush=True)
        if not args.loop:
            break
        asyncio.run(asyncio.sleep(args.loop))


if __name__ == "__main__":
    main()
