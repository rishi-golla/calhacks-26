"""Agent recall: similar resolved cases from the shared Elastic Cloud `agent-memory` index.

SpacetimeDB stays the system of record for agent memory and precedents (versioned, revertible by a human, P-012).
This index is a mirror for similarity search: `sync()` copies active `agent_memory` and `precedent` rows into it and
deletes anything a human deactivated, so a revert also removes it from recall. Recall is advisory: guarded workflows
still decide every outcome.

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


async def similar_cases(*, carrier_id: str = "", claim_type: str = "", lane: str = "", skill: str = "",
                        text: str = "", limit: int = 5) -> list[dict]:
    """Closest active lessons and precedents. Same carrier and claim type rank highest; text breaks ties."""
    headers = _headers("ELASTIC_AGENT_MEMORY_READER_KEY")
    if headers is None:
        return []
    should = []
    for field, value, boost in (("carrier_id", carrier_id, 4), ("claim_type", claim_type, 3), ("lane", lane, 1),
                                ("skill", skill, 1)):
        if value:
            should.append({"term": {field: {"value": value, "boost": boost}}})
    if text.strip():
        should.append({"match": {"text": {"query": text[:2000]}}})
    if not should:
        return []
    query = {"size": max(1, min(limit, 10)),
             "_source": ["id", "kind", "agent", "carrier_id", "claim_type", "lane", "outcome", "source_ref",
                         "case_id", "text", "version", "at"],
             "query": {"bool": {"filter": [{"term": {"active": True}}], "should": should,
                                "minimum_should_match": 1}}}
    try:
        async with httpx.AsyncClient(timeout=3, headers=headers) as client:
            r = await client.post(f"{_endpoint()}/{INDEX}/_search", json=query)
            r.raise_for_status()
            hits = r.json()["hits"]["hits"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("agent recall unavailable: %s", type(e).__name__)
        return []
    return [{**h["_source"], "score": round(h["_score"], 2)} for h in hits]


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


async def sync() -> dict:
    """One mirror pass. Inactive (reverted) memory is deleted from the index, never kept as stale recall."""
    headers = _headers("ELASTIC_AGENT_MEMORY_WRITER_KEY")
    if headers is None:
        raise SystemExit("set ELASTIC_CLOUD_URL and ELASTIC_AGENT_MEMORY_WRITER_KEY")
    from common.spacetime_client import SpacetimeClient

    db = SpacetimeClient(actor="validator")   # reads only; both tables are public
    memories = await db.query_rows("SELECT * FROM agent_memory")
    precedents = await db.query_rows("SELECT * FROM precedent")
    docs = [memory_doc(m) for m in memories] + [precedent_doc(p) for p in precedents]
    upserts = [d for d in docs if d["active"]]
    deletes = [d["id"] for d in docs if not d["active"]]
    lines = []
    for d in upserts:
        lines += [json.dumps({"index": {"_index": INDEX, "_id": d["id"]}}), json.dumps(d, default=str)]
    for doc_id in deletes:
        lines.append(json.dumps({"delete": {"_index": INDEX, "_id": doc_id}}))
    errors = 0
    if lines:
        async with httpx.AsyncClient(timeout=60, headers=headers) as es:
            r = await es.post(f"{_endpoint()}/_bulk", content="\n".join(lines) + "\n",
                              headers={"content-type": "application/x-ndjson"})
            r.raise_for_status()
            errors = sum(1 for item in r.json().get("items", [])
                         if next(iter(item.values())).get("status", 200) >= 300
                         and next(iter(item.values())).get("status") != 404)
    return {"memories": len(memories), "precedents": len(precedents), "indexed": len(upserts),
            "removed": len(deletes), "errors": errors, "at": datetime.now(UTC).isoformat()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="repeat every N seconds")
    args = ap.parse_args()
    if not args.sync:
        ap.print_help()
        return
    while True:
        print(json.dumps(asyncio.run(sync())), flush=True)
        if not args.loop:
            break
        asyncio.run(asyncio.sleep(args.loop))


if __name__ == "__main__":
    main()
