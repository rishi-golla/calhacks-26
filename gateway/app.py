"""Public HTTPS gateway (behind the tunnel): Cartesia webhooks, approvals, flags, audit reads, demo triggers.
Everything that resumes a parked agent goes through common.parking (claimed once, duplicate-safe)."""
import hmac
import json
import os
import pathlib
import time
import uuid

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from common import a2a_client, store
from common.envelope import TaskEnvelope
from common.parking import resume_parked
from router.main import dispatch

app = FastAPI(title="freightops-gateway")
FIXTURES = pathlib.Path(__file__).parent.parent / "sim" / "fixtures"


# ---- voice ----
@app.post("/voice/webhook")
async def voice_webhook(req: Request, x_webhook_secret: str = Header(default="")):
    secret = os.getenv("CARTESIA_WEBHOOK_SECRET", "")
    if not secret or not hmac.compare_digest(x_webhook_secret, secret):
        raise HTTPException(401, "bad webhook secret")
    body = await req.json()
    # TODO(W4): map Cartesia's call-ended payload fields; we expect our call_id in its metadata/variables.
    call_id = body.get("call_id") or body.get("metadata", {}).get("call_id")
    p = await resume_parked(call_id, {"kind": "call_ended", "status": body.get("status", "completed"),
                                      "transcript": body.get("transcript", [])})
    return {"ok": p is not None}


class CallEnd(BaseModel):
    transcript: list[dict] | str = []
    status: str = "completed"


@app.post("/demo/calls/{call_id}/complete")
async def demo_call_complete(call_id: str, body: CallEnd):
    """Demo console: teammate played the counterparty; paste/stream the transcript to resume the case."""
    p = await resume_parked(call_id, {"kind": "call_ended", "status": body.status, "transcript": body.transcript})
    if p is None:
        raise HTTPException(404, "no parked call with that id")
    return {"ok": True, "case_id": p["case_id"]}


class EmailReply(BaseModel):
    text: str


@app.post("/demo/emails/{email_id}/reply")
async def demo_email_reply(email_id: str, body: EmailReply):
    p = await resume_parked(email_id, {"kind": "email_reply", "text": body.text})
    if p is None:
        raise HTTPException(404, "no parked email with that id")
    return {"ok": True, "case_id": p["case_id"]}


READ_ONLY_TOOLS = {"get_case_summary", "get_payment_status"}


@app.post("/voice/tools/{name}")
async def voice_tool(name: str, req: Request):
    if name not in READ_ONLY_TOOLS:
        raise HTTPException(404)
    body = await req.json()
    if name == "get_payment_status":
        inv = body.get("invoice_id", "")
        return {"invoice_id": inv, "status": {c["col"]: c["value"] for c in store.sheet("ap_subledger")
                                              if c["row"] == inv}}
    return {"case_id": body.get("case_id"), "events": store.events(body.get("case_id"))[-10:]}


# ---- human input ----
@app.get("/parked")
async def parked(kind: str | None = None):
    return store.list_parked(kind)


class Approval(BaseModel):
    decision: str  # approve | reject
    approver: str
    note: str = ""


@app.post("/approvals/{approval_id}")
async def approve(approval_id: str, body: Approval):
    p = await resume_parked(approval_id, {"kind": "approval", **body.model_dump()})
    if p is None:
        raise HTTPException(404, "no pending approval with that id")
    return {"ok": True, "case_id": p["case_id"]}


class Flag(BaseModel):
    target_type: str  # cell | row | task | call | decision | memory
    target_id: str
    note: str
    by: str
    case_id: str = ""
    agent: str = ""


@app.post("/flags")
async def flag(body: Flag):
    fid = store.add_flag(body.target_type, body.target_id, body.note, body.by, body.case_id, body.agent)
    env = TaskEnvelope(task_type="review_flag", case_id=body.case_id or fid,
                       subject={"flag_id": fid, **body.model_dump()}, priority="P1")
    await a2a_client.start_task("validator", env.model_dump(), {"case_id": env.case_id, "from_agent": "human"},
                                context_id=env.case_id)
    return {"flag_id": fid}


@app.post("/instructions/{agent}/revert/{version}")
async def revert(agent: str, version: int, by: str = "human"):
    if not store.revert_instruction(agent, version):
        raise HTTPException(404)
    store.log_event("", "human", None, "instruction_reverted", agent=agent, version=version, by=by)
    return {"ok": True}


# ---- audit reads (W2 renders these from SpacetimeDB; this is the fallback/API) ----
@app.get("/audit/log")
async def audit_log(case_id: str | None = None):
    return {"chain_ok": store.verify_chain(), "change_sets": store.change_sets(case_id)}


@app.get("/audit/sheet/{name}")
async def audit_sheet(name: str):
    return store.sheet(name)


@app.get("/audit/blame/{sheet}/{row}/{col}")
async def blame(sheet: str, row: str, col: str):
    return store.cell_history(sheet, row, col)


@app.get("/cases/{case_id}")
async def case_view(case_id: str):
    return {"events": store.events(case_id), "change_sets": store.change_sets(case_id),
            "reviews": store.reviews(case_id), "parked": [p for p in store.list_parked() if p["case_id"] == case_id]}


@app.get("/agents/{agent}/learning")
async def learning(agent: str):
    return {"memory": store.memories(agent), "instructions": store.instruction_patches(agent, include_reverted=True)}


# ---- demo triggers ----
class Scenario(BaseModel):
    channel: str = "call"  # call | email
    demo_target: str | None = None
    force_human_approval: bool = False
    overrides: dict = {}


@app.post("/demo/{kind}")
async def demo_trigger(kind: str, body: Scenario | None = None):
    """Publish a preset event (Redis if the router is running, else dispatch directly over A2A)."""
    if kind not in {"phone", "email", "scenario"}:
        raise HTTPException(404)
    body = body or Scenario()
    hero = json.loads((FIXTURES / "hero_case.json").read_text())
    run = uuid.uuid4().hex[:4].upper()
    payload = {**hero, "case_id": f"{hero['case_id']}-{run}", "invoice_id": f"INV-{hero['load_id']}-{run}",
               "verification_channel": "email" if kind == "email" else body.channel,
               "demo_target": body.demo_target, "force_human_approval": body.force_human_approval,
               **body.overrides}
    ev = {"event_id": f"EVT-{uuid.uuid4().hex[:8]}", "type": "carrier_invoice.received", "ts": time.time(),
          "priority": "P0", "payload": payload, "source": f"demo:{kind}"}
    if os.getenv("DEMO_VIA_REDIS", "0") == "1":
        import redis.asyncio as redis
        await redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0")).xadd("events", {"json": json.dumps(ev)})
        return {"queued": ev["event_id"], "case_id": payload["case_id"]}
    task_id = await dispatch(ev)
    return {"dispatched": task_id, "case_id": payload["case_id"]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("GATEWAY_PORT", "9000")))
