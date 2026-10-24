"""Formal checks for generated W4 evidence and its answer key."""
import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

ALLOWED = {
    "rate_mismatch": {"V3"}, "fsc_error": {"V3"}, "math_error": {"V2"},
    "pod_unsigned": {"V6"}, "factoring_unverified": {"V8"}, "double_brokering": {"V8"},
    "misapplied_payment": {"V9"},
}
SUPPORTED_DETENTION = {"detention_supported", "decoy_detention_supported_small"}
HERO = {"C-311": ("Sierra Haul LLC", "3141592"), "S-07": ("Bay Produce Co", None),
        "C-77": ("Rapid Ridge Transport", "7700770")}


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _date(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M")
        except ValueError:
            return None


def _text_part(part) -> str:
    content = part.get_payload(decode=True)
    if content is None:
        content = part.get_content()
    return content.decode(errors="ignore") if isinstance(content, bytes) else str(content)


def run(data_dir: str | Path = "data") -> dict:
    root = Path(data_dir)
    docs_dir = root / "docs"
    docs = []
    for path in docs_dir.glob("*.json"):
        try:
            docs.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    answers = _read_jsonl(root / "answer_key.jsonl")
    events = _read_jsonl(root / "events.jsonl.1") + _read_jsonl(root / "events.jsonl")
    world_path = root / "world.json"
    world = json.loads(world_path.read_text()) if world_path.exists() else {}
    pruned = {x.get("load_id") for x in _read_jsonl(root / "pruned_loads.jsonl")}
    violations: list[dict] = []

    def add(rule, load_id, ref, detail):
        if load_id in pruned:
            return
        violations.append({"rule": rule, "load_id": load_id, "doc_ref": ref, "detail": str(detail)})

    by_load: dict[str, list[dict]] = defaultdict(list)
    for doc in docs:
        load_id = doc.get("load_id")
        if load_id:
            by_load[load_id].append(doc)
        ref, kind = doc.get("ref", ""), doc.get("type", "")
        pdf = docs_dir / doc.get("pdf", f"{ref}.pdf")
        pdf_text = None
        if not pdf.exists():
            add("V1", load_id, ref, "PDF is missing")
        else:
            try:
                pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)
                identifier = doc.get("invoice_number") or doc.get("load_id") or doc.get("bol")
                if not identifier or str(identifier) not in pdf_text or "SIMULATED" not in pdf_text:
                    add("V1", load_id, ref, "primary identifier or SIMULATED footer missing")
            except (OSError, ValueError, PdfReadError) as exc:
                add("V1", load_id, ref, f"PDF unreadable: {exc}")
        if kind == "carrier_invoice":
            lines = doc.get("line_items", [])
            total = round(sum(float(line.get("amount", 0)) for line in lines), 2)
            if round(float(doc.get("total", 0)), 2) != total:
                add("V2", load_id, ref, f"total {doc.get('total')} != lines {total}")
            for line in lines:
                if "qty" in line and "rate" in line:
                    amount = round(float(line["qty"]) * float(line["rate"]), 2)
                    if round(float(line.get("amount", 0)), 2) != amount:
                        add("V2", load_id, ref, f"{line.get('desc')} amount does not equal qty × rate")
            if pdf_text is not None:
                section = re.search(r"Description\s+Qty\s+Rate\s+Amount(.*?)TOTAL\s+DUE", pdf_text,
                                    re.IGNORECASE | re.DOTALL)
                number = r"[\d,]+(?:\.\d+)?"
                row_pattern = re.compile(
                    rf"^(?P<desc>.+?)\s+(?P<qty>{number})\s+(?P<rate>-?\$?-?{number})"
                    rf"\s+(?P<amount>-?\$?-?{number})\s*$")
                pdf_lines = []
                if section:
                    for row in section.group(1).splitlines():
                        match = row_pattern.match(row.strip())
                        if match:
                            pdf_lines.append({
                                key: float(match.group(key).replace("$", "").replace(",", ""))
                                for key in ("qty", "rate", "amount")
                            })
                if len(pdf_lines) != len(lines):
                    add("V2", load_id, ref, "invoice PDF line table could not be fully parsed")
                for line in pdf_lines:
                    if round(line["qty"] * line["rate"], 2) != round(line["amount"], 2):
                        add("V2", load_id, ref, "PDF line amount does not equal printed qty × rate")

    carriers = {x.get("id"): x for x in world.get("carriers", [])}
    shippers = {x.get("id"): x for x in world.get("shippers", [])}
    for load_id, items in by_load.items():
        if load_id in pruned:
            continue
        rc = next((d for d in items if d.get("type") == "rate_confirmation"), None)
        bol = next((d for d in items if d.get("type") == "bill_of_lading"), None)
        pod = next((d for d in items if d.get("type") == "proof_of_delivery"), None)
        invoices = [d for d in items if d.get("type") == "carrier_invoice"]
        for inv in invoices:
            ref = inv.get("ref")
            if not rc or not bol or not pod or any(x.get("load_id") != load_id for x in (rc, bol, pod)):
                add("V4", load_id, ref, "rate con, BOL, or POD missing/mismatched")
            if rc:
                for line in inv.get("line_items", []):
                    desc = line.get("desc", "").lower()
                    target = rc.get("linehaul") if "linehaul" in desc else rc.get("fsc") if "fuel" in desc else None
                    if target is not None and round(float(line.get("amount", 0)), 2) != round(float(target), 2):
                        add("V3", load_id, ref, f"{line.get('desc')} differs from rate con")
            carrier = inv.get("carrier", {})
            tender = rc.get("carrier", {}) if rc else {}
            if inv.get("remit_to", {}).get("name") != tender.get("name") and load_id not in pruned:
                add("V8", load_id, ref, "remit-to differs from tendered carrier")
            if carrier.get("usdot") != tender.get("usdot"):
                add("V8", load_id, ref, "invoice carrier USDOT differs from tendered carrier")
            if pod and pod.get("carrier", {}).get("usdot") != tender.get("usdot"):
                add("V8", load_id, ref, "POD carrier USDOT differs from tendered carrier")
            if rc and any(inv.get(key) != rc.get(key) for key in ("bol", "po")):
                add("V4", load_id, ref, "invoice identifiers differ from rate confirmation")
            if rc and bol and pod and any(x.get(key) != rc.get(key) for x in (bol, pod) for key in ("bol", "po")):
                add("V4", load_id, ref, "BOL/POD identifiers differ from rate confirmation")
        if pod:
            if not pod.get("receiver"):
                add("V6", load_id, pod.get("ref"), "receiver signature missing")
            dwell = int((_date(pod.get("departed")) - _date(pod.get("arrived"))).total_seconds() // 60) \
                if _date(pod.get("arrived")) and _date(pod.get("departed")) else None
            if bol and pod:
                loaded_at, arrived_at, departed_at = (_date(bol.get("loaded_at")), _date(pod.get("arrived")),
                                                       _date(pod.get("departed")))
                if loaded_at and arrived_at and departed_at and not loaded_at <= arrived_at <= departed_at:
                    add("V5", load_id, pod.get("ref"), "BOL/POD times are out of order")
            delivered = [e for e in events if e.get("type") == "load.delivered"
                         and e.get("payload", {}).get("load_id") == load_id]
            if delivered and dwell is not None and delivered[-1].get("payload", {}).get("dwell_min") != dwell:
                add("V5", load_id, pod.get("ref"), "load.delivered dwell differs from POD")
            det_logs = [d for d in items if d.get("type") == "detention_log"]
            scenario = next((a.get("scenario") for a in answers if a.get("load_id") == load_id), "")
            if bool(det_logs) != (scenario in SUPPORTED_DETENTION):
                add("V7", load_id, pod.get("ref"), "detention log presence does not match scenario")
            for inv in invoices:
                for line in inv.get("line_items", []):
                    desc = line.get("desc", "")
                    is_detention = "detention" in desc.lower()
                    match = re.search(r"Detention\s+(\d+(?:\.\d+)?)\s*h\b", desc, re.IGNORECASE)
                    if is_detention and not match:
                        add("V7", load_id, inv.get("ref"), "detention hours missing from line")
                    if is_detention and det_logs and dwell is not None and rc:
                        claimed = float(match.group(1)) if match else 0
                        free = int(rc.get("free_time_min", 0))
                        if det_logs[0].get("dwell_min") != dwell or claimed != math.ceil((dwell-free)/60):
                            add("V7", load_id, det_logs[0].get("ref"), "detention dwell or hours mismatch")

        # Verify every party embedded in a source document against the world directory.
        for doc in items:
            for key, directory in (("carrier", carriers), ("shipper", shippers)):
                party = doc.get(key)
                if not isinstance(party, dict):
                    continue
                expected = directory.get(party.get("id"))
                if not expected or any(party.get(k) != expected.get(k) for k in
                                       ("name", "usdot", "mc", "cik", "phone", "email")):
                    add("V10", load_id, doc.get("ref"), f"{key} differs from world directory")

    # Event order and document references.
    timestamps: dict[str, list] = defaultdict(list)
    for event in events:
        load_id = event.get("payload", {}).get("load_id")
        if load_id and _date(event.get("sim_ts")):
            timestamps[load_id].append(_date(event["sim_ts"]))
    for load_id, times in timestamps.items():
        if times != sorted(times):
            add("V5", load_id, None, "event sim_ts is not nondecreasing")
    first_refs = {}
    for event in events:
        sim_time = _date(event.get("sim_ts"))
        for ref in event.get("evidence_refs", []):
            if str(ref).startswith("doc:"):
                first_refs.setdefault(str(ref)[4:], sim_time)
    for doc in docs:
        doc_time = _date(doc.get("date"))
        ref_time = first_refs.get(doc.get("ref"))
        if doc_time and ref_time and doc_time > ref_time:
            add("V5", doc.get("load_id"), doc.get("ref"), "document date is after its first referencing event")

    # Email attachments and unique Message-IDs.
    mids = Counter()
    mail_records = []
    for path in (root / "mail").glob("*.eml"):
        try:
            message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            mail_records.append((path, message))
            mid = message.get("Message-ID")
            if mid:
                mids[mid] += 1
            for part in message.iter_attachments():
                filename = part.get_filename()
                if filename and not (docs_dir / Path(filename).name).exists():
                    add("V4", None, path.stem, f"attachment {filename} missing")
        except OSError as exc:
            add("V4", None, path.stem, exc)
    for mid, count in mids.items():
        if count > 1:
            add("V4", None, mid, "Message-ID is not unique")
    for path, message in mail_records:
        attachments = {Path(p.get_filename()).name for p in message.iter_attachments() if p.get_filename()}
        for invoice in (d for d in docs if d.get("type") == "carrier_invoice"
                        and Path(d.get("pdf", "")).name in attachments):
            load_id = invoice.get("load_id")
            rc = next((d for d in by_load.get(load_id, []) if d.get("type") == "rate_confirmation"), {})
            if parseaddr(str(message.get("From", "")))[1].lower() != \
                    str(rc.get("carrier", {}).get("email", "")).lower():
                add("V4", load_id, invoice.get("ref"), "invoice email sender differs from registered carrier email")

    shipper_docs = {d.get("invoice_id"): d for d in docs if d.get("type") == "shipper_invoice"}
    bank_records = []
    for path in (root / "bank").glob("*"):
        try:
            for line in path.read_text().splitlines():
                parts = line.rstrip("/").split(",")
                if len(parts) >= 7 and parts[0] == "16":
                    bank_records.append((round(int(parts[2]) / 100, 2), parts[4], parts[5], parts[6]))
        except (OSError, ValueError):
            continue
    for answer in answers:
        load_id = answer.get("load_id")
        if load_id in pruned:
            continue
        sinv = shipper_docs.get(f"SINV-{load_id}")
        if not sinv:
            add("V4", load_id, f"SINV-{load_id}", "shipper invoice missing")
            continue
        shipper_name = sinv.get("shipper", {}).get("name")
        matching = [row for row in bank_records if row[3] == shipper_name
                    and row[2] in {f"SINV-{load_id}", f"SINV-MISSING-{load_id}"}]
        if matching:
            amount, _txn, reference, _ = matching[-1]
            remittance_amount = None
            deduction = 0.0
            for _, message in mail_records:
                body = "\n".join(
                    _text_part(part) for part in message.walk() if part.get_content_type() == "text/plain")
                match = re.search(r"Payment of \$([\d,.]+)", body)
                if match and (f"SINV-{load_id}" in body or f"SINV-MISSING-{load_id}" in body):
                    remittance_amount = float(match.group(1).replace(",", ""))
                    ded = re.search(r"Deduction \$([\d,.]+)", body)
                    deduction = float(ded.group(1).replace(",", "")) if ded else 0.0
            if reference != f"SINV-{load_id}" or remittance_amount is None \
                    or amount != remittance_amount or round(amount, 2) != round(float(sinv.get("total", 0))-deduction, 2):
                add("V9", load_id, None, "bank/reference/remittance amount mismatch")
        else:
            add("V9", load_id, None, "shipper payment or remittance reference missing")

    # Answer-key completeness, allowed-rule constraints, and planted-fact checks.
    answer_by_load = Counter(a.get("load_id") for a in answers)
    for load_id, count in answer_by_load.items():
        if count != 1:
            add("V11", load_id, None, f"answer key has {count} entries")
    observed_loads = {d.get("load_id") for d in docs if d.get("load_id")}
    observed_loads.update(event.get("payload", {}).get("load_id") for event in events
                          if event.get("type") == "load.tendered" and event.get("payload", {}).get("load_id"))
    for load_id in observed_loads - pruned:
        if answer_by_load.get(load_id, 0) != 1:
            add("V11", load_id, None, "completed load must have exactly one answer-key entry")
    for load_id in set(answer_by_load) - observed_loads - pruned:
        add("V11", load_id, None, "answer-key entry has no corresponding load")
    rules_by_load: dict[str, set[str]] = defaultdict(set)
    for violation in violations:
        if violation["load_id"]:
            rules_by_load[violation["load_id"]].add(violation["rule"])
    answer_map = {a.get("load_id"): a for a in answers}
    for load_id, answer in answer_map.items():
        if load_id in pruned:
            continue
        scenario = answer.get("scenario")
        actual = rules_by_load.get(load_id, set())
        allowed = ALLOWED.get(scenario, set())
        if not actual.issubset(allowed):
            violations.append({"rule": "V11", "load_id": load_id, "doc_ref": None,
                               "detail": f"unexpected rules {sorted(actual-allowed)}"})
        if allowed and not actual.intersection(allowed):
            violations.append({"rule": "V11", "load_id": load_id, "doc_ref": None,
                               "detail": f"{scenario} did not violate an allowed rule"})
        items = by_load.get(load_id, [])
        invoice_docs = [d for d in items if d.get("type") == "carrier_invoice"]
        if scenario == "lumper_no_receipt":
            if not any("lumper" in x.get("desc", "").lower() for d in invoice_docs for x in d.get("line_items", [])) \
                    or any(d.get("type") == "lumper_receipt" for d in items):
                add("V12", load_id, None, "lumper-without-receipt fact missing")
        elif scenario == "detention_unsupported":
            pod = next((d for d in items if d.get("type") == "proof_of_delivery"), {})
            rc = next((d for d in items if d.get("type") == "rate_confirmation"), {})
            dwell = int((_date(pod.get("departed")) - _date(pod.get("arrived"))).total_seconds() // 60) \
                if _date(pod.get("departed")) and _date(pod.get("arrived")) else 9999
            if not any("detention" in x.get("desc", "").lower() for d in invoice_docs for x in d.get("line_items", [])) \
                    or dwell > int(rc.get("free_time_min", 0)):
                add("V12", load_id, None, "unsupported detention fact missing")
        elif scenario == "duplicate_renumbered":
            if len({x.get("invoice_number") for x in invoice_docs}) < 2:
                add("V12", load_id, None, "renumbered duplicate missing")
        elif scenario == "duplicate_invoice":
            count = sum(1 for p, m in mail_records if any(Path(a.get_filename() or "").stem in
                         {x.get("ref") for x in invoice_docs} for a in m.iter_attachments()))
            count = max(count, sum(1 for e in events if e.get("type") == "email.received"
                                  and e.get("case_hint") == f"CASE-{load_id}"))
            if count < 2:
                add("V12", load_id, None, "duplicate invoice email missing")
        elif scenario == "bank_change":
            carrier = next((d.get("carrier", {}) for d in items if d.get("type") == "rate_confirmation"), {})
            domain = str(carrier.get("email", "")).rsplit("@", 1)[-1]
            if not any("changed banks" in "\n".join(str(p.get_payload()) for p in m.walk()) and
                       parseaddr(str(m.get("From", "")))[1].rsplit("@", 1)[-1].casefold() != domain.casefold()
                       for _, m in mail_records):
                add("V12", load_id, None, "lookalike bank-change email missing")
        elif scenario == "shipper_short_pay":
            sinv = shipper_docs.get(f"SINV-{load_id}")
            records = [row for row in bank_records if row[3] == (sinv or {}).get("shipper", {}).get("name")
                       and row[2] == f"SINV-{load_id}"]
            if not sinv or not records or max(r[0] for r in records) >= float(sinv.get("total", 0)):
                add("V12", load_id, None, "short payment fact missing")
    answer_map = {a.get("load_id"): a for a in answers}
    unexpected = [v for v in violations if v["rule"] == "V11" or
                  (v.get("load_id") and v["rule"] not in ALLOWED.get(
                      answer_map.get(v["load_id"], {}).get("scenario"), set())) or not v.get("load_id")]
    report = {"ok": not unexpected, "violations": violations, "unexpected_violations": unexpected,
              "pruned": sorted(x for x in pruned if x),
              "by_rule": dict(Counter(v["rule"] for v in violations)),
              "by_scenario": dict(Counter(a.get("scenario") for a in answers))}
    (root / "verify_report.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run(args.data)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("Rule | Violations")
        print("--- | ---:")
        for rule in [f"V{i}" for i in range(1, 13)]:
            print(f"{rule} | {report['by_rule'].get(rule, 0)}")
        print("Scenario | Loads")
        print("--- | ---:")
        for name, count in sorted(report["by_scenario"].items()):
            print(f"{name} | {count}")
        print(f"Pruned loads: {len(report['pruned'])}; unexpected violations: "
              f"{len(report['unexpected_violations'])}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
