"""Presence must reflect a running service without inventing work or bypassing disable controls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.envelope import ResultArtifact
from common.runtime import FreightAgent
from common.runtime_store import RuntimeStore
from common.server import _readiness, agent_presence_loop, build_app, publish_agent_presence
from common.spacetime_client import AgentConfig


class PresenceAgent(FreightAgent):
    name = "risk"

    async def handle(self, env, meta, task_id, updater):
        return ResultArtifact(outcome="clear")


@pytest.fixture
async def executor(tmp_path):
    agent = PresenceAgent(store=RuntimeStore(f"sqlite+aiosqlite:///{tmp_path / 'presence.sqlite'}"))
    await agent.store.initialize()
    yield agent
    await agent.store.close()


def client(enabled=True):
    return SimpleNamespace(get_agent_config=AsyncMock(return_value=AgentConfig("risk", "auto", enabled)),
                           mutate=AsyncMock())


def test_readiness_rejects_provider_without_model_tool_support(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("AGENT_PROVIDER_RISK", "anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    with pytest.raises(ValueError, match="model-selected tools require OpenAI"):
        _readiness(SimpleNamespace(name="risk"))


async def test_disabled_persistence_never_connects(monkeypatch, executor):
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    db = client()
    assert not await publish_agent_presence(executor, "emission", db=db)
    db.get_agent_config.assert_not_awaited()
    db.mutate.assert_not_awaited()


async def test_disabled_agent_is_not_reported_online(monkeypatch, executor):
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    db = client(enabled=False)
    assert not await publish_agent_presence(executor, "emission", db=db)
    db.mutate.assert_not_awaited()


async def test_idle_and_working_presence_use_real_lease_state(monkeypatch, executor):
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    db = client()
    assert await publish_agent_presence(executor, "idle-emission", db=db)
    payload = db.mutate.await_args.args[1]
    assert payload["status"] == "idle" and payload["write_id"] == "idle-emission"
    assert payload["card_url"].endswith("/.well-known/agent-card.json")
    row, _ = await executor.store.admit("risk", "router", "once", {"case_id": "case"}, {}, {})
    generation = await executor.store.claim(row["id"], "worker")
    assert await publish_agent_presence(executor, "busy-emission", db=db)
    assert db.mutate.await_args.args[1]["status"] == "busy"
    await executor.store.release(row["id"], "worker", generation, status="waiting")
    assert await publish_agent_presence(executor, "waiting-emission", db=db)
    assert db.mutate.await_args.args[1]["status"] == "idle"


async def test_presence_outage_retries_without_logging_response_secrets(monkeypatch, executor, caplog):
    from common import server
    recovered = asyncio.Event()
    ids = []

    async def publish(_agent, emission_id):
        ids.append(emission_id)
        if len(ids) == 1:
            raise RuntimeError("secret response body")
        recovered.set()
    monkeypatch.setattr(server, "publish_agent_presence", publish)
    task = asyncio.create_task(agent_presence_loop(executor, interval=0.001))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(set(ids)) == len(ids) and len(ids) >= 2
    assert "secret response body" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_lifespan_cancels_presence_on_shutdown(monkeypatch, tmp_path):
    from common import server
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    started, stopped = asyncio.Event(), asyncio.Event()

    async def presence(_executor):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def recovery(_handler):
        await asyncio.Event().wait()
    monkeypatch.setattr(server, "agent_presence_loop", presence)
    monkeypatch.setattr(server.DurableRequestHandler, "recovery_loop", recovery)
    agent = PresenceAgent(store=RuntimeStore(f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.sqlite'}"))
    app = build_app(agent, {})
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not stopped.is_set()
    assert stopped.is_set()
