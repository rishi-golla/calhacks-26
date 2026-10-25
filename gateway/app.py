"""Authenticated voice evidence gateway with recoverable same-task A2A callbacks."""
import asyncio
import contextlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as redis
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

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
from gateway.voice_tools import READ_ONLY_TOOLS, lookup

log = logging.getLogger(__name__)


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
    from common.demo_calls import DemoCallStore
    from common.runtime_store import RuntimeStore
    store = CallStore()
    await store.initialize()
    app.state.calls = store
    app.state.demo_calls = DemoCallStore(RuntimeStore(engine=store.engine))
    await app.state.demo_calls.initialize()
    # Burst announcements go to the simulator's `raw` stream; the loads keep streaming inside this process, and
    # loads a previous gateway left mid-lifecycle are resumed from their saved progress (sim.generator).
    app.state.raw = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    from sim.generator import resume_inflight_loop
    worker = asyncio.create_task(reconcile_calls(store)) if mode() == "live" else None
    # On by default only for the live gateway, so a test or fixture process never streams another one's loads.
    resume = os.getenv("SIM_RESUME_INFLIGHT", "1" if mode() == "live" else "0") == "1"
    resumer = asyncio.create_task(resume_inflight_loop(app.state.raw)) if resume else None
    try:
        yield
    finally:
        for task in (worker, resumer):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await app.state.raw.aclose()
        await store.close()


app = FastAPI(title="freightline-gateway", lifespan=lifespan)


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
    if name in READ_ONLY_TOOLS:
        return await lookup(name, req, x_call_token)
    if name not in {"record_promise_to_pay", "open_dispute"}:
        raise HTTPException(404)
    body = await object_body(req)
    row = await req.app.state.calls.get(str(body.get("attempt_id", "")))
    if row is None or not valid_call_token(row, x_call_token):
        raise HTTPException(401, "invalid or expired call capability")
    if body.get("case_id", row["case_id"]) != row["case_id"]:
        raise HTTPException(403, "call belongs to a different case")
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


def require_demo_auth(authorization: str) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "invalid credentials")
    require_secret(authorization[7:], "DEMO_CONTROL_TOKEN")


@app.get("/demo/team-call")
async def team_call_status(req: Request, request_id: str | None = None, authorization: str = Header(default="")):
    from common.demo_calls import canonical_request_id
    require_demo_auth(authorization)
    try:
        request_id = canonical_request_id(request_id) if request_id is not None else None
    except ValueError:
        raise HTTPException(400, "request_id must be a canonical UUID") from None
    return await req.app.state.demo_calls.snapshot(request_id)


@app.post("/demo/team-call")
async def request_team_call(req: Request, authorization: str = Header(default="")):
    from common.demo_calls import canonical_request_id
    require_demo_auth(authorization)
    body = await object_body(req)
    try:
        if set(body) != {"request_id"}:
            raise ValueError
        request_id = canonical_request_id(body["request_id"])
    except (ValueError, TypeError):
        raise HTTPException(400, "Only a canonical UUID request_id is accepted") from None
    result, status = await req.app.state.demo_calls.submit(request_id)
    return JSONResponse(result, status_code=status)


@app.post("/demo/burst", status_code=202)
async def demo_burst(req: Request, authorization: str = Header(default="")):
    """Local operator load test: `count` synthetic loads announced at once, each opening a real case through
    ingest, the router and the agents. Same private control token as the team call; the browser never holds it."""
    from dataclasses import asdict

    from sim.demo import BURST_MAX, start_burst
    require_demo_auth(authorization)
    body = await object_body(req)
    count = body.get("count", 100)
    if set(body) - {"count"} or isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= BURST_MAX:
        raise HTTPException(400, f"count must be an integer from 1 to {BURST_MAX}")
    try:
        started = await start_burst(count, req.app.state.raw)
    except Exception as exc:  # noqa: BLE001 -- a missing world or an unreachable Redis is reported, never raw
        log.warning("burst not announced: %s", type(exc).__name__)
        raise HTTPException(503, "burst could not be announced; check the simulator world and Redis") from None
    return asdict(started)


@app.post("/demo/{kind}")
async def demo_trigger(kind: str):
    if kind not in {"phone", "email", "bank_change", "short_pay", "scenario"}:
        raise HTTPException(404)
    raise HTTPException(503, "W4 scenario ingestion must be connected before demo events can be accepted")


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("GATEWAY_BIND_HOST", "127.0.0.1"), port=int(os.getenv("GATEWAY_PORT", "9000")))
