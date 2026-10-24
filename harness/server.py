"""W1 MOCK harness implementing the PRD 7.1 contract. Replace internals with vLLM/SGLang routing."""
import time

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI(title="freightline-harness (mock)")


@app.get("/v1/models")
async def models():
    return {"data": [{"id": m, "status": "mock"} for m in ("small-fast", "reasoning", "coder-tools")]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    md = body.get("metadata", {})
    t0 = time.time()
    return {
        "id": "mock-1", "object": "chat.completion", "model": "mock",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "{}"}}],
        "harness": {"model_used": "mock", "route_reason": f"mock for {md.get('agent')}/{md.get('task_type')}",
                    "ensemble": None, "latency_ms": int((time.time() - t0) * 1000), "queue_ms": 0},
    }


@app.post("/v1/sandbox/exec")
async def sandbox_exec(req: Request):
    body = await req.json()
    return {"ok": False, "output": None, "files_out": [], "stdout": "", "limits_hit": None,
            "error": f"sandbox not implemented (tool={body.get('tool')})"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
