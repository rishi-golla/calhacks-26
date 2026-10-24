"""Authenticated voice evidence gateway with recoverable same-task A2A callbacks."""
import asyncio
import contextlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request

from common.envelope import TaskEnvelope
from common.providers import infer, mode
from common.spacetime_client import SpacetimeClient
from common.voice import (
    CallStore,
    VoiceError,
    fetch_call,
    finish_call,
    project_finalization,
    recover_uncertain_call,
    valid_call_token,
)

log = logging.getLogger(__name__)
READ_ONLY_TOOLS = {"get_load_facts", "get_payment_status", "get_invoice_status", "get_case_summary"}


def require_secret(actual: str, variable: str) -> None:
    expected = os.getenv(variable, "")
    if not actual or not expected or not hmac.compare_digest(actual, expected):
        raise HTTPException(401, "invalid credentials")


async def deliver_result(store: CallStore, row: dict) -> None:
    if not row["result"] or row["delivered"] == "1":
        return
    from common.a2a_transport import resume_task

    await resume_task("voice_desk", row["task_id"], row["case_id"], f"call:{row['id']}",
                      json.loads(row["result"]), idempotency_key=f"call:{row['id']}:complete")
    await store.set(row["id"], delivered="1")


async def reconcile_calls(store: CallStore) -> None:
    while True:
        try:
            await reconcile_once(store)
        except Exception as exc:  # noqa: BLE001 -- database unavailability must not terminate the worker
            log.warning("call polling deferred: %s", type(exc).__name__)
        await asyncio.sleep(5)


async def reconcile_once(store: CallStore) -> None:
    for row in await store.pending():
        try:
            if row["result"]:
                await deliver_result(store, row)
                continue
            frozen = json.loads(row["payload"]).get("finalization")
            if frozen:
                finished = await project_finalization(store, row, frozen)
            else:
                external = (await fetch_call(row["provider_id"]) if row["provider_id"] else
                            await recover_uncertain_call(store, row))
                if external and not row["provider_id"]:
                    row = await store.get(row["id"])
                finished = bool(external) and await finish_call(store, row, external)
            if finished:
                await deliver_result(store, await store.get(row["id"]))
        except Exception as exc:  # noqa: BLE001 -- each durable call retries independently
            log.warning("call reconciliation pending: %s", type(exc).__name__)


@asynccontextmanager
async def lifespan(app):
    store = CallStore()
    await store.initialize()
    app.state.calls = store
    worker = asyncio.create_task(reconcile_calls(store)) if mode() == "live" else None
    try:
        yield
    finally:
        if worker:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        await store.close()


app = FastAPI(title="freightops-gateway", lifespan=lifespan)


async def object_body(req: Request) -> dict:
    raw = await req.body()
    if len(raw) > 32_000:
        raise HTTPException(413, "request body exceeds limit")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "JSON object required") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    return body


@app.get("/healthz")
async def health():
    return {"status": "ok", "mode": mode()}


@app.post("/voice/webhook")
async def voice_webhook(req: Request, x_webhook_secret: str = Header(default="")):
    require_secret(x_webhook_secret, "CARTESIA_WEBHOOK_SECRET")
    body = await object_body(req)
    provider_id = body.get("agent_call_id") or body.get("call_id") or body.get("id")
    if not isinstance(provider_id, str):
        raise HTTPException(400, "call identifier required")
    store = req.app.state.calls
    row = await store.by_provider(provider_id)
    try:
        external = await fetch_call(provider_id)
    except VoiceError:
        raise HTTPException(502, "call provider could not be read") from None
    if row is None:
        # Recover an accepted call whose outbound response was lost using provider-held correlation.
        variables = external.get("dynamic_variables") or {}
        row = await store.get(variables.get("attempt_id", ""))
        if row is None or not valid_call_token(row, variables.get("call_token", "")) \
                or variables.get("task_id") != row["task_id"] or variables.get("case_id") != row["case_id"]:
            raise HTTPException(404, "unknown call")
        await store.set(row["id"], provider_id=provider_id)
        row = await store.get(row["id"])
    try:
        finished = await finish_call(store, row, external)
        if finished:
            try:
                await deliver_result(store, await store.get(row["id"]))
            except Exception as exc:  # noqa: BLE001 -- result is durable; reconciler retries delivery
                log.warning("callback persisted; delivery pending: %s", type(exc).__name__)
        return {"ok": True, "finished": finished, "attempt_id": row["id"]}
    except VoiceError:
        raise HTTPException(409, "call correlation mismatch") from None


@app.post("/voice/tools/{name}")
async def voice_tool(name: str, req: Request, x_call_token: str = Header(default="")):
    if name not in READ_ONLY_TOOLS | {"record_promise_to_pay", "open_dispute"}:
        raise HTTPException(404)
    body = await object_body(req)
    row = await req.app.state.calls.get(str(body.get("attempt_id", "")))
    if row is None or not valid_call_token(row, x_call_token):
        raise HTTPException(401, "invalid or expired call capability")
    if body.get("case_id", row["case_id"]) != row["case_id"]:
        raise HTTPException(403, "call belongs to a different case")
    from common.source_repository import SourceRepository

    source = await SourceRepository().load(TaskEnvelope(task_type="place_call", case_id=row["case_id"]))
    if name == "get_load_facts":
        keys = ("load_id", "lane", "gps_dwell_min", "free_time_min", "contract_rate")
    elif name in {"get_payment_status", "get_invoice_status"}:
        case = row["case_id"].replace("'", "''")
        if mode() == "live":
            db = SpacetimeClient(actor="voice_desk")
            table = "payment_proposal" if name == "get_payment_status" else "invoice"
            return {"tool": name, "result": await db.query_rows(f"SELECT * FROM {table} WHERE case_id = '{case}'")}
        keys = ("invoice_id", "invoice_amount", "paid_amount")
    elif name == "get_case_summary":
        keys = ("load_id", "lane", "invoice_id", "claim_type", "required_documents")
    else:
        event_id = body.get("event_id")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 100:
            raise HTTPException(400, "stable event_id required")
        # Append evidence only; this cannot approve or settle any financial record.
        await SpacetimeClient(actor="voice_desk").mutate("append_audit", {
            "write_id": f"voice-tool:{row['id']}:{event_id}", "case_id": row["case_id"],
            "task_id": row["task_id"], "kind": name, "reason_code": "CALLER_STATEMENT",
            "summary": str(body.get("statement", ""))[:2000], "evidence_refs": [f"call://{row['id']}"],
        })
        return {"tool": name, "result": {"recorded_as_evidence": True}}
    return {"tool": name, "result": {key: source[key] for key in keys if key in source}}


@app.get("/cases/{case_id}/why")
async def explain(case_id: str, decision: str, authorization: str = Header(default="")):
    require_secret(authorization.removeprefix("Bearer "), "GATEWAY_API_TOKEN")
    escaped = case_id.replace("'", "''")
    rows = await SpacetimeClient(actor="validator").query_rows(
        f"SELECT * FROM audit_event WHERE case_id = '{escaped}'")
    selected = [row for row in rows if decision in {row.get("id"), row.get("task_id")}]
    if not selected:
        raise HTTPException(404, "no stored evidence for this decision")
    agent = selected[-1]["agent"]
    refs = [f"audit://{row['id']}" for row in selected]
    result = await infer(agent, TaskEnvelope(task_type="explain_decision", case_id=case_id, evidence_refs=refs),
                         {"audit": selected}, purpose="explain_stored_decision")
    return {"case_id": case_id, "decision": decision, "explanation": result["assessment"],
            "evidence_refs": refs, "model_used": result["model_used"]}


@app.post("/demo/{kind}")
async def demo_trigger(kind: str):
    if kind not in {"phone", "email", "bank_change", "short_pay", "scenario"}:
        raise HTTPException(404)
    raise HTTPException(503, "W4 scenario ingestion must be connected before demo events can be accepted")


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("GATEWAY_BIND_HOST", "127.0.0.1"), port=9000)
