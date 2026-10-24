"""Public HTTPS gateway (behind the tunnel): Cartesia webhooks, in-call tools, demo endpoints."""
import hmac
import os

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="freightops-gateway")


@app.post("/voice/webhook")
async def voice_webhook(req: Request, x_webhook_secret: str = Header(default="")):
    if not hmac.compare_digest(x_webhook_secret, os.getenv("CARTESIA_WEBHOOK_SECRET", "")) \
            or not x_webhook_secret:
        raise HTTPException(401, "bad webhook secret")
    body = await req.json()
    # TODO(W3): map agent_call_id -> parked task, fetch transcript, extract findings, resume parent
    return {"ok": True, "received": body.get("type")}


READ_ONLY_TOOLS = {"get_load_facts", "get_payment_status", "get_invoice_status", "get_case_summary"}


@app.post("/voice/tools/{name}")
async def voice_tool(name: str, req: Request):
    if name not in READ_ONLY_TOOLS | {"record_promise_to_pay", "open_dispute"}:
        raise HTTPException(404)
    # TODO(W3): verify per-call token from dynamic_variables, answer from company DB
    return {"tool": name, "result": None}


@app.post("/demo/{kind}")
async def demo_trigger(kind: str):
    if kind not in {"phone", "email", "bank_change", "short_pay", "scenario"}:
        raise HTTPException(404)
    # TODO(W4): publish the preset event to Redis with priority P0
    return {"queued": kind}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
