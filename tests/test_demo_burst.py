"""A burst announces many operator-injected loads at once and the gateway exposes it as a guarded local control."""
import asyncio
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from sim import demo, generator
from tests.test_demo_sim import FakeRedis, StopStreaming
from tests.test_demo_sim import frozen as frozen_fixture  # noqa: F401 -- fixture re-export
from tests.test_demo_sim import world as world_fixture  # noqa: F401 -- fixture re-export


async def test_burst_announces_every_load_before_returning(world_fixture, frozen_fixture):  # noqa: F811
    raw = FakeRedis()
    started = await demo.start_burst(3, raw, seed=5)
    try:
        await asyncio.gather(*generator._BACKGROUND)
    except StopStreaming:
        pass
    announced = [event for event in raw.raw if event["type"] == "scenario.injected"]
    assert started.count == len(announced) == 3
    assert len(set(started.case_ids)) == 3 and started.case_ids == [f"CASE-{lid}" for lid in started.load_ids]
    assert {event["payload"]["burst_id"] for event in announced} == {started.burst_id}
    assert {event["priority"] for event in announced} == {"P1"} and {event["source"] for event in announced} == {"demo"}
    assert [event["payload"]["burst_index"] for event in announced] == [0, 1, 2]
    assert started.trigger_event == "carrier_invoice.received"
    # The fast-forward publishes the case-opening carrier invoice straight away for every load.
    invoices = [event for event in raw.raw if event["type"] == "email.received"
                and any(str(name).startswith("INV-") for name in event["payload"]["attachments"])]
    assert len(invoices) == 3


async def test_burst_rejects_out_of_range_counts_and_unknown_triggers(world_fixture):  # noqa: F811
    for count in (0, demo.BURST_MAX + 1, True, "10"):
        with pytest.raises(ValueError):
            await demo.start_burst(count, FakeRedis())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await demo.start_burst(1, FakeRedis(), trigger_step="paid")


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_CONTROL_TOKEN", "private-test-control")
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("A2A_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'gateway.sqlite'}")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    from gateway.app import app
    with TestClient(app) as client:
        yield client


def test_gateway_burst_requires_the_control_token_and_a_bounded_count(gateway, monkeypatch):
    calls = []

    async def fake(count, r, trigger_step="carrier_invoice", seed=None):
        calls.append((count, r is gateway.app.state.raw))
        return demo.BurstStarted("BURST-" + "0" * 32, count, trigger_step, "carrier_invoice.received",
                                 ["LD-1"] * count, ["CASE-LD-1"] * count, 0.05)

    monkeypatch.setattr(demo, "start_burst", fake)
    auth = {"Authorization": "Bearer private-test-control"}
    assert gateway.post("/demo/burst", json={"count": 2}).status_code == 401
    assert gateway.post("/demo/burst", json={"count": 2}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    for body in ({"count": 0}, {"count": 201}, {"count": True}, {"count": "5"}, {"count": 5, "scenario": "x"}, []):
        assert gateway.post("/demo/burst", json=body, headers=auth).status_code == 400
    response = gateway.post("/demo/burst", json={"count": 2}, headers=auth)
    assert response.status_code == 202
    assert response.json() == asdict(demo.BurstStarted("BURST-" + "0" * 32, 2, "carrier_invoice",
                                                        "carrier_invoice.received", ["LD-1", "LD-1"],
                                                        ["CASE-LD-1", "CASE-LD-1"], 0.05))
    assert calls == [(2, True)]


def test_gateway_reports_an_unannounced_burst_without_leaking_the_cause(gateway, monkeypatch):
    async def broken(count, r, trigger_step="carrier_invoice", seed=None):
        raise ConnectionError("redis://secret-host details")

    monkeypatch.setattr(demo, "start_burst", broken)
    response = gateway.post("/demo/burst", json={"count": 1}, headers={"Authorization": "Bearer private-test-control"})
    assert response.status_code == 503 and "secret-host" not in response.text
