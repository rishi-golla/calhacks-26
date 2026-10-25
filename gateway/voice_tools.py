"""Case-scoped, read-only tools for the hosted Cartesia conversational agent."""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import Header, HTTPException, Request

from common.policy import check
from common.runtime import projection_target
from common.runtime_store import RuntimeStore, projection_row_id
from common.source_repository import SourceRepository, SourceUnavailable
from common.spacetime_client import SpacetimeClient
from common.tool_loop import audit_summary, public_value
from common.voice import valid_call_token

READ_ONLY_TOOLS = {"get_load_facts", "get_payment_status", "get_invoice_status", "get_case_summary", "get_document_status"}
log = logging.getLogger(__name__)


async def ledger_status(context: dict, *, payments: bool) -> dict:
    """Return only conversational financial fields, never whole database rows."""
    case_id = context["case_id"].replace("'", "''")
    result = {"invoice_reference": context.get("invoice_reference"),
              "source_record": context.get("invoice", {}), "ledger_available": False,
              "current_payment_status": "unknown",
              "note": "Source figures describe the scenario; payment proposals do not establish money movement."}
    db = SpacetimeClient(actor="voice_desk")
    if db.disabled:
        return result
    try:
        invoices = await db.query_rows(f"SELECT * FROM invoice WHERE case_id = '{case_id}'")
        invoice = next((row for row in invoices if row.get("id") == context.get("invoice_id")), None)
        result.update(ledger_available=True, invoice_record_present=invoice is not None)
        if invoice:
            result["ledger_invoice"] = {key: invoice[key] for key in
                ("amount", "contract_amount", "paid_amount", "status", "issued_at") if key in invoice}
            try:
                amount, paid = Decimal(str(invoice["amount"])), Decimal(str(invoice["paid_amount"]))
                if amount.is_finite() and paid.is_finite() and amount > 0 and paid >= 0:
                    result["current_payment_status"] = "paid" if paid >= amount else "unpaid" if paid == 0 else "partially_paid"
            except (KeyError, InvalidOperation):
                pass
        if payments:
            proposals = await db.query_rows(f"SELECT * FROM payment_proposal WHERE case_id = '{case_id}'")
            result["payment_proposals"] = [{key: row[key] for key in ("amount", "status") if key in row}
                for row in proposals if row.get("invoice_id") == context.get("invoice_id")][:20]
    except Exception:  # noqa: BLE001 -- a failed lookup must not imply an unpaid or paid invoice
        result.update(ledger_available=False, current_payment_status="unknown",
                      note="The live ledger is unavailable. Do not assert current payment status.")
        result.pop("ledger_invoice", None)
        result.pop("payment_proposals", None)
    return result


async def audit_lookup(req: Request, row: dict, name: str, result: dict, started: float) -> None:
    call_id = str(uuid.uuid4())  # Matches the PostgreSQL outbox's 36-character ID column.
    model = "gateway/lookup-probe" if json.loads(row["payload"]).get("lookup_probe") else "cartesia/hosted-agent"
    event = {"version": 1, "tool_call_id": call_id, "tool_name": name, "status": "completed",
             "arguments": {"case_id": row["case_id"]}, "result": public_value(result),
             "duration_ms": round((time.monotonic() - started) * 1000), "model": model}
    payload = {"write_id": call_id, "task_id": row["task_id"], "case_id": row["case_id"],
               "kind": "tool_call", "reason_code": "tool_completed", "summary": audit_summary(event),
               "model_used": model, "evidence_refs": [f"call://{row['id']}"], "policy_ids": []}
    if check("append_audit", "voice_desk", payload).result != "allow":
        return
    runtime = RuntimeStore(engine=req.app.state.calls.engine)
    # The Voice service's normal projection worker retries this durable event.
    target = projection_target()
    await runtime.queue_projection("voice_desk", {**payload, "_operation": "append_audit", "_target": target})
    try:
        await SpacetimeClient(actor="voice_desk").mutate("append_audit", payload)
        await runtime.acknowledge_projection(projection_row_id(call_id, target))
    except Exception:  # noqa: BLE001 -- dashboard delivery does not interrupt a read-only call answer
        log.warning("voice lookup audit queued for delivery")


async def lookup(name: str, req: Request, x_call_token: str = Header(default="")):
    if name not in READ_ONLY_TOOLS:
        raise HTTPException(404, "unknown read-only voice tool")
    if not isinstance(x_call_token, str) or not x_call_token:
        raise HTTPException(401, "call capability required")
    raw = await req.body()
    if len(raw) > 2048:
        raise HTTPException(413, "request body exceeds limit")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "JSON object required") from None
    if (not isinstance(body, dict) or set(body) != {"attempt_id", "case_id"}
            or any(not isinstance(body[key], str) or not 0 < len(body[key]) <= 200 for key in body)):
        raise HTTPException(400, "Only the call-bound attempt_id and case_id are accepted")
    row = await req.app.state.calls.get(body["attempt_id"])
    if (row is None or not valid_call_token(row, x_call_token)
            or row["status"] not in {"dialing", "ringing", "uncertain"}):
        raise HTTPException(401, "invalid or expired call capability")
    if body["case_id"] != row["case_id"]:
        raise HTTPException(403, "call belongs to a different case")
    from common.envelope import TaskEnvelope
    from common.voice_context import build_voice_context

    started = time.monotonic()
    repository = SourceRepository()
    try:
        source = await repository.load(TaskEnvelope(task_type="place_call", case_id=row["case_id"]))
        context = await build_voice_context(source, repository, purpose=json.loads(row["payload"]).get("purpose", "verification"))
    except (SourceUnavailable, ValueError, TypeError, KeyError):
        raise HTTPException(503, "Case details are unavailable; do not invent an answer") from None
    if name == "get_case_summary":
        result = context
    elif name == "get_load_facts":
        result = {key: context[key] for key in ("load_reference", "bill_of_lading_reference", "scenario",
                  "parties", "route", "stops", "cargo", "evidence_refs") if key in context}
    elif name == "get_document_status":
        result = {key: context[key] for key in ("load_reference", "documents", "contacts", "evidence_refs") if key in context}
    else:
        result = await ledger_status(context, payments=name == "get_payment_status")
    response = {"tool": name, "result": result, "synthetic": context.get("synthetic", False),
                "as_of": datetime.now(UTC).isoformat()}
    await audit_lookup(req, row, name, response, started)
    return response
