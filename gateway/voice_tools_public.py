"""Minimal HTTPS-tunnel surface: active-call lookups only, no operator controls."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.voice import CallStore
from gateway.voice_tools import lookup


@asynccontextmanager
async def lifespan(app):
    store = CallStore()
    await store.initialize()
    app.state.calls = store
    try:
        yield
    finally:
        await store.close()


app = FastAPI(title="FreightLine call lookups", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_api_route("/voice/tools/{name}", lookup, methods=["POST"])


@app.get("/healthz")
async def health():
    return {"ok": True, "service": "voice-lookups"}
