"""Turn raw simulator emails and bank files into the case-opening events consumed by the router."""
import asyncio
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import redis.asyncio as redis

from ingest.documents import (
    classify,
    extract_detention_log_rules,
    extract_invoice,
    extract_pod_rules,
    read_email,
)
from integrations import directory

log = logging.getLogger("ingest")
STREAM = "events"
GROUP = "ingest"
CONSUMER = "ingest-1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS loads (load_id TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoices (invoice_id TEXT PRIMARY KEY, load_id TEXT, invoice_number TEXT, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shipper_invoices (invoice_id TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_offsets (file TEXT PRIMARY KEY, offset INT NOT NULL);
"""


def data_dir() -> Path:
    return Path(os.getenv("SIM_DATA_DIR", "data"))


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def connect() -> sqlite3.Connection:
    path = data_dir() / "ingest.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    init_db(db)
    return db


def _dump(value: dict) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _read_json(row) -> dict | None:
    return json.loads(row[0]) if row else None


def _load(db: sqlite3.Connection, load_id: str | None) -> dict | None:
    if not load_id:
        return None
    return _read_json(db.execute("SELECT json FROM loads WHERE load_id=?", (load_id,)).fetchone())


def _find_load(db: sqlite3.Connection, fields: dict) -> dict | None:
    load = _load(db, fields.get("load_id"))
    if load:
        return load
    bol = fields.get("bol")
    if bol:
        for row in db.execute("SELECT json FROM loads"):
            candidate = json.loads(row[0])
            if candidate.get("bol") == bol:
                return candidate
    return None


def _event(trigger: dict, event_type: str, payload: dict, priority: str = "P2",
           evidence_refs: list[str] | None = None) -> dict:
    return {"event_id": f"EVT-{uuid.uuid4().hex[:10]}", "type": event_type,
            "ts": datetime.now(UTC).isoformat(), "occurred_at": trigger.get("occurred_at") or
            datetime.now(UTC).isoformat(), "case_hint": payload.get("case_id") or trigger.get("case_hint"),
            "priority": priority, "source": "ingest", "payload": payload,
            "evidence_refs": evidence_refs or []}


def _document_event(trigger: dict, kind: str, status: str, fields: dict | None = None,
                    confidence: dict | None = None, method: str = "rules", load: dict | None = None,
                    case_id: str | None = None, evidence_refs: list[str] | None = None) -> dict:
    payload = {"status": status, "kind": kind, "fields": fields or {}, "confidence": confidence or {},
               "method": method, "matched_load_id": load.get("load_id") if load else None,
               "case_id": case_id or (f"CASE-{load['load_id']}" if load else None)}
    return _event(trigger, "document.extracted", payload, evidence_refs=evidence_refs)


def _evidence_refs(message: dict) -> list[str]:
    refs = [f"doc:{item['filename'].removesuffix('.pdf')}" for item in message.get("attachments", [])]
    if message.get("mail_id"):
        refs.append(f"mail:{message['mail_id']}")
    return refs


async def _invoice_email(trigger: dict, message: dict, db: sqlite3.Connection) -> list[dict]:
    attachments = message.get("attachments", [])
    invoice_doc = next((item for item in attachments if classify(item.get("text", "")) == "carrier_invoice"), None)
    if not invoice_doc:
        return [_document_event(trigger, "carrier_invoice", "needs_review", evidence_refs=_evidence_refs(message))]
    extracted = await extract_invoice(invoice_doc.get("text", ""), str(trigger.get("case_hint") or "unmatched"))
    fields = extracted["fields"]
    load = _find_load(db, fields)
    refs = _evidence_refs(message)
    if not load or fields.get("total") is None or not fields.get("invoice_number"):
        return [_document_event(trigger, "carrier_invoice", "needs_review", fields, extracted["confidence"],
                                extracted["method"], load, evidence_refs=refs)]

    carrier_fields = load.get("carrier") or {}
    usdot = fields.get("usdot") or carrier_fields.get("usdot")
    carrier = directory.by_usdot(str(usdot)) if usdot else None
    carrier = carrier or carrier_fields
    load_id = load["load_id"]
    invoice_number = fields["invoice_number"]
    invoice_id = f"INV-{usdot or ''}-{invoice_number}"
    existing = db.execute("SELECT invoice_number FROM invoices WHERE load_id=?", (load_id,)).fetchall()
    case_id = f"CASE-{load_id}" + ("-R" if any(row[0] != invoice_number for row in existing) else "")

    pod_text = next((item.get("text", "") for item in attachments
                     if classify(item.get("text", "")) == "pod"), None)
    pod = extract_pod_rules(pod_text) if pod_text is not None else None
    detention_text = next((item.get("text", "") for item in attachments
                           if classify(item.get("text", "")) == "detention_log"), None)
    detention_log = extract_detention_log_rules(detention_text) if detention_text is not None else None
    has_lumper_receipt = any(classify(item.get("text", "")) == "lumper_receipt" for item in attachments)
    has_receipt = any(classify(item.get("text", "")) in ("detention_log", "lumper_receipt")
                      for item in attachments)
    line_items = fields.get("line_items", [])
    has_accessorial_line = any(not re.search(r"\b(linehaul|fuel surcharge|fsc)\b",
                                             str(item.get("desc", "")), re.IGNORECASE)
                               for item in line_items)
    receipt_attached = True if has_receipt else False if has_accessorial_line else None
    gps_dwell_min = load.get("gps_dwell_min")
    free_time_min = load.get("free_time_min")
    payload = {
        "case_id": case_id, "load_id": load_id, "lane": load.get("lane"), "pro": load.get("pro"),
        "bol": load.get("bol"), "po": load.get("po"), "invoice_id": invoice_id,
        "invoice_number": invoice_number, "carrier": carrier, "shipper": load.get("shipper"),
        "contract_rate": load.get("contract_rate"), "invoice_amount": fields["total"], "line_items": line_items,
        "detention_hours_claimed": next(
            (
                float(match.group(1))
                for item in line_items
                if (match := re.search(r"\bDetention\s+(\d+(?:\.\d+)?)\s*h\b",
                                       str(item.get("desc", "")), re.IGNORECASE))
            ),
            0,
        ),
        "free_time_min": free_time_min,
        "gps": {"delivery_dwell_min": gps_dwell_min, "free_time_min": free_time_min},
        "gps_dwell_min": gps_dwell_min, "receipt_attached": receipt_attached,
        "pod_signed": bool(pod and pod["signed"]), "remit_to": {"name": fields.get("remit_to_name")},
        "source_email": message.get("mail_id"), **(load.get("operator_overrides") or {}),
        "extraction": extracted,
    }
    document_fields = {**fields, "attachments": {"pod": pod, "detention_log": detention_log,
                                                   "lumper_receipt": has_lumper_receipt}}
    document = _document_event(trigger, "carrier_invoice", "extracted", document_fields, extracted["confidence"],
                               extracted["method"], load, case_id, refs)
    priority = "P1" if has_accessorial_line or (
        payload["contract_rate"] is not None and payload["invoice_amount"] > payload["contract_rate"]) else "P2"
    invoice_event = _event(trigger, "carrier_invoice.received", payload, priority, refs)
    db.execute("INSERT OR REPLACE INTO invoices(invoice_id, load_id, invoice_number, json) VALUES (?,?,?,?)",
               (invoice_id, load_id, invoice_number, _dump(payload)))
    return [document, invoice_event]


def _bank_change(trigger: dict, message: dict, db: sqlite3.Connection) -> list[dict]:
    refs = _evidence_refs(message)
    subject_match = re.search(r"remittance details\s*-\s*(.+)$", message.get("subject", ""), re.IGNORECASE)
    number_match = re.search(r"\binvoice\s+(\S+),", message.get("body", ""), re.IGNORECASE)
    account_match = re.search(r"\bAccount\s+\*{4}(\d{4})", message.get("body", ""), re.IGNORECASE)
    carrier_name = subject_match.group(1).strip() if subject_match else ""
    carriers = directory.load().get("carriers", [])
    carrier = next((item for item in carriers if carrier_name.casefold() in {
        str(item.get("name", "")).casefold(), str(item.get("legal_name", "")).casefold()}), None)
    invoice = None
    if carrier and number_match:
        for row in db.execute("SELECT json FROM invoices WHERE invoice_number=?", (number_match.group(1),)):
            candidate = json.loads(row[0])
            registered = candidate.get("carrier") or {}
            if (str(registered.get("name", "")).casefold() == carrier_name.casefold()
                    or str(registered.get("usdot", "")) == str(carrier.get("usdot", ""))):
                invoice = candidate
                break
    if not carrier or not invoice or not account_match:
        fields = {"carrier": carrier_name or None, "invoice_number": number_match.group(1) if number_match else None,
                  "new_account_last4": account_match.group(1) if account_match else None}
        return [_document_event(trigger, "bank_change_request", "needs_review", fields,
                                {key: 0.95 if value else 0.0 for key, value in fields.items()},
                                evidence_refs=refs)]

    invoice_carrier = invoice.get("carrier") or carrier
    remit_name = (invoice.get("remit_to") or {}).get("name")
    carrier_name = carrier.get("name") or invoice_carrier.get("name", "")
    requested_via = ("invoice_remit_to" if remit_name and
                     remit_name.strip().casefold() != carrier_name.strip().casefold() else "email")
    load_id = invoice.get("load_id")
    case_id = f"CASE-{load_id}-BANK"
    payload = {"case_id": case_id, "load_id": load_id, "lane": invoice.get("lane"), "pro": invoice.get("pro"),
               "bol": invoice.get("bol"), "po": invoice.get("po"), "carrier": invoice_carrier,
               "invoice_id": invoice.get("invoice_id"), "requested_via": requested_via,
               "from": message.get("from"), "registered_email": carrier.get("email"),
               "new_payee": remit_name or carrier_name, "new_account_last4": account_match.group(1),
               "source_email": message.get("mail_id")}
    document = _document_event(
        trigger, "bank_change_request", "extracted",
        {"carrier": carrier_name, "invoice_number": number_match.group(1),
         "new_account_last4": account_match.group(1), "requested_via": requested_via},
        {"carrier": 0.95, "invoice_number": 0.95, "new_account_last4": 0.95, "requested_via": 0.95},
        load={"load_id": load_id}, case_id=case_id, evidence_refs=refs)
    return [document, _event(trigger, "bank_change.requested", payload, "P0", refs)]


def _remittance_advice(trigger: dict, message: dict, db: sqlite3.Connection) -> list[dict]:
    refs = _evidence_refs(message)
    invoice_match = re.search(r"\binvoice\s+(SINV-\S+)", message.get("body", ""), re.IGNORECASE)
    deduction_match = re.search(r"Deduction\s+\$[\d,.]+:\s*(.+)", message.get("body", ""), re.IGNORECASE)
    invoice_id = invoice_match.group(1) if invoice_match else None
    reason = deduction_match.group(1).strip().rstrip(".") if deduction_match else None
    row = db.execute("SELECT json FROM shipper_invoices WHERE invoice_id=?", (invoice_id,)).fetchone() \
        if invoice_id else None
    invoice = _read_json(row)
    if not invoice:
        fields = {"invoice_id": invoice_id, "deduction_reason": reason}
        return [_document_event(trigger, "remittance_advice", "needs_review", fields,
                                {"invoice_id": 0.95 if invoice_id else 0.0,
                                 "deduction_reason": 0.95 if reason else 0.0}, evidence_refs=refs)]
    invoice["deduction_reason"] = reason
    invoice["source_email"] = message.get("mail_id")
    db.execute("UPDATE shipper_invoices SET json=? WHERE invoice_id=?", (_dump(invoice), invoice_id))
    fields = {"invoice_id": invoice_id, "deduction_reason": reason}
    case_id = f"{invoice.get('case_id')}-AR"
    return [_document_event(trigger, "remittance_advice", "extracted", fields,
                            {"invoice_id": 0.95, "deduction_reason": 0.95 if reason else 0.0},
                            load={"load_id": invoice.get("load_id")}, case_id=case_id, evidence_refs=refs)]


def _bank_appended(trigger: dict, db: sqlite3.Connection) -> list[dict]:
    filename = trigger.get("payload", {}).get("file")
    if not filename:
        return [_document_event(trigger, "bank_file", "needs_review")]
    filename = Path(str(filename)).name
    path = data_dir() / "bank" / filename
    if not path.is_file():
        return [_document_event(trigger, "bank_file", "needs_review", {"file": filename})]
    contents = path.read_bytes()
    row = db.execute("SELECT offset FROM bank_offsets WHERE file=?", (filename,)).fetchone()
    offset = int(row[0]) if row else 0
    if offset > len(contents):
        offset = 0
    pending = contents[offset:].decode("utf-8", errors="replace")
    output = []
    for line in pending.splitlines():
        if not line.startswith("16,"):
            continue
        parts = line.rstrip("/\r\n").split(",")
        if len(parts) < 7:
            output.append(_document_event(trigger, "bank_transaction", "needs_review",
                                          {"file": filename, "record": line}))
            continue
        cents, txn_id, reference, counterparty = parts[2], parts[4], parts[5], parts[6]
        try:
            paid_amount = round(int(cents) / 100, 2)
        except ValueError:
            output.append(_document_event(trigger, "bank_transaction", "needs_review",
                                          {"file": filename, "record": line}))
            continue
        invoice = _read_json(db.execute("SELECT json FROM shipper_invoices WHERE invoice_id=?",
                                        (reference,)).fetchone())
        if not invoice:
            output.append(_document_event(trigger, "bank_transaction", "needs_review",
                                          {"file": filename, "reference": reference,
                                           "counterparty": counterparty, "paid_amount": paid_amount}))
            continue
        billed_amount = float(invoice.get("amount", 0))
        case_id = f"{invoice.get('case_id')}-AR"
        payload = {"case_id": case_id, "load_id": invoice.get("load_id"), "lane": invoice.get("lane"),
                   "pro": invoice.get("pro"), "bol": invoice.get("bol"), "po": invoice.get("po"),
                   "invoice_id": reference, "shipper": invoice.get("shipper"), "invoice_amount": billed_amount,
                   "paid_amount": paid_amount, "chase": paid_amount < billed_amount, "remittance": "ACH",
                   "bank_txn": txn_id, "deduction_reason": invoice.get("deduction_reason")}
        evidence_refs = list(invoice.get("_evidence_refs", []))
        if invoice.get("source_email"):
            evidence_refs.append(f"mail:{invoice['source_email']}")
        output.append(_event(trigger, "shipper_payment.received", payload, "P2", evidence_refs))
    db.execute("INSERT OR REPLACE INTO bank_offsets(file, offset) VALUES (?,?)", (filename, len(contents)))
    return output


async def handle(ev: dict, db: sqlite3.Connection) -> list[dict]:
    init_db(db)
    event_type, payload = ev.get("type"), ev.get("payload", {})
    if event_type == "load.tendered":
        load_id = payload.get("load_id")
        if load_id:
            load = dict(payload)
            load.setdefault("gps_dwell_min", None)
            db.execute("INSERT OR REPLACE INTO loads(load_id, json) VALUES (?,?)", (load_id, _dump(load)))
    elif event_type == "gps.ping" and payload.get("milestone") == "delivery.departed":
        load = _load(db, payload.get("load_id"))
        if load:
            load["gps_dwell_min"] = payload.get("dwell_min")
            db.execute("UPDATE loads SET json=? WHERE load_id=?", (_dump(load), load["load_id"]))
    elif event_type == "shipper_invoice.issued":
        invoice_id = payload.get("invoice_id")
        if invoice_id:
            invoice = {**payload, "_evidence_refs": ev.get("evidence_refs", [])}
            db.execute("INSERT OR REPLACE INTO shipper_invoices(invoice_id, json) VALUES (?,?)",
                       (invoice_id, _dump(invoice)))
    elif event_type == "email.received":
        mail_id = payload.get("mail_id")
        if not mail_id:
            return [_document_event(ev, "other", "needs_review")]
        path = data_dir() / "mail" / f"{Path(str(mail_id)).name}.eml"
        try:
            message = read_email(path)
        except OSError:
            log.warning("email file missing for %s", mail_id)
            return [_document_event(ev, "other", "needs_review")]
        kind = classify(message)
        if kind == "invoice_email":
            return await _invoice_email(ev, message, db)
        if kind == "bank_change_request":
            return _bank_change(ev, message, db)
        if kind == "remittance_advice":
            return _remittance_advice(ev, message, db)
    elif event_type == "bank.file_appended":
        return _bank_appended(ev, db)
    return []


async def main() -> None:
    r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    db = connect()
    try:
        try:
            await r.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        while True:
            response = await r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=10, block=2000)
            for _stream, entries in response or []:
                for entry_id, values in entries:
                    raw = values.get(b"json") or values.get("json")
                    try:
                        events = await handle(json.loads(raw), db)
                        for event in events:
                            await r.xadd(STREAM, {"json": json.dumps(event, default=str)},
                                         maxlen=20_000, approximate=True)
                    except Exception:
                        log.exception("ingest failed for %s", entry_id)
                        continue
                    await r.xack(STREAM, GROUP, entry_id)
    finally:
        db.close()
        await r.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
