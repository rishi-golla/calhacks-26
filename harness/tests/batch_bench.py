"""Batch benchmark: clean a fixed batch of routable events and score it against the sim's ground truth.

    python -m harness.tests.batch_bench --export 300        # snapshot a fixed batch from Redis `events`
    python -m harness.tests.batch_bench                     # run it (dry run: nothing is sent to agents)
    python -m harness.tests.batch_bench --dispatch          # also send each envelope over A2A

Ground truth comes from the W4 data dir (default ~/w4data/data): docs/<ref>.json holds the true fields and the
contact details planted in each PDF, mail/<id>.eml the sender addresses, answer_key.jsonl the scenario per case.
The batch and reports live in harness/data/bench/ (gitignored: the sim uses real FMCSA contact data)."""
import argparse
import asyncio
import email
import email.utils
import json
import os
import re
import statistics
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import redis

from common.config import SKILL_OWNER
from harness.ingest.consumer import MODEL_CONCURRENCY, needs_model
from harness.ingest.dispatch import Dispatcher
from harness.ingest.pipeline import BUSINESS, process
from harness.ingest.routing import ROUTES
from harness.ingest.vault import DATA_DIR, Vault
from harness.pool import Pool

BENCH_DIR = DATA_DIR / "bench"
BATCH = BENCH_DIR / "batch.jsonl"
PII_KEYS = {"address", "phone", "email", "signed_by", "receiver", "driver", "loader", "contact", "requested_by"}
ROUTABLE = {*ROUTES, "email.received", "scenario.injected"}


# ---------------------------------------------------------------- batch export
def export(n: int, url: str) -> None:
    r = redis.Redis.from_url(url)
    by_type: dict[str, list[dict]] = defaultdict(list)
    for _id, f in r.xrange("events"):
        ev = json.loads(f[b"json"])
        if ev.get("type") in ROUTABLE:
            by_type[ev["type"]].append(ev)
    rare = [e for t, es in by_type.items() if len(es) < 50 for e in es]   # bank changes, emails, bank events
    common = {t: es for t, es in by_type.items() if len(es) >= 50}
    picked = rare[:n]
    left = n - len(picked)
    for i, (_t, es) in enumerate(sorted(common.items())):
        share = left // len(common) + (1 if i < left % len(common) else 0)
        step = max(1, len(es) // max(share, 1))
        picked += es[::step][:share]
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    BATCH.write_text("".join(json.dumps(e) + "\n" for e in picked))
    print(f"wrote {len(picked)} events to {BATCH}: {dict(Counter(e['type'] for e in picked))}")


# ---------------------------------------------------------------- ground truth
def _walk(obj, key=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, key)
    else:
        yield key, obj


class Truth:
    def __init__(self, data: Path) -> None:
        self.data = data
        self.cases: dict[str, dict] = {}
        ak = data / "answer_key.jsonl"
        if ak.exists():
            for line in ak.read_text().splitlines():
                a = json.loads(line)
                self.cases[a["case_id"]] = a

    def doc(self, ref: str) -> dict | None:
        p = self.data / "docs" / f"{ref}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def planted(self, ev: dict) -> list[tuple[str, str]]:
        """(kind, value) pairs of personal data planted in this event's documents and mail."""
        out: set[tuple[str, str]] = set()
        payload = ev.get("payload") or {}
        for ref in payload.get("evidence_refs") or ev.get("evidence_refs") or []:
            kind, _, rid = ref.partition(":")
            if kind == "doc" and (d := self.doc(rid)):
                for k, v in _walk(d):
                    if k in PII_KEYS and isinstance(v, str) and len(v) >= 5 and "*" not in v:
                        if k in ("signed_by", "receiver", "driver", "loader", "contact", "requested_by") \
                                and BUSINESS.search(v):
                            continue  # company names are kept on purpose
                        out.add((k, v))
            elif kind == "mail" and (p := self.data / "mail" / f"{rid}.eml").exists():
                msg = email.message_from_bytes(p.read_bytes())
                for _name, addr in email.utils.getaddresses(msg.get_all("From", []) + msg.get_all("Reply-To", [])):
                    if "@" in addr:
                        out.add(("email", addr))
        return sorted(out)


def _variants(kind: str, value: str) -> list[str]:
    if kind == "phone":
        d = re.sub(r"\D", "", value)[-10:]
        if len(d) < 10:
            return [value.lower()]
        return [f"+1{d}", d, f"({d[:3]}) {d[3:6]}-{d[6:]}", f"{d[:3]}-{d[3:6]}-{d[6:]}", f"{d[:3]}.{d[3:6]}.{d[6:]}"]
    if kind == "address":
        return [value.split(",")[0].strip().lower()]   # the street line is the personal part
    return [value.lower()]


def leaks(planted: list[tuple[str, str]], outgoing: str) -> list[tuple[str, str]]:
    low = outgoing.lower()
    return [(k, v) for k, v in planted
            if any(re.search(rf"(?<![\w@]){re.escape(x)}(?![\w])", low) for x in _variants(k, v))]


# ---------------------------------------------------------------- run
async def run(args) -> dict:
    events = [json.loads(line) for line in BATCH.read_text().splitlines()]
    truth = Truth(Path(args.data).expanduser())
    pool = Pool()
    await pool.check_health()
    health = asyncio.create_task(pool.health_loop())
    tmp = Path(tempfile.mkdtemp(prefix="bench-"))
    vault, dispatcher = Vault(tmp / "vault.sqlite"), Dispatcher(tmp / "ingest.sqlite")
    slots = asyncio.Semaphore(args.concurrency)

    async def one(i: int, ev: dict) -> dict:
        ev = {**ev, "event_id": f"{ev['event_id']}#bench{args.tag}"}   # fresh id: no dedup against live runs
        t0 = time.perf_counter()
        if needs_model(ev):
            async with slots:
                t1 = time.perf_counter()
                res = await process(pool, vault, dispatcher, ev, dispatch=args.dispatch)
        else:
            t1 = t0
            res = await process(pool, vault, dispatcher, ev, dispatch=args.dispatch)
        res["_wait_ms"] = int((t1 - t0) * 1000)
        res["_e2e_ms"] = int((time.perf_counter() - t0) * 1000)
        res["_source"] = ev
        if args.progress and i % 25 == 0:
            print(f"  {i}/{len(events)}", flush=True)
        return res

    t = time.perf_counter()
    results = await asyncio.gather(*(one(i, ev) for i, ev in enumerate(events)))
    wall = time.perf_counter() - t
    health.cancel()
    await pool.aclose()
    return score(results, truth, wall, args)


def _pct(xs: list[int], p: float) -> int:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))] if xs else 0


def score(results: list[dict], truth: Truth, wall: float, args) -> dict:
    status = Counter(r["status"] for r in results)
    per_type = Counter(r["type"] for r in results)
    service = [r.get("timings_ms", {}).get("total", 0) for r in results]
    e2e = [r["_e2e_ms"] for r in results]
    stages = {s: int(statistics.median([r["timings_ms"][s] for r in results if s in r.get("timings_ms", {})]))
              for s in ("redact", "extract", "classify", "verify", "dispatch")
              if any(s in r.get("timings_ms", {}) for r in results)}

    planted_n, leaked = 0, []
    for r in results:
        p = truth.planted(r["_source"])
        planted_n += len(p)
        if r.get("envelope"):
            leaked += [(r["event_id"], k, v) for k, v in leaks(p, json.dumps(r["envelope"]))]

    ex = {"invoices_scored": 0, "total_ok": 0, "lines_ok": 0, "payload_total_ok": 0}
    for r in results:
        if r["type"] != "carrier_invoice.received":
            continue
        refs = [x.partition(":")[2] for x in (r["_source"].get("payload") or {}).get("evidence_refs", [])
                if x.startswith("doc:INV")]
        d = truth.doc(refs[0]) if refs else None
        if not d:
            continue
        e = r.get("extracted") or {}
        ex["invoices_scored"] += 1
        tot = d.get("total")
        if isinstance(e.get("invoice_amount"), (int, float)) and abs(e["invoice_amount"] - tot) < 0.01:
            ex["total_ok"] += 1
        pa = (r["_source"].get("payload") or {}).get("invoice_amount")
        if isinstance(pa, (int, float)) and abs(pa - tot) < 0.01:
            ex["payload_total_ok"] += 1
        want = sorted(round(float(x["amount"]), 2) for x in d.get("line_items", []))
        got = sorted(round(float(x["amount"]), 2) for x in e.get("line_items") or []
                     if isinstance(x, dict) and isinstance(x.get("amount"), (int, float)))
        if want and want == got:
            ex["lines_ok"] += 1

    routed = [r for r in results if r.get("route", {}).get("skill")]
    by_scenario: dict[str, list[bool]] = defaultdict(list)
    owners_by_case: dict[str, set[str]] = defaultdict(set)
    for r in results:
        case = truth.cases.get(r["case_id"])
        if not case:
            continue
        by_scenario[case["scenario"]].append(bool(r.get("needs_review")))
        if skill := r.get("route", {}).get("skill"):
            owners_by_case[r["case_id"]].add(SKILL_OWNER[skill])
    # A case's planted problem can ride on any of its events (e.g. the bank-change email, not the invoice),
    # so check that the case as a whole reached an agent the answer key expects.
    owner_ok = owner_n = 0
    for cid, owners in owners_by_case.items():
        want = set(truth.cases[cid].get("expected", {})) & set(SKILL_OWNER.values())
        if want:
            owner_n += 1
            owner_ok += bool(want & owners)
    notes = Counter(re.sub(r"[\d.]+", "#", n.split(":")[0] + ":" + n.split(":")[1][:40] if ":" in n else n)
                    for r in results for n in (r.get("envelope") or {}).get("subject", {}).get("review_notes", []))
    models = Counter(f"lane={r.get('lane')} extract={r.get('extractor')} "
                     f"verify={(r.get('verifier') or {}).get('model_used')}" for r in routed)
    emails = Counter(r.get("route", {}).get("skill") or "triage" for r in results if r["type"] == "email.received")

    return {
        "n": len(results), "wall_s": round(wall, 1), "docs_per_min": round(len(results) / wall * 60, 1),
        "concurrency": args.concurrency, "dispatch": args.dispatch, "mode": os.getenv("INGEST_MODE", "fast"),
        "types": dict(per_type),
        "status": dict(status),
        "latency_ms": {"service_p50": _pct(service, .5), "service_p95": _pct(service, .95),
                       "e2e_p50": _pct(e2e, .5), "e2e_p95": _pct(e2e, .95), "stage_p50": stages},
        "redaction": {"planted": planted_n, "leaked": len(leaked),
                      "recall": round(1 - len(leaked) / planted_n, 4) if planted_n else None,
                      "leaks_by_kind": dict(Counter(k for _e, k, _v in leaked)),
                      "examples": [(e, k, v[:4] + "…") for e, k, v in leaked[:10]]},
        "extraction": ex,
        "routing": {"owner_matches_answer_key": f"{owner_ok}/{owner_n}", "email_choices": dict(emails)},
        "needs_review": {"rate": round(sum(bool(r.get("needs_review")) for r in routed) / max(len(routed), 1), 3),
                         "by_scenario": {s: f"{sum(v)}/{len(v)}" for s, v in sorted(by_scenario.items())},
                         "top_notes": notes.most_common(8)},
        "models": dict(models),
    }


def print_report(rep: dict) -> None:
    x, lat, red = rep["extraction"], rep["latency_ms"], rep["redaction"]
    print(f"\n## Batch benchmark: {rep['n']} events, mode {rep['mode']}, concurrency {rep['concurrency']}, "
          f"{'dispatched over A2A' if rep['dispatch'] else 'dry run'}")
    print(f"- **Throughput:** {rep['wall_s']} s wall, **{rep['docs_per_min']} docs/min**")
    print(f"- **Per document:** service p50 {lat['service_p50']} ms / p95 {lat['service_p95']} ms; "
          f"end-to-end incl. queueing p50 {lat['e2e_p50']} ms / p95 {lat['e2e_p95']} ms")
    print(f"- **Stage p50 (ms):** {lat['stage_p50']}")
    print(f"- **Status:** {rep['status']}  types: {rep['types']}")
    print(f"- **Redaction recall:** {red['recall']} ({red['leaked']} leaked of {red['planted']} planted) "
          f"{red['leaks_by_kind'] or ''}")
    if x["invoices_scored"]:
        n = x["invoices_scored"]
        print(f"- **Invoice extraction vs document truth:** total {x['total_ok']}/{n}, line items "
              f"{x['lines_ok']}/{n} (payload total from data side {x['payload_total_ok']}/{n})")
    print(f"- **Routing:** cases reaching an expected agent {rep['routing']['owner_matches_answer_key']}; "
          f"emails -> {rep['routing']['email_choices']}")
    nr = rep["needs_review"]
    print(f"- **needs_review:** {nr['rate']:.0%} of routed. By scenario: {nr['by_scenario']}")
    print(f"- **Top review notes:** {nr['top_notes']}")
    print(f"- **Models:** {rep['models']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", type=int, metavar="N")
    ap.add_argument("--data", default=os.getenv("W4_DATA_DIR", "~/w4data/data"))
    ap.add_argument("--concurrency", type=int, default=MODEL_CONCURRENCY)
    ap.add_argument("--dispatch", action="store_true")
    ap.add_argument("--progress", action="store_true")
    ap.add_argument("--tag", default=str(int(time.time())))
    args = ap.parse_args()
    if args.export:
        export(args.export, os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        return
    rep = asyncio.run(run(args))
    out = BENCH_DIR / f"report-{args.tag}.json"
    out.write_text(json.dumps(rep, indent=1, default=str))
    print_report(rep)
    print(f"\nfull report: {out}")


if __name__ == "__main__":
    main()
