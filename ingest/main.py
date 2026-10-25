"""Turn raw simulator emails and bank files into the case-opening events consumed by the router."""
import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from email.utils import parseaddr
from pathlib import Path

import redis.asyncio as redis

from ingest import mirror as spacetime_mirror
from ingest import source_export
from ingest.documents import (
    classify,
    extract_detention_log_rules,
    extract_invoice,
    extract_lumper_receipt_rules,
    extract_pod_rules,
    read_email,
)
from integrations import directory
from sim.events import iso_z

log = logging.getLogger("ingest")
RAW_STREAM = os.getenv("RAW_STREAM", "raw")
EVENTS_STREAM = os.getenv("EVENTS_STREAM", "events")
DOCUMENTS_STREAM = os.getenv("DOCUMENTS_STREAM", "documents")
GROUP = "ingest"
CONSUMER = "ingest-1"
ACCESSORIAL_TERMS = ("detention", "lumper", "layover", "stop-off", "stop off", "tonu", "driver assist")
# Events ingest derives that open a case for the router; a demo load's one jumps to P0 and carries its outreach target.
CASE_EVENTS = {"carrier_invoice.received", "bank_change.requested", "shipper_payment.received"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS loads (load_id TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoices (invoice_id TEXT PRIMARY KEY, load_id TEXT, invoice_number TEXT, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shipper_invoices (invoice_id TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_offsets (file TEXT PRIMARY KEY, offset INT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_txns (txn_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS bank_changes (case_id TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deposits (txn_id TEXT PRIMARY KEY, invoice_id TEXT, json TEXT NOT NULL);
"""


def data_dir() -> Path:
    return Path(os.getenv("SIM_DATA_DIR", "data"))


def init_db(db: sqlite3.Connection) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)


def connect() -> sqlite3.Connection:
    path = data_dir() / "ingest.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
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


def _load_by_bol(db: sqlite3.Connection, bol: str | None) -> dict | None:
    if bol:
        for row in db.execute("SELECT json FROM loads"):
            candidate = json.loads(row[0])
            if candidate.get("bol") == bol:
                return candidate
    return None


def _record_documents(db: sqlite3.Connection, load: dict | None, documents: list[dict]) -> None:
    """Remember which source documents arrived for a load; the latest reading of a ref wins."""
    if not load or not documents:
        return
    merged = {item["ref"]: item for item in load.get("documents", [])}
    for document in documents:
        merged[document["ref"]] = {**merged.get(document["ref"], {}), **document, "load_id": load["load_id"]}
    load["documents"] = list(merged.values())
    db.execute("UPDATE loads SET json=? WHERE load_id=?", (_dump(load), load["load_id"]))


def _attachment_ref(item: dict) -> str:
    return f"doc:{Path(item.get('filename', '')).stem}"


def _find_load(db: sqlite3.Connection, fields: dict) -> tuple[dict | None, str | None]:
    load_id, bol = fields.get("load_id"), fields.get("bol")
    by_id = _load(db, load_id) if load_id else None
    by_bol = _load_by_bol(db, bol) if bol else None
    if load_id and bol:
        if (by_id is None) != (by_bol is None) \
                or (by_id and by_bol and by_id["load_id"] != by_bol["load_id"]):
            return None, "load_id/bol"
        return by_id, None
    return (by_id if load_id else by_bol), None


def _event(trigger: dict, event_type: str, payload: dict, priority: str = "P2",
           evidence_refs: list[str] | None = None) -> dict:
    wall_time = iso_z()
    return {"event_id": f"EVT-{uuid.uuid4().hex[:10]}", "type": event_type,
            "ts": wall_time, "sim_ts": trigger.get("sim_ts") or wall_time,
            "case_hint": payload.get("case_id") or trigger.get("case_hint"),
            "priority": priority, "source": "sim", "payload": payload,
            "evidence_refs": evidence_refs or []}


def _document_event(trigger: dict, kind: str, status: str, fields: dict | None = None,
                    confidence: dict | None = None, method: str = "rules", load: dict | None = None,
                    case_id: str | None = None, evidence_refs: list[str] | None = None) -> dict:
    payload = {"status": status, "kind": kind, "fields": fields or {}, "confidence": confidence or {},
               "method": method, "matched_load_id": load.get("load_id") if load else None,
               "case_id": case_id or (f"CASE-{load['load_id']}" if load else None)}
    return _event(trigger, "document.extracted", payload, evidence_refs=evidence_refs)


def _evidence_refs(message: dict) -> list[str]:
    refs = []
    for item in message.get("attachments", []):
        filename = item if isinstance(item, str) else item.get("filename", "")
        if filename:
            refs.append(f"doc:{Path(filename).stem}")
    if message.get("mail_id"):
        refs.append(f"mail:{message['mail_id']}")
    return refs


def _same_identifier(value: str | None, expected: str | None) -> bool:
    return bool(value and expected and str(value).strip().casefold() == str(expected).strip().casefold())


def _email_needs_review(trigger: dict, message: dict) -> dict:
    attachments = message.get("attachments", [])
    filenames = [item if isinstance(item, str) else item.get("filename", "") for item in attachments]
    filenames = [name for name in filenames if name and name.lower().endswith(".pdf")]
    fields = {"subject": message.get("subject", ""), "attachments": filenames}
    return _document_event(trigger, "other", "needs_review", fields, evidence_refs=_evidence_refs(message))


def _email_fields(trigger: dict, message: dict) -> dict:
    refs = _evidence_refs(message)
    attachments = list(message.get("attachments", []))
    invoice = next((item for item in attachments
                    if isinstance(item, dict) and classify(item.get("text", "")) == "carrier_invoice"), None)
    if invoice:
        attachments.remove(invoice)
        attachments.insert(0, invoice)
    text_parts = [message.get("body") or trigger.get("payload", {}).get("snippet") or ""]
    text_parts.extend(
        f"--- attachment: {item if isinstance(item, str) else item.get('filename', '')} ---\n"
        f"{'' if isinstance(item, str) else item.get('text', '')}"
        for item in attachments)
    payload = {"text": "\n\n".join(part for part in text_parts if part)[:12000],
               "from": message.get("from") or "",
               "to": message.get("to") or "",
               "subject": message.get("subject") or "",
               "date": trigger.get("payload", {}).get("sent_at") or message.get("date"),
               "evidence_refs": refs}
    return payload


def _email_event(trigger: dict, message: dict) -> dict:
    refs = _evidence_refs(message)
    payload = {**trigger.get("payload", {}), **_email_fields(trigger, message)}
    return _event(trigger, "email.received", payload, evidence_refs=refs)


async def _invoice_email(trigger: dict, message: dict, db: sqlite3.Connection) -> list[dict]:
    attachments = message.get("attachments", [])
    invoice_doc = next((item for item in attachments if classify(item.get("text", "")) == "carrier_invoice"), None)
    refs = _evidence_refs(message)
    if not invoice_doc:
        return [_document_event(trigger, "carrier_invoice", "needs_review", evidence_refs=refs),
                _email_event(trigger, message)]
    extracted = await extract_invoice(invoice_doc.get("text", ""), str(trigger.get("case_hint") or "unmatched"))
    fields = extracted["fields"]
    load, load_mismatch = _find_load(db, fields)
    if load_mismatch:
        mismatch_fields = {**fields, "mismatch": load_mismatch}
        return [_document_event(trigger, "carrier_invoice", "needs_review", mismatch_fields,
                                extracted["confidence"], extracted["method"], evidence_refs=refs),
                _email_event(trigger, message)]
    if not load:
        return [_document_event(trigger, "carrier_invoice", "needs_review", fields, extracted["confidence"],
                                extracted["method"], load, evidence_refs=refs), _email_event(trigger, message)]

    carrier_fields = load.get("carrier") or {}
    load_usdot = carrier_fields.get("usdot")
    line_items = fields.get("line_items") or []
    if not line_items:
        mismatch_fields = {**fields, "mismatch": "no_line_items"}
        return [_document_event(trigger, "carrier_invoice", "needs_review", mismatch_fields,
                                extracted["confidence"], extracted["method"], load, evidence_refs=refs),
                _email_event(trigger, message)]
    if fields.get("total") is None or not fields.get("invoice_number"):
        return [_document_event(trigger, "carrier_invoice", "needs_review", fields, extracted["confidence"],
                                extracted["method"], load, evidence_refs=refs), _email_event(trigger, message)]

    usdot = load_usdot or fields.get("usdot")
    carrier = directory.by_usdot(str(load_usdot)) if load_usdot else None
    carrier = carrier or carrier_fields
    invoice_carrier_name = fields.get("carrier_name")
    remit_to_name = fields.get("remit_to_name")
    ingest_flags = []
    if fields.get("usdot") and load_usdot and not _same_identifier(fields["usdot"], load_usdot):
        ingest_flags.append("usdot_mismatch")
    if invoice_carrier_name and carrier_fields.get("name") \
            and not _same_identifier(invoice_carrier_name, carrier_fields["name"]):
        ingest_flags.append("carrier_mismatch")
    if remit_to_name and carrier_fields.get("name") and not _same_identifier(remit_to_name, carrier_fields["name"]):
        ingest_flags.append("remit_to_mismatch")
    load_id = load["load_id"]
    invoice_number = fields["invoice_number"]
    invoice_id = f"INV-{usdot or ''}-{invoice_number}"
    prior = _read_json(db.execute("SELECT json FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone())
    if prior and prior.get("_trigger_event_id") != trigger.get("event_id"):
        # The same carrier invoice number again (a re-sent email): one settlement, never a second payment.
        prior["_resubmissions"] = sorted({*prior.get("_resubmissions", []), *refs})
        db.execute("UPDATE invoices SET json=? WHERE invoice_id=?", (_dump(prior), invoice_id))
        duplicate_fields = {**fields, "mismatch": "duplicate_invoice", "duplicate_of": invoice_id,
                            "duplicate_case_id": prior["case_id"],
                            "amount_changed": prior.get("invoice_amount") != fields["total"]}
        return [_document_event(trigger, "carrier_invoice", "needs_review", duplicate_fields,
                                extracted["confidence"], extracted["method"], load, prior["case_id"], refs)]
    existing = db.execute("SELECT invoice_number FROM invoices WHERE load_id=?", (load_id,)).fetchall()
    case_id = (load.get("case_id") or f"CASE-{load_id}") + (
        "-R" if any(row[0] != invoice_number for row in existing) else "")

    unmatched_attachments = []
    received = [{"kind": "carrier_invoice", "ref": _attachment_ref(invoice_doc)}]
    pod = None
    pod_signed = False
    detention_log = None
    has_detention_log = False
    has_lumper_receipt = False
    for item in attachments:
        attachment_kind = classify(item.get("text", ""))
        filename = item.get("filename", "")
        text = item.get("text", "")
        if attachment_kind == "pod":
            candidate = extract_pod_rules(text)
            if _same_identifier(candidate.get("load_id"), load_id) \
                    or _same_identifier(candidate.get("bol"), load.get("bol")):
                pod = candidate
                pod_signed = candidate["signed"]
                received.append({"kind": "pod", "ref": _attachment_ref(item), "signed": pod_signed})
            else:
                unmatched_attachments.append(filename)
        elif attachment_kind == "detention_log":
            candidate = extract_detention_log_rules(text)
            if _same_identifier(candidate.get("load_id"), load_id) \
                    or _same_identifier(candidate.get("bol"), load.get("bol")):
                detention_log = candidate
                has_detention_log = True
                received.append({"kind": "detention_log", "ref": _attachment_ref(item)})
            else:
                unmatched_attachments.append(filename)
        elif attachment_kind == "lumper_receipt":
            candidate = extract_lumper_receipt_rules(text)
            if _same_identifier(candidate.get("po"), load.get("po")):
                has_lumper_receipt = True
                received.append({"kind": "lumper_receipt", "ref": _attachment_ref(item)})
            else:
                unmatched_attachments.append(filename)
    has_receipt = has_detention_log or has_lumper_receipt
    has_accessorial_line = any(any(term in str(item.get("desc", "")).casefold()
                                   for term in ACCESSORIAL_TERMS) for item in line_items)
    receipt_attached = True if has_receipt else False if has_accessorial_line else None
    gps_dwell_min = load.get("gps_dwell_min")
    free_time_min = load.get("free_time_min")
    email_fields = _email_fields(trigger, message)
    shipper = load.get("shipper") or {}
    payload = {
        "case_id": case_id, "load_id": load_id, "lane": load.get("lane"), "pro": load.get("pro"),
        "bol": load.get("bol"), "po": load.get("po"), "invoice_id": invoice_id,
        "invoice_number": invoice_number, "carrier": carrier, "shipper": load.get("shipper"),
        "carrier_id": carrier_fields.get("id"), "carrier_name": carrier_fields.get("name"),
        "shipper_id": shipper.get("id"), "shipper_name": shipper.get("name"),
        "invoice_date": fields.get("invoice_date"),
        "contract_rate": load.get("contract_rate"), "invoice_amount": fields["total"], "line_items": line_items,
        "invoice_carrier_usdot": fields.get("usdot"), "ingest_flags": ingest_flags,
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
        "pod_signed": pod_signed, "remit_to": {"name": fields.get("remit_to_name")},
        "source_email": message.get("mail_id"), **(load.get("operator_overrides") or {}),
        "extraction": extracted, **email_fields,
    }
    document_fields = {**fields, "attachments": {"pod": pod, "detention_log": detention_log,
                                                   "lumper_receipt": has_lumper_receipt},
                       "unmatched_attachments": unmatched_attachments}
    if ingest_flags:
        document_fields["ingest_flags"] = ingest_flags
        document_fields["mismatch"] = ingest_flags[0].removesuffix("_mismatch")
    document = _document_event(trigger, "carrier_invoice", "needs_review" if ingest_flags else "extracted",
                               document_fields, extracted["confidence"],
                               extracted["method"], load, case_id, refs)
    priority = "P1" if has_accessorial_line or (
        payload["contract_rate"] is not None and payload["invoice_amount"] > payload["contract_rate"]) else "P2"
    invoice_event = _event(trigger, "carrier_invoice.received", payload, priority, refs)
    db.execute("INSERT OR REPLACE INTO invoices(invoice_id, load_id, invoice_number, json) VALUES (?,?,?,?)",
               (invoice_id, load_id, invoice_number, _dump({**payload, "_trigger_event_id": trigger.get("event_id"),
                      "_resubmissions": (prior or {}).get("_resubmissions", [])})))
    _record_documents(db, load, received)
    return [document, invoice_event]


def _bank_change(trigger: dict, message: dict, db: sqlite3.Connection) -> list[dict]:
    refs = _evidence_refs(message)
    subject_match = re.search(r"remittance details\s*-\s*(.+)$", message.get("subject", ""), re.IGNORECASE)
    number_match = re.search(r"\binvoice\s+(\S+),", message.get("body", ""), re.IGNORECASE)
    account_match = re.search(r"\bAccount(?:\s+number)?\s*:\s*(\d{10,12})\b", message.get("body", ""), re.IGNORECASE)
    account_last4_match = re.search(r"\bAccount\s+\*{4}(\d{4})", message.get("body", ""), re.IGNORECASE)
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
    invoice_carrier = invoice.get("carrier") if invoice else carrier
    invoice_carrier = invoice_carrier or {}
    remit_name = ((invoice or {}).get("remit_to") or {}).get("name")
    carrier_name = (carrier or {}).get("name") or invoice_carrier.get("name") or carrier_name
    requested_via = ("invoice_remit_to" if remit_name and
                     remit_name.strip().casefold() != carrier_name.strip().casefold() else "email")
    load_id = invoice.get("load_id") if invoice else None
    case_id = trigger.get("case_hint")
    if load_id:
        case_id = f"{(_load(db, load_id) or {}).get('case_id') or f'CASE-{load_id}'}-BANK"
    sender_address = parseaddr(str(message.get("from") or ""))[1].strip()
    registered_email = str((carrier or {}).get("email") or "").strip()
    sender_domain = sender_address.rpartition("@")[2]
    registered_domain = registered_email.rpartition("@")[2]
    sender_matches_registered = bool(
        sender_address and registered_email
        and sender_address.casefold() == registered_email.casefold()
        and sender_domain.casefold() == registered_domain.casefold()
    )
    account_number = account_match.group(1) if account_match else None
    account_last4 = account_number[-4:] if account_number else (
        account_last4_match.group(1) if account_last4_match else None)
    routing_match = re.search(r"\bRouting(?:\s+number)?\s*:\s*(\d{9})\b",
                              message.get("body", ""), re.IGNORECASE)
    routing_last4_match = re.search(r"\bRouting\s+\*{4}(\d{4})", message.get("body", ""), re.IGNORECASE)
    routing_number = routing_match.group(1) if routing_match else None
    next_payment_match = re.search(
        r"\b(?:next payment(?: amount)?|scheduled payment(?: amount)?|payment amount)"
        r"\s*(?:is|:)?\s*\$?([\d,]+(?:\.\d{2})?)",
        message.get("body", ""), re.IGNORECASE)
    next_payment = float(next_payment_match.group(1).replace(",", "")) if next_payment_match else (
        invoice.get("invoice_amount") if invoice else None)
    urgency_match = re.search(r"\b(urgent(?:ly)?|immediately|today|as soon as possible)\b",
                              f"{message.get('subject', '')} {message.get('body', '')}", re.IGNORECASE)
    effective_match = re.search(r"\bEffective\s+(?:date\s*:?\s*)?(\d{4}-\d{2}-\d{2})\b",
                                message.get("body", ""), re.IGNORECASE)
    effective_date = effective_match.group(1) if effective_match else (
        trigger.get("payload", {}).get("sent_at") if re.search(r"\beffective immediately\b",
                                                               message.get("body", ""), re.IGNORECASE) else None)
    email_fields = _email_fields(trigger, message)
    payload = {"case_id": case_id, "load_id": load_id, "lane": invoice.get("lane") if invoice else None,
               "pro": invoice.get("pro") if invoice else None,
               "bol": invoice.get("bol") if invoice else None, "po": invoice.get("po") if invoice else None,
               "carrier": invoice_carrier or None,
               "invoice_id": invoice.get("invoice_id") if invoice else None, "requested_via": requested_via,
               "from": message.get("from"), "registered_email": registered_email,
               "sender_matches_registered": sender_matches_registered,
               "new_payee": remit_name or carrier_name, "new_account_last4": account_last4,
               "carrier_id": (carrier or {}).get("id"), "carrier_name": carrier_name or None,
               "requested_by": sender_address, "channel": "email", "effective_date": effective_date,
               "new_routing": routing_number, "new_account": account_number,
               "next_payment_amount": next_payment,
               "urgency_language": urgency_match.group(0) if urgency_match else None,
               "source_email": message.get("mail_id"), **email_fields}
    if routing_number:
        payload["new_routing_last4"] = routing_number[-4:]
    fields = {"carrier": carrier_name or None, "invoice_number": number_match.group(1) if number_match else None,
              "new_account_last4": account_last4,
              "new_routing_last4": routing_number[-4:] if routing_number else (
                  routing_last4_match.group(1) if routing_last4_match else None),
              "requested_via": requested_via,
              "sender_matches_registered": sender_matches_registered}
    if case_id:
        db.execute("INSERT OR REPLACE INTO bank_changes(case_id, json) VALUES (?,?)",
                   (case_id, _dump({**payload, "sim_ts": trigger.get("sim_ts")})))
    status = "extracted_unverified_sender" if not sender_matches_registered else (
        "extracted" if carrier and invoice and account_last4 else "needs_review")
    document = _document_event(
        trigger, "bank_change_request", status, fields,
        {key: 0.95 if value else 0.0 for key, value in fields.items()},
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
                                 "deduction_reason": 0.95 if reason else 0.0}, evidence_refs=refs),
                _email_event(trigger, message)]
    invoice["deduction_reason"] = reason
    invoice["source_email"] = message.get("mail_id")
    invoice["remittance_text"] = message.get("body") or ""
    db.execute("UPDATE shipper_invoices SET json=? WHERE invoice_id=?", (_dump(invoice), invoice_id))
    fields = {"invoice_id": invoice_id, "deduction_reason": reason}
    case_id = f"{invoice.get('case_id')}-AR"
    return [_document_event(trigger, "remittance_advice", "extracted", fields,
                            {"invoice_id": 0.95, "deduction_reason": 0.95 if reason else 0.0},
                            load={"load_id": invoice.get("load_id")}, case_id=case_id, evidence_refs=refs)]


def _bank_appended(trigger: dict, db: sqlite3.Connection) -> list[dict]:
    payload = trigger.get("payload", {})
    record = payload.get("record")
    filename = Path(str(payload["file"])).name if payload.get("file") else None
    if record is not None:
        pending = [str(record)]
    else:
        if not filename:
            return [_document_event(trigger, "bank_file", "needs_review")]
        path = data_dir() / "bank" / filename
        if not path.is_file():
            return [_document_event(trigger, "bank_file", "needs_review", {"file": filename})]
        contents = path.read_bytes()
        row = db.execute("SELECT offset FROM bank_offsets WHERE file=?", (filename,)).fetchone()
        offset = int(row[0]) if row else 0
        if offset > len(contents):
            offset = 0
        pending = contents[offset:].decode("utf-8", errors="replace").splitlines()
    output = []
    for line in pending:
        if not line.startswith("16,"):
            if record is not None:
                output.append(_event(trigger, "bank.event", {
                    "amount": None, "direction": "credit", "counterparty": None, "reference": None,
                    "posted_at": trigger.get("sim_ts"), "txn_id": None, "file": filename, "record": line,
                }))
            continue
        parts = line.rstrip("/\r\n").split(",")
        txn_id = parts[4] if len(parts) > 4 else None
        if txn_id and db.execute("SELECT 1 FROM bank_txns WHERE txn_id=?", (txn_id,)).fetchone():
            continue
        cents = parts[2] if len(parts) > 2 else None
        reference = parts[5] if len(parts) > 5 else None
        counterparty = parts[6].rstrip("/") if len(parts) > 6 else None
        try:
            paid_amount = round(int(cents) / 100, 2) if cents is not None else None
        except ValueError:
            paid_amount = None
        direction = "debit" if len(parts) > 1 and parts[1] == "455" else "credit"
        if len(parts) < 7 or paid_amount is None:
            output.append(_event(trigger, "bank.event", {
                "amount": paid_amount, "direction": direction, "counterparty": counterparty,
                "reference": reference, "posted_at": trigger.get("sim_ts"), "txn_id": txn_id,
                "file": filename, "record": line,
            }))
            if txn_id:
                db.execute("INSERT OR IGNORE INTO bank_txns(txn_id) VALUES (?)", (txn_id,))
            continue
        invoice = _read_json(db.execute("SELECT json FROM shipper_invoices WHERE invoice_id=?",
                                        (reference,)).fetchone())
        if txn_id and direction == "credit":
            deposit = {"id": txn_id, "invoice_id": reference, "amount": paid_amount,
                       "shipper_id": ((invoice or {}).get("shipper") or {}).get("id"),
                       "counterparty": counterparty, "posted_at": trigger.get("sim_ts"), "file": filename}
            db.execute("INSERT OR IGNORE INTO deposits(txn_id, invoice_id, json) VALUES (?,?,?)",
                       (txn_id, reference, _dump(deposit)))
        if not invoice:
            output.append(_event(trigger, "bank.event", {
                "amount": paid_amount, "direction": direction, "counterparty": counterparty,
                "reference": reference, "posted_at": trigger.get("sim_ts"), "txn_id": txn_id,
                "file": filename, "record": line,
            }))
            if txn_id:
                db.execute("INSERT OR IGNORE INTO bank_txns(txn_id) VALUES (?)", (txn_id,))
            continue
        billed_amount = float(invoice.get("amount", 0))
        case_id = f"{invoice.get('case_id')}-AR"
        payload = {"case_id": case_id, "load_id": invoice.get("load_id"), "lane": invoice.get("lane"),
                   "pro": invoice.get("pro"), "bol": invoice.get("bol"), "po": invoice.get("po"),
                   "invoice_id": reference, "shipper": invoice.get("shipper"), "invoice_amount": billed_amount,
                   "paid_amount": paid_amount, "chase": paid_amount < billed_amount, "remittance": "ACH",
                   "bank_txn": txn_id, "deduction_reason": invoice.get("deduction_reason"),
                   "payment_id": txn_id, "shipper_id": (invoice.get("shipper") or {}).get("id"),
                   "shipper_name": (invoice.get("shipper") or {}).get("name"),
                   "amount": paid_amount, "remittance_ref": reference,
                   "invoices_applied": [{"invoice_id": reference, "amount": paid_amount}],
                   "posted_at": trigger.get("sim_ts"), "direction": direction,
                   "text": f"{line}\n{invoice.get('remittance_text', '')}".rstrip(),
                   "evidence_refs": []}
        evidence_refs = list(invoice.get("_evidence_refs", []))
        if invoice.get("source_email"):
            evidence_refs.append(f"mail:{invoice['source_email']}")
        payload["evidence_refs"] = evidence_refs
        output.append(_event(trigger, "shipper_payment.received", payload, "P2", evidence_refs))
        if txn_id:
            db.execute("INSERT OR IGNORE INTO bank_txns(txn_id) VALUES (?)", (txn_id,))
    if record is None:
        db.execute("INSERT OR REPLACE INTO bank_offsets(file, offset) VALUES (?,?)", (filename, len(contents)))
    return output


async def _handle(ev: dict, db: sqlite3.Connection) -> list[dict]:
    init_db(db)
    event_type, payload = ev.get("type"), ev.get("payload", {})
    if event_type == "load.tendered":
        load_id = payload.get("load_id")
        if load_id:
            load = dict(payload)
            load.setdefault("gps_dwell_min", None)
            # The tender carries the rate confirmation; keep documents already received on a re-tender.
            load["documents"] = (_load(db, load_id) or {}).get("documents", [])
            db.execute("INSERT OR REPLACE INTO loads(load_id, json) VALUES (?,?)", (load_id, _dump(load)))
            _record_documents(db, load, [{"kind": "rate_confirmation", "ref": ref}
                                         for ref in ev.get("evidence_refs", [])[:1]])
        return [ev]
    elif event_type == "gps.ping":
        if payload.get("milestone") == "delivery.departed":
            load = _load(db, payload.get("load_id"))
            if load:
                load["gps_dwell_min"] = payload.get("dwell_min")
                db.execute("UPDATE loads SET json=? WHERE load_id=?", (_dump(load), load["load_id"]))
        return [ev]
    elif event_type == "pod.received":
        if payload.get("pod_ref"):
            _record_documents(db, _load(db, payload.get("load_id")), [
                {"kind": "pod", "ref": payload["pod_ref"], "signed": payload.get("signed") is True}])
        return [ev]
    elif event_type in {"load.dispatched", "load.delivered"}:
        return [ev]
    elif event_type == "load.status":
        if payload.get("status") == "loaded":
            _record_documents(db, _load(db, payload.get("load_id")), [
                {"kind": "bill_of_lading", "ref": ref} for ref in ev.get("evidence_refs", [])[:1]])
        return []
    elif event_type == "scenario.injected":
        return [{**ev, "payload": {**payload, "generated_type": "load.tendered"}}]
    elif event_type == "shipper_invoice.issued":
        invoice_id = payload.get("invoice_id")
        if invoice_id:
            invoice = {**payload, "_evidence_refs": ev.get("evidence_refs", []), "_issued_at": ev.get("sim_ts")}
            db.execute("INSERT OR REPLACE INTO shipper_invoices(invoice_id, json) VALUES (?,?)",
                       (invoice_id, _dump(invoice)))
        return []
    elif event_type == "email.received":
        mail_id = payload.get("mail_id")
        if not mail_id:
            return [_email_needs_review(ev, payload), _email_event(ev, payload)]
        path = data_dir() / "mail" / f"{Path(str(mail_id)).name}.eml"
        try:
            message = read_email(path)
        except Exception:
            log.exception("email file unreadable for %s", mail_id)
            return [_email_needs_review(ev, payload), _email_event(ev, payload)]
        kind = classify(message)
        if kind == "invoice_email":
            return await _invoice_email(ev, message, db)
        if kind == "bank_change_request":
            return _bank_change(ev, message, db)
        if kind == "remittance_advice":
            return _remittance_advice(ev, message, db)
        return [_email_needs_review(ev, message), _email_event(ev, message)]
    elif event_type == "bank.event":
        return _bank_appended(ev, db)
    return []


def _stamp_demo(trigger: dict, derived: list[dict], db: sqlite3.Connection) -> None:
    """Keep a demo load's origin on what ingest derives from it (the simulator already stamps its own events).

    The case-opening event also gets P0 (PRD H5 reserves P0 capacity for the demo) and the routing context the
    Voice Desk reads from its subject. That context is routing, not evidence: it is added after the records are
    stored, so the source export never sees it. `demo_channel`, not `channel`: a bank-change payload's `channel`
    is how the request arrived."""
    for event in derived:
        if event.get("event_id") == trigger.get("event_id"):
            continue
        payload = event.get("payload") or {}
        demo = (_load(db, payload.get("load_id") or payload.get("matched_load_id")) or {}).get("demo")
        if not demo:
            continue
        event["source"] = "demo"
        if event.get("type") in CASE_EVENTS:
            event["priority"] = "P0"
            payload.update(demo_target=demo.get("demo_target"), demo_channel=demo.get("channel"),
                           injection_id=demo.get("injection_id"))


async def handle(ev: dict, db: sqlite3.Connection) -> list[dict]:
    derived = await _handle(ev, db)
    _stamp_demo(ev, derived, db)
    trigger_id = ev.get("event_id")
    if not trigger_id:
        trigger_id = hashlib.sha256(json.dumps(ev, sort_keys=True, default=str).encode()).hexdigest()[:16]
    trigger_id = str(trigger_id)
    if not trigger_id.startswith("EVT-"):
        trigger_id = f"EVT-{trigger_id}"
    index = 0
    for event in derived:
        if event.get("event_id") == ev.get("event_id"):
            continue
        event["event_id"] = f"{trigger_id}:{index}"
        index += 1
    return derived


async def _process_entry(r, db: sqlite3.Connection, entry_id, values: dict,
                         exporter: source_export.Exporter | None = None,
                         mirror: spacetime_mirror.Mirror | None = None) -> bool:
    try:
        raw = values.get(b"json") or values.get("json")
        events = await handle(json.loads(raw), db)
        if exporter is not None:
            exporter.changed()
        routed = mirror is not None and not mirror.disabled and spacetime_mirror.routed_case_ids(events)
        exported = exporter is not None and any(
            event.get("type") != "document.extracted" and (event.get("payload") or {}).get("case_id")
            for event in events)
        if routed:
            # Agents read the case from the export and write against its SpacetimeDB rows as soon as the router
            # admits it, so both land first. A failed mirror write leaves the entry unacked; its retry resends
            # the same write IDs, which the module treats as no-ops.
            dataset = source_export.build(db)
            await mirror.cases(dataset, events)
            if exporter is not None:
                exporter.flush(db, dataset)
        elif exported:
            # TMS progress on a case nobody is routed to yet only needs the export fresh within a bounded delay.
            exporter.flush_if_due(db)
        for event in events:
            stream = DOCUMENTS_STREAM if event.get("type") == "document.extracted" else EVENTS_STREAM
            await r.xadd(stream, {"json": json.dumps(event, default=str)},
                         maxlen=20_000, approximate=True)
        db.commit()
        await r.xack(RAW_STREAM, GROUP, entry_id)
        return True
    except Exception:
        db.rollback()
        log.exception("ingest failed for %s", entry_id)
        return False


class DirectoryWatch:
    """Mirror the directory at startup and again whenever `make world` rewrites world.json."""

    def __init__(self, mirror: spacetime_mirror.Mirror):
        self.mirror = mirror
        self.seen: float | None = None
        self.task: asyncio.Task | None = None

    def check(self) -> None:
        if self.mirror.disabled:
            return
        try:
            stamp = directory.path().stat().st_mtime
        except OSError:
            stamp = 0.0
        if stamp == self.seen:
            return
        self.seen = stamp
        if self.task:
            self.task.cancel()
        self.task = asyncio.create_task(spacetime_mirror.sync_directory(self.mirror))

    def stop(self) -> None:
        if self.task:
            self.task.cancel()


async def main() -> None:
    r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    db = connect()
    exporter = source_export.Exporter()
    mirror = spacetime_mirror.Mirror()
    if mirror.disabled:
        log.info("SpacetimeDB mirror off (SPACETIME_DISABLED=1)")
    watch = DirectoryWatch(mirror)
    try:
        try:
            await r.xgroup_create(RAW_STREAM, GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        while True:
            watch.check()
            while True:
                pending = await r.xreadgroup(GROUP, CONSUMER, {RAW_STREAM: "0"}, count=10)
                entries = [entry for _stream, batch in pending or [] for entry in batch]
                if not entries:
                    break
                failed = False
                for entry_id, values in entries:
                    if not await _process_entry(r, db, entry_id, values, exporter, mirror):
                        failed = True
                if failed:
                    await asyncio.sleep(1)
            # Catch up on state that opened no case (documents, deposits, TMS) before blocking for more.
            exporter.flush(db)
            response = await r.xreadgroup(GROUP, CONSUMER, {RAW_STREAM: ">"}, count=10, block=2000)
            for _stream, entries in response or []:
                for entry_id, values in entries:
                    await _process_entry(r, db, entry_id, values, exporter, mirror)
    finally:
        watch.stop()
        db.close()
        await r.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
