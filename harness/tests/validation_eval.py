"""Score input validation + intake memory against the simulator's answer key, replaying every recorded event in
stream order (no models, no dispatch, fresh memory).

    python -m harness.tests.validation_eval [--data ~/w4data/data]
"""
import argparse
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import redis

from harness.ingest.memory import Memory, event_time
from harness.ingest.validate import validate

DUP_SCENARIOS = {"duplicate_invoice", "duplicate_renumbered"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.getenv("W4_DATA_DIR", "~/w4data/data"))
    args = ap.parse_args()
    key = {}
    for line in (Path(args.data).expanduser() / "answer_key.jsonl").read_text().splitlines():
        a = json.loads(line)
        key[a["case_id"]] = a["scenario"]

    def scenario(case_id: str) -> str:
        for c in (case_id, case_id.removesuffix("-R")):
            if c in key:
                return key[c]
        return "unknown"

    from harness.ingest.memory import Directory
    mem = Memory(Path(tempfile.mkdtemp()) / "m.sqlite", Directory(str(Path(args.data).expanduser() / "source.json")))
    r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    issues_by_type: dict[str, Counter] = defaultdict(Counter)
    fatal = Counter()
    dup_hits: Counter = Counter()
    dup_events: dict[str, int] = Counter()
    identity: dict[str, Counter] = defaultdict(Counter)
    examples: dict[str, str] = {}
    n = Counter()
    for _id, fields in r.xrange("events"):
        ev = json.loads(fields[b"json"])
        etype, eid = ev.get("type", ""), ev.get("event_id", "")
        if eid.startswith(("EVT-DEMO", "EVT-W1-", "EVT-0951", "EVT-allopen", "EVT-pod-", "EVT-acct")):
            continue  # our own test events
        n[etype] += 1
        payload, issues, why = validate(etype, ev.get("payload") or {})
        if why:
            fatal[why] += 1
            continue
        for i in issues:
            issues_by_type[etype][i.split(" (")[0].split(" '")[0]] += 1
            examples.setdefault(i.split(" (")[0].split(" '")[0], i)
        case = payload.get("case_id") or ""
        sc = scenario(case)
        if etype == "carrier_invoice.received":
            if sc in DUP_SCENARIOS:
                dup_events[sc] += 1
            at = event_time(ev, payload)
            dup = mem.find_duplicate(eid, payload, at)
            if dup:
                dup_hits[(sc, dup["rule"])] += 1
            mem.remember_invoice(eid, case, payload, at)
        for note in mem.identity_signals(etype, payload):
            identity[sc][note.split(" for ")[0]] += 1
            examples.setdefault(note.split(" for ")[0], note)
        mem.remember_identity(etype, eid, payload, event_time(ev, payload))

    print(f"replayed: {dict(n)}")
    print(f"rejected as unusable: {dict(fatal) or 0}")
    print("\nvalidation flags by event type:")
    for t, c in issues_by_type.items():
        print(f"  {t}: {dict(c.most_common(6))}")
    print("\nduplicate invoices flagged (scenario, rule): ", dict(dup_hits))
    caught = sum(v for (sc, _r), v in dup_hits.items() if sc in DUP_SCENARIOS)
    false_dup = sum(v for (sc, _r), v in dup_hits.items() if sc not in DUP_SCENARIOS)
    print(f"  invoices in duplicate scenarios: {dict(dup_events)} -> second copies caught: {caught}; "
          f"flagged outside duplicate scenarios: {false_dup}")
    print("\nidentity signals by scenario:")
    for sc, c in sorted(identity.items()):
        print(f"  {sc}: {dict(c)}")
    print("\nexamples:")
    for k, v in list(examples.items())[:10]:
        print(f"  {v[:150]}")


if __name__ == "__main__":
    main()
