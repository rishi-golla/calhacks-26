"""Export ingest state as the trusted source dataset agents read through SOURCE_DATA_PATH.

Shape: {"cases": {case_id: {...}}, "companies": {id: {...}}, "contacts": {id: [...]}} (see
common/source_repository.py and sim/fixtures/brokerage_source.json). One case per resolved case ID:
CASE-<load> (carrier invoice), CASE-<load>-R (re-numbered invoice), CASE-<load>-BANK (bank change),
CASE-<load>-AR (shipper receivable). Simulated brokerage records carry "synthetic": true; directory
records keep their public-registry provenance. Values ingest never observed are left out, never defaulted.

    python -m ingest.source_export                         # data/ingest.db -> data/source.json
    python -m ingest.source_export --replay data/events.jsonl --db /tmp/replay.db   # offline dataset
"""
import argparse
import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from integrations import directory

REQUIRED_DOCUMENTS = ["rate_confirmation", "pod"]
# Public registry records are independently verifiable contacts; fixtures stand in for them in offline worlds.
REGISTRY_CONFIDENCE = {"fmcsa_census": 0.95, "sec_edgar": 0.9}
FIXTURE_SOURCES = {"prd_hero_fixture", "offline_fixture"}
LINE_KINDS = (("linehaul", "linehaul"), ("fuel", "fuel_surcharge"), ("detention", "detention"),
              ("lumper", "lumper"), ("layover", "layover"), ("tonu", "tonu"), ("stop-off", "extra_stop"),
              ("stop off", "extra_stop"), ("driver assist", "driver_assist"), ("quickpay", "quickpay_discount"))
# Accessorials the rate confirmation pays only against a receipt (detention log or lumper receipt).
RECEIPT_DOCUMENT = {"detention": "detention_log", "lumper": "lumper_receipt"}
# Ingest flags that mean the invoice names a different carrier than the one tendered (double brokering).
# remit_to_mismatch is a payee question (factoring) and goes through the bank-change path instead.
IDENTITY_FLAGS = ("usdot_mismatch", "carrier_mismatch")
BANK_CHANGE_FIELDS = ("requested_via", "requested_by", "registered_email", "sender_matches_registered", "channel",
                      "new_payee", "new_account_last4", "new_routing_last4", "effective_date",
                      "next_payment_amount", "urgency_language", "source_email")


def path() -> Path:
    return Path(os.getenv("SOURCE_EXPORT_PATH") or Path(os.getenv("SIM_DATA_DIR", "data")) / "source.json")


def _rows(db: sqlite3.Connection, table: str) -> list[dict]:
    return [json.loads(row[0]) for row in db.execute(f"SELECT json FROM {table}")]


def _iso(value) -> str | None:
    """Ingest timestamps are UTC; document dates are rendered without a zone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def _line_kind(desc: str) -> str:
    text = desc.casefold()
    return next((kind for term, kind in LINE_KINDS if term in text), "other")


def _line_items(items: list[dict], documents: list[dict]) -> list[dict]:
    out = []
    for item in items:
        line = {**item, "kind": item.get("kind") or _line_kind(str(item.get("desc", "")))}
        receipt_kind = RECEIPT_DOCUMENT.get(line["kind"])
        if receipt_kind:
            receipt = next((doc["ref"] for doc in documents if doc.get("kind") == receipt_kind), None)
            line.update(receipt_required=True, receipt_ref=receipt)
        out.append(line)
    return out


def _party(record: dict | None, *keys: str) -> dict:
    record = record or {}
    return {key: record[key] for key in ("id", "name", *keys) if record.get(key) is not None}


def _load_facts(load: dict, brokerage: str | None) -> dict:
    documents = [{key: value for key, value in doc.items() if value is not None}
                 for doc in load.get("documents", [])]
    facts = {"load_id": load["load_id"], "lane": load.get("lane"), "pro": load.get("pro"), "bol": load.get("bol"),
             "po": load.get("po"), "brokerage": brokerage,
             "carrier": _party(load.get("carrier"), "usdot", "mc"), "shipper": _party(load.get("shipper"), "cik"),
             "carrier_id": (load.get("carrier") or {}).get("id"), "shipper_id": (load.get("shipper") or {}).get("id"),
             "contract_rate": load.get("contract_rate"), "free_time_min": load.get("free_time_min"),
             "gps_dwell_min": load.get("gps_dwell_min"), "required_documents": REQUIRED_DOCUMENTS,
             "documents": documents}
    if load.get("gps_dwell_min") is not None:
        facts["gps"] = {"delivery_dwell_min": load["gps_dwell_min"], "free_time_min": load.get("free_time_min")}
    return facts


def _receivable(shipper_invoice: dict | None, deposits: list[dict]) -> dict:
    if not shipper_invoice:
        return {}
    invoice_id = shipper_invoice["invoice_id"]
    return {"shipper_invoice_id": invoice_id, "shipper_invoice_amount": shipper_invoice.get("amount"),
            "deposits": [{key: deposit[key] for key in ("id", "shipper_id", "invoice_id", "amount", "posted_at")}
                         for deposit in deposits if deposit.get("invoice_id") == invoice_id],
            "deduction_reason": shipper_invoice.get("deduction_reason")}


def _payable(invoice: dict, invoices: list[dict], documents: list[dict]) -> dict:
    issued_at = _iso(invoice.get("invoice_date")) or _iso(invoice.get("date"))
    line_items = _line_items(invoice.get("line_items") or [], documents)
    claim = next((line["kind"] for line in line_items if line["kind"] in RECEIPT_DOCUMENT
                  or line["kind"] in {"layover", "tonu", "extra_stop", "driver_assist"}), None)
    previous = [{"id": other["invoice_id"], "carrier_id": other.get("carrier_id"), "load_id": other.get("load_id"),
                 "amount": other.get("invoice_amount"),
                 "issued_at": _iso(other.get("invoice_date")) or _iso(other.get("date"))}
                for other in invoices if other["invoice_id"] != invoice["invoice_id"]]
    identity_flags = [flag for flag in invoice.get("ingest_flags") or [] if flag in IDENTITY_FLAGS]
    identity = {"tendered_carrier_id": invoice.get("carrier_id"), "tendered_usdot": (invoice.get("carrier") or {}).get("usdot"),
                "invoice_usdot": invoice.get("invoice_carrier_usdot"),
                "invoice_carrier_name": ((invoice.get("extraction") or {}).get("fields") or {}).get("carrier_name"),
                "flags": identity_flags} if identity_flags else None
    return {"invoice_id": invoice["invoice_id"], "invoice_number": invoice.get("invoice_number"),
            "carrier_identity_mismatch": bool(identity_flags), "carrier_identity": identity,
            "resubmission_refs": invoice.get("_resubmissions"),
            "invoice_amount": invoice.get("invoice_amount"), "line_items": line_items, "issued_at": issued_at,
            "period": issued_at[:7] if issued_at else None, "claim_type": claim,
            "detention_hours_claimed": invoice.get("detention_hours_claimed"),
            "receipt_attached": invoice.get("receipt_attached"), "pod_signed": invoice.get("pod_signed"),
            "remit_to": invoice.get("remit_to"), "invoice_carrier_usdot": invoice.get("invoice_carrier_usdot"),
            "ingest_flags": invoice.get("ingest_flags") or [], "previous_invoices": previous}


def _bank_change(change: dict) -> dict:
    return {"bank_change_requested": True, "bank_change": {key: change.get(key) for key in BANK_CHANGE_FIELDS}}


def _finish(case_id: str, case: dict, refs: list[str]) -> dict:
    case = {key: value for key, value in case.items() if value is not None}
    case.update(case_id=case_id, synthetic=True,
                evidence_refs=list(dict.fromkeys(ref for ref in refs if isinstance(ref, str) and ref)))
    return case


def build_cases(db: sqlite3.Connection, brokerage: str | None = None) -> dict[str, dict]:
    loads = {load["load_id"]: load for load in _rows(db, "loads")}
    invoices = _rows(db, "invoices")
    shipper_invoices = {item.get("load_id"): item for item in _rows(db, "shipper_invoices")}
    deposits = _rows(db, "deposits")
    changes = _rows(db, "bank_changes")
    by_load: dict[str, list[dict]] = {}
    for invoice in invoices:
        by_load.setdefault(invoice["load_id"], []).append(invoice)
    for items in by_load.values():
        items.sort(key=lambda item: (item.get("case_id", "").endswith("-R"), item.get("invoice_date") or ""))
    changed_invoices = {change.get("invoice_id") for change in changes if change.get("invoice_id")}

    cases: dict[str, dict] = {}
    for load_id, load in loads.items():
        facts = _load_facts(load, brokerage)
        doc_refs = [doc["ref"] for doc in facts["documents"]]
        receivable = _receivable(shipper_invoices.get(load_id), deposits)
        sinv_refs = (shipper_invoices.get(load_id) or {}).get("_evidence_refs", [])
        load_invoices = by_load.get(load_id, [])
        for invoice in load_invoices:
            case = {**facts, **receivable, **_payable(invoice, load_invoices, facts["documents"])}
            if invoice["invoice_id"] in changed_invoices:
                case.update(_bank_change(next(c for c in changes if c.get("invoice_id") == invoice["invoice_id"])))
            cases[invoice["case_id"]] = _finish(invoice["case_id"], case,
                                                [*doc_refs, *invoice.get("evidence_refs", [])])
        load_case = load.get("case_id") or f"CASE-{load_id}"
        if not load_invoices:
            # Delivered but not billed yet: the load and its documents, no payable to match.
            cases[load_case] = _finish(load_case, {**facts, **receivable}, [*doc_refs, *sinv_refs])
        if receivable:
            # The receivable keeps the original carrier invoice so margin and payables have a source.
            original = _payable(load_invoices[0], load_invoices, facts["documents"]) if load_invoices else {}
            issued = _iso(shipper_invoices[load_id].get("_issued_at"))
            case = {**facts, **original, **receivable, "period": issued[:7] if issued else original.get("period")}
            cases[f"{load_case}-AR"] = _finish(f"{load_case}-AR", case, [*sinv_refs, *doc_refs])
    for change in changes:
        load = loads.get(change.get("load_id") or "")
        invoice = next((item for item in invoices if item["invoice_id"] == change.get("invoice_id")), None)
        case = _load_facts(load, brokerage) if load else {
            "brokerage": brokerage, "carrier_id": change.get("carrier_id"),
            "carrier": _party(change.get("carrier"), "usdot", "mc")}
        if invoice:
            case.update(_payable(invoice, by_load.get(invoice["load_id"], []), case.get("documents", [])))
        sent = _iso(change.get("date")) or _iso(change.get("sim_ts"))
        case.setdefault("period", sent[:7] if sent else None)
        case.update(_bank_change(change))
        refs = [doc["ref"] for doc in case.get("documents", [])] + list(change.get("evidence_refs", []))
        cases[change["case_id"]] = _finish(change["case_id"], case, refs)
    return cases


def _company(record: dict) -> dict:
    source = record.get("source")
    keep = ("id", "name", "legal_name", "kind", "usdot", "mc", "cik", "ticker", "industry", "address", "city", "state",
            "phone", "email", "website", "terms", "authority_status", "owner_operator", "power_units", "drivers",
            "mcs150_date", "source", "source_url")
    company = {key: record[key] for key in keep if record.get(key) is not None}
    fixture = source in FIXTURE_SOURCES
    company.update(verified=source in REGISTRY_CONFIDENCE or fixture,
                   confidence=REGISTRY_CONFIDENCE.get(source, 1.0 if fixture else 0.0),
                   synthetic=source not in REGISTRY_CONFIDENCE)
    if fixture:
        company.setdefault("source_url", f"fixture://directory/{record['id']}")
    return company


def _contacts(record: dict, company: dict) -> list[dict]:
    listed = record.get("directory") if isinstance(record.get("directory"), dict) else {}
    phone, email = listed.get("phone") or record.get("phone"), listed.get("email", record.get("email"))
    if not phone and not email:
        return []
    source = listed.get("source") or company.get("source")
    name = listed.get("name") or ("Registered contact (FMCSA MCS-150)" if source == "fmcsa_census"
                                  else "Registered company contact")
    contact = {"id": f"CT-{record['id']}", "company_id": record["id"], "name": name, "phone": phone, "email": email,
               "source": source, "source_url": listed.get("source_url") or company.get("source_url"),
               "verified": company["verified"], "confidence": company["confidence"],
               "synthetic": company["synthetic"]}
    return [{key: value for key, value in contact.items() if value is not None}]


def build_directory(world: dict) -> tuple[dict, dict]:
    companies, contacts = {}, {}
    for record in [*world.get("carriers", []), *world.get("shippers", [])]:
        if not record.get("id"):
            continue
        company = _company(record)
        companies[record["id"]] = company
        if found := _contacts(record, company):
            contacts[record["id"]] = found
    return companies, contacts


def build(db: sqlite3.Connection, world: dict | None = None) -> dict:
    world = directory.load() if world is None else world
    companies, contacts = build_directory(world)
    return {"schema": 1, "generated_at": datetime.now(UTC).isoformat(),
            "source_kind": "w4_ingest_export", "cases": build_cases(db, world.get("brokerage")),
            "companies": companies, "contacts": contacts}


def write(dataset: dict, target: Path | None = None) -> Path:
    """Readers see either the previous file or the new one, never a partial write."""
    target = target or path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with temporary.open("w") as handle:
        json.dump(dataset, handle, default=str, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)
    return target


def export(db: sqlite3.Connection, target: Path | None = None) -> Path:
    return write(build(db), target)


class Exporter:
    """Keeps the export current: right before a case becomes routable, otherwise within `max_age_s`."""

    def __init__(self, target: Path | None = None, max_age_s: float = 1.0):
        self.target = target
        self.max_age_s = max_age_s
        self.dirty = True  # the first flush writes the file even before any new arrival
        self.last_flush = 0.0

    def changed(self) -> None:
        self.dirty = True

    def flush(self, db: sqlite3.Connection, dataset: dict | None = None) -> None:
        """Write `dataset` (built from the current state of `db`) or, when stale, a fresh build."""
        if dataset is not None or self.dirty:
            write(dataset or build(db), self.target)
            self.dirty = False
            self.last_flush = time.monotonic()

    def flush_if_due(self, db: sqlite3.Connection) -> None:
        """Flush stale state at most once per `max_age_s`: a full rebuild and fsync per raw arrival capped ingest
        at about ten events per second, while nothing but a routable case needs the file current immediately."""
        if self.dirty and time.monotonic() - self.last_flush >= self.max_age_s:
            self.flush(db)


async def _replay(events_path: Path, db: sqlite3.Connection) -> int:
    from ingest.main import handle
    count = 0
    for line in events_path.read_text().splitlines():
        if line.strip():
            await handle(json.loads(line), db)
            count += 1
    db.commit()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, help="ingest database (default: $SIM_DATA_DIR/ingest.db)")
    parser.add_argument("--replay", type=Path, help="raw events.jsonl to ingest into --db before exporting")
    parser.add_argument("--out", type=Path, help="output path (default: $SOURCE_EXPORT_PATH or data/source.json)")
    args = parser.parse_args()
    if args.replay and not args.db:
        parser.error("--replay needs a fresh --db so live ingest state is never re-ingested")
    from ingest.main import connect, init_db
    if args.db:
        db = sqlite3.connect(args.db)
        init_db(db)
    else:
        db = connect()
    try:
        replayed = asyncio.run(_replay(args.replay, db)) if args.replay else 0
        dataset = build(db)
        target = write(dataset, args.out)
    finally:
        db.close()
    kinds: dict[str, int] = {}
    for case_id in dataset["cases"]:
        suffix = re.search(r"-(R|BANK|AR)$", case_id)
        kinds[suffix.group(1) if suffix else "AP"] = kinds.get(suffix.group(1) if suffix else "AP", 0) + 1
    print(f"replayed {replayed} events; {len(dataset['cases'])} cases {kinds}, "
          f"{len(dataset['companies'])} companies -> {target}")


if __name__ == "__main__":
    main()
