"""Should intake validation use history? Scores history-based invoice checks against the answer key, replaying the
recorded `events` in time order. Re-run on real data before shipping any of them.

    python -m harness.tests.history_eval

Result on the recorded sim run (2026-10-24), so none are shipped:
- amount vs the carrier's usual amount (1.5x/2x/3x): 4-11% of clean invoices flagged, no better on bad ones;
  invoice size follows the lane (distance), not the carrier
- velocity (>=3 or >=5 invoices in 24 h): never fires; the sim plants no spikes
- invoice/contract vs the carrier's usual ratio (+0.05): 0 clean flagged, ~60% of anomalies caught
- plain P-001 (invoice > contract + 2%, no history): 0 clean flagged, 100% of anomalies caught. Carrier AP already
  enforces it, so intake does not duplicate it
In the sim, clean invoices equal their contract exactly; real carriers often bill agreed extras, which is where a
per-carrier baseline could start to matter.
"""
import json
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import redis

from harness.ingest.memory import carrier_of, event_time

OWN = ("EVT-DEMO", "EVT-W1", "EVT-0951")


def main() -> None:
    data = Path(os.getenv("W4_DATA_DIR", "~/w4data/data")).expanduser()
    key = {json.loads(line)["case_id"]: json.loads(line)["scenario"]
           for line in (data / "answer_key.jsonl").read_text().splitlines()}

    def scenario(case_id: str) -> str:
        return key.get(case_id) or key.get((case_id or "").removesuffix("-R")) or "unknown"

    r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    events = sorted((json.loads(f[b"json"]) for _, f in r.xrange("events")),
                    key=lambda e: event_time(e, e.get("payload") or {}))
    rows, amounts, ratios = [], defaultdict(list), defaultdict(list)
    for e in events:
        p = e.get("payload") or {}
        if e.get("type") != "carrier_invoice.received" or e.get("event_id", "").startswith(OWN):
            continue
        inv, con, c = p.get("invoice_amount"), p.get("contract_rate"), carrier_of(p)
        if not isinstance(inv, (int, float)):
            continue
        ratio = inv / con if isinstance(con, (int, float)) and con > 0 else None
        rows.append((scenario(p.get("case_id")), inv, ratio, list(amounts[c]), list(ratios[c])))
        amounts[c].append(inv)
        if ratio is not None:
            ratios[c].append(ratio)

    def report(name, check) -> None:
        hit, total = Counter(), Counter()
        for s, inv, ratio, prior_amt, prior_ratio in rows:
            total[s] += 1
            hit[s] += bool(check(inv, ratio, prior_amt, prior_ratio))
        caught = ", ".join(f"{k} {hit[k]}/{total[k]}" for k in sorted(total) if hit[k] and k != "clean")
        print(f"{name:48s} clean {hit['clean']}/{total['clean']} | {caught or 'nothing'}")

    for k in (1.5, 2.0, 3.0):
        report(f"amount > {k}x carrier median (>=3 prior)",
               lambda i, r_, a, _p, k=k: len(a) >= 3 and i > k * statistics.median(a))
    report("invoice/contract > carrier usual + 0.05 (>=2 prior)",
           lambda _i, r_, _a, p: r_ is not None and len(p) >= 2 and r_ > statistics.median(p) + 0.05)
    report("P-001: invoice > contract + 2% (no history)", lambda _i, r_, _a, _p: r_ is not None and r_ > 1.02)


if __name__ == "__main__":
    main()
