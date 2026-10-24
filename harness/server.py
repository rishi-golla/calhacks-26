"""W1 harness on :8000. W1-internal under PRD v1.3: the ingest pipeline uses it for classification and
extraction; mesh agents call their own providers and must not depend on it. Keeps the v1.0 7.1 shape."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from harness.chat import BadRequest, complete
from harness.ingest.dispatch import Dispatcher
from harness.ingest.pipeline import BadEvent, process
from harness.ingest.vault import Vault
from harness.pool import Pool, Unavailable

log = logging.getLogger("harness")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = pool = Pool()
    app.state.vault, app.state.dispatcher = Vault(), Dispatcher()
    await pool.check_health()
    tasks = [asyncio.create_task(pool.health_loop())]
    if os.getenv("HARNESS_CONSUME", "0") == "1":
        from harness.ingest.consumer import run_consumer
        tasks.append(asyncio.create_task(run_consumer(pool)))
    yield
    for t in tasks:
        t.cancel()
    await pool.aclose()


app = FastAPI(title="freightline-harness", lifespan=lifespan)


def _unavailable() -> JSONResponse:
    return JSONResponse({"error": "unavailable"}, status_code=503)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/v1/models")
async def models(request: Request):
    pool: Pool = request.app.state.pool
    data = []
    for name, keys in pool.logical.items():
        serving = next((pool.backends[k] for k in keys if pool.backends[k].healthy), None)
        data.append({"id": name, "object": "model", "status": "ok" if serving else "down",
                     "serving": serving.served_name if serving else None,
                     "fallbacks": [pool.backends[k].served_name for k in keys]})
    any_up = any(b.healthy for b in pool.backends.values())
    data.append({"id": "auto", "object": "model", "status": "ok" if any_up else "down",
                 "serving": "routed", "fallbacks": []})
    backends = [{"key": b.key, "id": b.served_name, "url": b.url, "status": "ok" if b.healthy else "down",
                 "inflight": b.inflight, "queued": b.queued, "max_concurrency": b.max_concurrency,
                 "last_check": b.last_check} for b in pool.backends.values()]
    return {"object": "list", "data": data, "backends": backends, "policy_version": pool.policy_version}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    if body.get("stream"):
        return JSONResponse({"error": "stream not supported"}, status_code=400)
    try:
        return await complete(request.app.state.pool, body)
    except Unavailable as e:
        log.warning("unavailable: %s", e)
        return _unavailable()
    except KeyError as e:
        return JSONResponse({"error": f"unknown model {e}"}, status_code=400)
    except BadRequest as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/v1/ingest")
async def ingest(request: Request, dispatch: bool = True):
    """Run one event through Stage 1 (testing path; production reads Redis). ?dispatch=false = dry run."""
    st = request.app.state
    try:
        return await process(st.pool, st.vault, st.dispatcher, await request.json(), dispatch=dispatch)
    except BadEvent as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/v1/sandbox/exec")
async def sandbox_exec(req: Request):
    body = await req.json()
    return {"ok": False, "output": None, "files_out": [], "stdout": "", "limits_hit": None,
            "error": f"sandbox not implemented (tool={body.get('tool')})"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # health polls every 5 s
    uvicorn.run(app, host=os.getenv("HARNESS_HOST", "127.0.0.1"), port=int(os.getenv("HARNESS_PORT", "8000")))
