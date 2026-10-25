"""Intake context memory: a local Elasticsearch index on the ASUS box (127.0.0.1:9200, never exposed).

Every processed document is indexed scrubbed (tokens, not personal data) with its carrier, load, amounts, route,
outcome and flags. Before routing, the harness builds context for the new document: the carrier's last 90 days
(how many documents and invoices, typical amount, which flags and how often, most recent items) and the most
similar past documents. The context rides in the envelope as `subject.intake_context`, so the agent starts with
the history. A carrier with prior identity or duplicate flags gets a `warning` in that context. It is context for
the agent, not a review trigger: the document's own signals decide review (history alone would flag ~1 in 4
invoices on the recorded run, mostly carriers that legitimately factor).

Best effort by design: if Elasticsearch is unavailable, intake continues without context.

    python -m harness.ingest.context --backfill      # index the recorded Redis `events` history
"""
import argparse
import asyncio
import json
import logging
import os
import time

import httpx

log = logging.getLogger("harness.context")
ES_URL = os.getenv("MEMORY_ES_URL", "http://127.0.0.1:9200")
INDEX = os.getenv("MEMORY_ES_INDEX", "w1-memory")
HISTORY_DAYS = 90
FRAUD_TAGS = ("identity:", "duplicate")

MAPPING = {"mappings": {"properties": {
    "event_id": {"type": "keyword"}, "type": {"type": "keyword"}, "case_id": {"type": "keyword"},
    "carrier_id": {"type": "keyword"}, "load_id": {"type": "keyword"}, "invoice_number": {"type": "keyword"},
    "amount": {"type": "double"}, "at": {"type": "date", "format": "epoch_second"},
    "status": {"type": "keyword"}, "skill": {"type": "keyword"}, "agent": {"type": "keyword"},
    "tags": {"type": "keyword"}, "notes": {"type": "text"}, "text": {"type": "text", "analyzer": "english"},
    "source": {"type": "keyword"}}}}


def tag(note: str) -> str:
    """Short, countable category for a review note."""
    n = note.lower()
    if n.startswith("identity: "):
        return note.split(" for ")[0]
    if "duplicate" in n:
        return "duplicate"
    if n.startswith("input: "):
        parts = note.split()
        return "input: " + (parts[1] if len(parts) > 1 else "")
    if "line items sum" in n:
        return "math: line items"
    if n.startswith("amount") or ("payload" in n and "document" in n):
        return "math: amount mismatch"
    if n.startswith("verifier pii"):
        return "pii leftover"
    if "unavailable" in n:
        return "model unavailable"
    return note[:30]


def amount_of(payload: dict) -> float | None:
    for k in ("invoice_amount", "amount", "next_payment_amount", "amount_disputed"):
        if isinstance(payload.get(k), (int, float)) and not isinstance(payload.get(k), bool):
            return float(payload[k])
    return None


class ContextStore:
    def __init__(self, url: str = ES_URL, index: str = INDEX) -> None:
        """url="" disables the store (tests, benchmarks that must not touch the live index)."""
        self.url, self.index = url.rstrip("/"), index
        self._client = httpx.AsyncClient(timeout=3)
        self._ready = False
        self._warned = False

    async def _ensure(self) -> bool:
        if self._ready:
            return True
        if not self.url:
            return False
        try:
            r = await self._client.head(f"{self.url}/{self.index}")
            if r.status_code == 404:
                r = await self._client.put(f"{self.url}/{self.index}", json=MAPPING)
                if r.status_code >= 400 and "resource_already_exists" not in r.text:
                    raise httpx.HTTPError(r.text[:200])
            self._ready = True
        except httpx.HTTPError as e:
            if not self._warned:
                log.warning("context memory unavailable (%s); continuing without context", type(e).__name__)
                self._warned = True
            return False
        return True

    @staticmethod
    def record(event: dict, payload: dict, *, carrier_id: str, at: float, status: str, notes: list[str],
               text: str = "", skill: str = "", agent: str = "", source: str = "live") -> dict:
        return {"event_id": event.get("event_id"), "type": event.get("type"), "case_id": payload.get("case_id") or "",
                "carrier_id": carrier_id, "load_id": payload.get("load_id") or "",
                "invoice_number": payload.get("invoice_number") or "", "amount": amount_of(payload), "at": int(at),
                "status": status, "skill": skill or "", "agent": agent or "", "tags": sorted({tag(n) for n in notes}),
                "notes": " | ".join(notes)[:2000], "text": text[:8000], "source": source}

    async def remember(self, doc: dict) -> None:
        if not await self._ensure():
            return
        try:
            await self._client.put(f"{self.url}/{self.index}/_doc/{doc['event_id']}", json=doc)
        except httpx.HTTPError as e:
            log.warning("context index write failed: %s", type(e).__name__)

    async def build(self, event_id: str, carrier_id: str, text: str, at: float) -> dict | None:
        """Carrier history (last 90 days, before this document) and the most similar past documents."""
        if not await self._ensure():
            return None
        out: dict = {}
        try:
            if carrier_id:
                q = {"size": 5, "sort": [{"at": "desc"}],
                     "query": {"bool": {"filter": [
                         {"term": {"carrier_id": carrier_id}},
                         {"range": {"at": {"gte": int(at - HISTORY_DAYS * 86400), "lte": int(at),
                                           "format": "epoch_second"}}}],   # numbers default to millis
                         "must_not": [{"term": {"event_id": event_id}}]}},
                     "aggs": {"types": {"terms": {"field": "type"}}, "tags": {"terms": {"field": "tags", "size": 20}},
                              "invoices": {"filter": {"term": {"type": "carrier_invoice.received"}},
                                           "aggs": {"avg": {"avg": {"field": "amount"}}}}},
                     "_source": ["event_id", "type", "case_id", "amount", "status", "tags", "at"]}
                r = (await self._client.post(f"{self.url}/{self.index}/_search", json=q)).json()
                aggs = r.get("aggregations", {})
                inv = aggs.get("invoices", {})
                out["carrier"] = {
                    "carrier_id": carrier_id, "documents_90d": r["hits"]["total"]["value"],
                    "invoices_90d": inv.get("doc_count", 0),
                    "avg_invoice": round(inv["avg"]["value"], 2) if inv.get("avg", {}).get("value") else None,
                    "flags_90d": {b["key"]: b["doc_count"] for b in aggs.get("tags", {}).get("buckets", [])},
                    "recent": [{k: h["_source"].get(k) for k in ("event_id", "type", "case_id", "amount", "status",
                                                                  "tags")} for h in r["hits"]["hits"]]}
            if text.strip():
                q = {"size": 3, "_source": ["event_id", "type", "case_id", "carrier_id", "status", "tags"],
                     "query": {"bool": {"must": [{"more_like_this": {"fields": ["text"], "like": text[:4000],
                                                                    "min_term_freq": 1, "min_doc_freq": 1}}],
                                        "must_not": [{"term": {"event_id": event_id}}]}}}
                r = (await self._client.post(f"{self.url}/{self.index}/_search", json=q)).json()
                out["similar"] = [{**h["_source"], "score": round(h["_score"], 1)} for h in r.get("hits", {}).get("hits", [])]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("context build failed: %s", type(e).__name__)
            return out or None
        warning = self.signals(out)
        if warning:
            out["warning"] = warning[0]
        return out

    @staticmethod
    def signals(context: dict | None) -> list[str]:
        """Review notes derived from history: repeat fraud signals for this carrier."""
        flags = ((context or {}).get("carrier") or {}).get("flags_90d", {})
        hits = {k: v for k, v in flags.items() if k.startswith(FRAUD_TAGS)}
        if not hits:
            return []
        cid = context["carrier"]["carrier_id"]
        detail = ", ".join(f"{k} x{v}" for k, v in hits.items())
        return [f"history: {cid} had {sum(hits.values())} identity/duplicate flags in {HISTORY_DAYS} days ({detail})"]

    async def aclose(self) -> None:
        await self._client.aclose()


async def backfill(url: str = ES_URL, index: str = INDEX, data_dir: str | None = None) -> dict:
    """Index the recorded `events` history: rules-layer redaction, deterministic flags, no models."""
    import tempfile
    from pathlib import Path

    import redis

    from harness.ingest.memory import Directory, Memory, carrier_of, event_time
    from harness.ingest.pipeline import document_text
    from harness.ingest.redact import known_spans, redact_obj, replace_spans
    from harness.ingest.validate import validate
    from harness.ingest.vault import Vault

    store = ContextStore(url, index)
    if not await store._ensure():
        raise SystemExit(f"elasticsearch not reachable at {url}")
    tmp = Path(tempfile.mkdtemp())
    src = Path(data_dir or os.getenv("W4_DATA_DIR", "~/w4data/data")).expanduser() / "source.json"
    mem, vault = Memory(tmp / "m.sqlite", Directory(str(src))), Vault(tmp / "v.sqlite")
    r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    lines, n, t0 = [], 0, time.time()
    for _id, fields in r.xrange("events"):
        ev = json.loads(fields[b"json"])
        etype = ev.get("type", "")
        if etype == "gps.ping":
            continue
        payload, notes, fatal = validate(etype, ev.get("payload") or {})
        if fatal or not isinstance(payload, dict):
            continue
        at = event_time(ev, payload)
        if etype == "carrier_invoice.received":
            dup = mem.find_duplicate(ev.get("event_id", ""), payload, at)
            if dup:
                notes.append(f"duplicate of {dup['event_id']} ({dup['rule']})")
            mem.remember_invoice(ev.get("event_id", ""), payload.get("case_id") or "", payload, at)
        notes += mem.identity_signals(etype, payload)
        mem.remember_identity(etype, ev.get("event_id", ""), payload, at)
        clean, toks = redact_obj(payload, vault)
        clean = replace_spans(clean, known_spans(toks, vault))
        has_text = any(isinstance(clean.get(k), str) for k in ("text", "body"))
        doc = ContextStore.record(ev, payload, carrier_id=carrier_of(payload), at=at, status="historical",
                                  notes=notes, text=document_text(clean) if has_text else "", source="backfill")
        lines += [json.dumps({"index": {"_index": index, "_id": doc["event_id"]}}), json.dumps(doc)]
        n += 1
        if len(lines) >= 1000:
            await store._client.post(f"{url}/_bulk", content="\n".join(lines) + "\n",
                                     headers={"content-type": "application/x-ndjson"}, timeout=60)
            lines = []
    if lines:
        await store._client.post(f"{url}/_bulk", content="\n".join(lines) + "\n",
                                 headers={"content-type": "application/x-ndjson"}, timeout=60)
    await store._client.post(f"{url}/{index}/_refresh")
    await store.aclose()
    return {"indexed": n, "seconds": round(time.time() - t0, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--index", default=INDEX)
    args = ap.parse_args()
    if args.backfill:
        print(asyncio.run(backfill(index=args.index)))


if __name__ == "__main__":
    main()
