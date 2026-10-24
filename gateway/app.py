"""Public HTTPS gateway (behind the tunnel): Cartesia webhooks, approvals, flags, audit reads, demo triggers.
Everything that resumes a parked agent goes through common.parking (claimed once, duplicate-safe)."""
import asyncio
import hmac
import json
import os
import pathlib
import random
import re
from email import policy as email_policy
from email.parser import BytesParser

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from common import a2a_client, store
from common.envelope import TaskEnvelope
from common.parking import resume_parked
from integrations import directory

app = FastAPI(title="freightops-gateway")


def _check(given: str, env_name: str) -> None:
    expected = os.getenv(env_name, "")
    if not expected:
        raise HTTPException(503, f"{env_name} not configured")
    if not hmac.compare_digest(given, expected):
        raise HTTPException(401, "unauthorized")


def operator(x_gateway_token: str = Header(default="")) -> None:
    """Dashboard/demo-console calls (approvals, flags, reverts, audit reads, demo triggers)."""
    _check(x_gateway_token, "GATEWAY_TOKEN")


def provider(x_webhook_secret: str = Header(default="")) -> None:
    """Cartesia webhooks and in-call tools."""
    _check(x_webhook_secret, "CARTESIA_WEBHOOK_SECRET")


OP = [Depends(operator)]


@app.get("/console", include_in_schema=False)
async def console():
    """Live ops console (static page; every data call it makes still needs the operator token)."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse((pathlib.Path(__file__).parent / "console.html").read_text())


@app.get("/cases", dependencies=OP)
async def cases():
    seen: dict[str, float] = {}
    for e in store.events():
        if e["case_id"]:
            seen[e["case_id"]] = e["ts"]
    return sorted(seen, key=seen.get, reverse=True)[:30]


# ---- voice ----
@app.post("/voice/webhook", dependencies=[Depends(provider)])
async def voice_webhook(req: Request):
    body = await req.json()
    # TODO(W4): map Cartesia's call-ended payload fields; we expect our call_id in its metadata/variables.
    call_id = body.get("call_id") or body.get("metadata", {}).get("call_id")
    p = await resume_parked(call_id, {"kind": "call_ended", "status": body.get("status", "completed"),
                                      "transcript": body.get("transcript", [])}, kind="call")
    return {"ok": p is not None}


class CallEnd(BaseModel):
    transcript: list[dict] | str = []
    status: str = "completed"


@app.post("/demo/calls/{call_id}/complete", dependencies=OP)
async def demo_call_complete(call_id: str, body: CallEnd):
    """Demo console: teammate played the counterparty; paste/stream the transcript to resume the case."""
    p = await resume_parked(call_id, {"kind": "call_ended", "status": body.status, "transcript": body.transcript}, kind="call")
    if p is None:
        raise HTTPException(404, "no parked call with that id")
    return {"ok": True, "case_id": p["case_id"]}


class EmailReply(BaseModel):
    text: str


@app.post("/demo/emails/{email_id}/reply", dependencies=OP)
async def demo_email_reply(email_id: str, body: EmailReply):
    p = await resume_parked(email_id, {"kind": "email_reply", "text": body.text}, kind="email")
    if p is None:
        raise HTTPException(404, "no parked email with that id")
    return {"ok": True, "case_id": p["case_id"]}


READ_ONLY_TOOLS = {"get_case_summary", "get_payment_status"}


@app.post("/voice/tools/{name}", dependencies=[Depends(provider)])
async def voice_tool(name: str, req: Request):
    if name not in READ_ONLY_TOOLS:
        raise HTTPException(404)
    body = await req.json()
    call = next((p for p in store.list_parked("call") if p["key"] == body.get("call_id")), None)
    if call is None:
        raise HTTPException(403, "tools are only available during an active call")
    case_id = call["case_id"]
    if name == "get_payment_status":
        inv = body.get("invoice_id", "")
        if not any(c["sheet"] == "ap_subledger" and c["row"] == inv
                   for cs in store.change_sets(case_id) for c in cs["cells"]):
            raise HTTPException(403, "invoice not part of this call's case")
        return {"invoice_id": inv, "status": {c["col"]: c["value"] for c in store.sheet("ap_subledger")
                                              if c["row"] == inv}}
    return {"case_id": case_id, "events": store.events(case_id)[-10:]}


# ---- human input ----
@app.get("/parked", dependencies=OP)
async def parked(kind: str | None = None):
    return store.list_parked(kind)


class Approval(BaseModel):
    decision: str  # approve | reject
    approver: str
    note: str = ""


@app.post("/approvals/{approval_id}", dependencies=OP)
async def approve(approval_id: str, body: Approval):
    p = await resume_parked(approval_id, {"kind": "approval", **body.model_dump()}, kind="approval")
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


@app.post("/flags", dependencies=OP)
async def flag(body: Flag):
    fid = store.add_flag(body.target_type, body.target_id, body.note, body.by, body.case_id, body.agent)
    env = TaskEnvelope(task_type="review_flag", case_id=body.case_id or fid,
                       subject={"flag_id": fid, **body.model_dump()}, priority="P1")
    await a2a_client.start_task("validator", env.model_dump(), {"case_id": env.case_id, "from_agent": "human"},
                                context_id=env.case_id)
    return {"flag_id": fid}


@app.post("/instructions/{agent}/revert/{version}", dependencies=OP)
async def revert(agent: str, version: int, by: str = "human"):
    if not store.revert_instruction(agent, version):
        raise HTTPException(404)
    store.log_event("", "human", None, "instruction_reverted", agent=agent, version=version, by=by)
    return {"ok": True}


# ---- audit reads (W2 renders these from SpacetimeDB; this is the fallback/API) ----
@app.get("/audit/log", dependencies=OP)
async def audit_log(case_id: str | None = None):
    return {"chain_ok": store.verify_chain(), "change_sets": store.change_sets(case_id)}


@app.get("/audit/sheet/{name}", dependencies=OP)
async def audit_sheet(name: str):
    return store.sheet(name)


@app.get("/audit/blame/{sheet}/{row}/{col}", dependencies=OP)
async def blame(sheet: str, row: str, col: str):
    return store.cell_history(sheet, row, col)


@app.get("/cases/{case_id}", dependencies=OP)
async def case_view(case_id: str):
    return {"events": store.events(case_id), "change_sets": store.change_sets(case_id),
            "reviews": store.reviews(case_id), "parked": [p for p in store.list_parked() if p["case_id"] == case_id]}


@app.get("/agents/{agent}/learning", dependencies=OP)
async def learning(agent: str):
    return {"memory": store.memories(agent), "instructions": store.instruction_patches(agent, include_reverted=True)}


# ---- live data plane (W4) ----
class Inject(BaseModel):
    channel: str = "call"  # call | email (verification channel for detention disputes)
    force_human_approval: bool = False
    demo_target: str | None = None


def _redis():
    import redis.asyncio as redis
    return redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


@app.post("/inject/{scenario}", dependencies=OP)
async def inject(scenario: str, body: Inject | None = None):
    """Start one live load now (real counterparties, real-time lifecycle) with the chosen planted exception."""
    from sim.generator import SCENARIOS, new_load, stream_load
    from sim.world import ensure
    if scenario not in SCENARIOS:
        raise HTTPException(404, f"scenario must be one of {sorted(SCENARIOS)}")
    body = body or Inject()
    rnd = random.Random()
    load = new_load(ensure(), rnd)
    ov = {"verification_channel": body.channel, "force_human_approval": body.force_human_approval,
          "demo_target": body.demo_target}
    task = asyncio.create_task(stream_load(_redis(), load, scenario, rnd,
                                           float(os.getenv("SIM_INJECT_SPEED", "3")), ov))
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    return {"case_id": load["case_id"], "load_id": load["load_id"], "carrier": load["carrier"]["name"],
            "shipper": load["shipper"]["name"], "scenario": scenario}


_bg: set[asyncio.Task] = set()


@app.get("/feed", dependencies=OP)
async def feed(n: int = 60):
    rows = await _redis().xrevrange("events", count=max(1, min(n, 500)))
    return [{"stream_id": sid.decode(), **json.loads(f[b"json"])} for sid, f in rows]


@app.get("/world", dependencies=OP)
async def world():
    return directory.load()


def _sim_file(sub: str, name: str, ext: str) -> pathlib.Path:
    name = name.split(":", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise HTTPException(400)
    p = pathlib.Path(os.getenv("SIM_DATA_DIR", "data")) / sub / f"{name}.{ext}"
    if not p.exists():
        raise HTTPException(404)
    return p


@app.get("/docs/{ref}", dependencies=OP)
async def source_doc(ref: str):
    return json.loads(_sim_file("docs", ref, "json").read_text())


@app.get("/docs/{ref}/pdf", dependencies=OP)
async def source_pdf(ref: str):
    return FileResponse(_sim_file("docs", ref, "pdf"), media_type="application/pdf")


@app.get("/mail/{mail_id}", dependencies=OP)
async def inbox_message(mail_id: str):
    """Raw simulated inbox message, parsed for display (headers, body, attachment names)."""
    m = BytesParser(policy=email_policy.default).parsebytes(_sim_file("mail", mail_id, "eml").read_bytes())
    body = m.get_body(("plain",))
    return {"headers": {k: str(v) for k, v in m.items()}, "body": body.get_content() if body else "",
            "attachments": [a.get_filename() for a in m.iter_attachments()]}


@app.get("/clock", dependencies=OP)
async def sim_clock():
    from sim import clock
    c = await clock.load(_redis())
    return {"business_time": c.now().isoformat(), "ratio": c.ratio}


@app.get("/answer_key", dependencies=OP)
async def answer_key(n: int = 50):
    p = pathlib.Path(os.getenv("SIM_DATA_DIR", "data")) / "answer_key.jsonl"
    lines = p.read_text().splitlines()[-max(1, min(n, 500)):] if p.exists() else []
    return [json.loads(x) for x in reversed(lines)]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("GATEWAY_PORT", "9000")))
