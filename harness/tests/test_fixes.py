"""Regressions from the first live sim run: strict JSON, invoice cross-checks, and consumer concurrency."""
import asyncio
import json
import uuid

import pytest
import redis.asyncio as aioredis

from harness.chat import check_format, parse_json
from harness.ingest import consumer
from harness.ingest.dispatch import Dispatcher
from harness.ingest.pipeline import amount_checks
from harness.ingest.schemas import SCHEMAS
from harness.ingest.vault import Vault


@pytest.mark.parametrize("bad", ['{"total": Infinity}', '{"x": NaN}', '{"x": -Infinity}', '{"x": 1e999}'])
def test_non_finite_json_is_invalid(bad):
    with pytest.raises(ValueError):
        parse_json(bad)
    _, err = check_format(bad, {"type": "json_object"})
    assert err is not None


def test_lone_surrogate_is_invalid():
    with pytest.raises(ValueError):
        parse_json('{"name": "\\udefa"}')
    assert parse_json('{"name": "caf\\u00e9 \\ud83d\\ude9a"}') == {"name": "café 🚚"}


def test_ingest_calls_do_not_ensemble():
    from harness.pool import Pool
    plan = Pool().plan({"model": "auto"}, {"agent": "ingest", "task_type": "extract_invoice", "stakes": "high"})
    assert not plan.ensemble and plan.reason.startswith("ingest")
    assert Pool().plan({"model": "auto"}, {"agent": "risk", "stakes": "high"}).ensemble


def test_finite_json_still_parses():
    assert parse_json('```json\n{"total": 2150.5, "n": 3}\n```') == {"total": 2150.5, "n": 3}


def test_invoice_schema_has_no_contract_rate_and_is_closed():
    inv = SCHEMAS["carrier_invoice"]
    assert "contract_rate" not in inv["properties"]
    assert inv["additionalProperties"] is False
    assert inv["properties"]["line_items"]["items"]["additionalProperties"] is False


def test_contract_rate_vs_linehaul_is_not_a_mismatch():
    payload = {"contract_rate": 765.18, "invoice_amount": 965.18,
               "line_items": [{"desc": "Linehaul", "amount": 850.0}, {"desc": "Fuel", "amount": 115.18}]}
    assert amount_checks(payload, {"contract_rate": 850.0, "invoice_amount": 965.18}) == []
    assert amount_checks(payload, {"invoice_amount": 999.0}) == ["invoice_amount: payload 965.18 vs document 999.0"]


async def test_consumer_does_not_block_tms_behind_model_work(tmp_path, monkeypatch):
    r = aioredis.from_url("redis://localhost:6379/0")
    try:
        await r.ping()
    except (aioredis.ConnectionError, OSError):
        pytest.skip("local redis not available")
    stream, triage = f"test-events-{uuid.uuid4().hex[:6]}", f"test-triage-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(consumer, "MODEL_CONCURRENCY", 2)
    done: dict[str, float] = {}
    seen: list[str] = []
    running = {"now": 0, "max": 0}
    loop = asyncio.get_running_loop()

    async def fake_process(pool, vault, dispatcher, ev):
        seen.append(ev["event_id"])
        if consumer.needs_model(ev):
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            await asyncio.sleep(0.6)
            running["now"] -= 1
            status = "accepted"
        else:
            status = "not_routed"
        done[ev["event_id"]] = loop.time()
        return {"event_id": ev["event_id"], "case_id": "CASE-1", "status": status}

    for i in range(4):
        await r.xadd(stream, {"json": json.dumps({"event_id": f"INV-{i}", "type": "carrier_invoice.received"})})
    for i in range(20):
        await r.xadd(stream, {"json": json.dumps({"event_id": f"GPS-{i}", "type": "gps.ping"})})
    start = loop.time()
    task = asyncio.create_task(consumer.run_consumer(
        None, stream=stream, group="router", triage=triage, consumer="test", handler=fake_process,
        vault=Vault(tmp_path / "v.sqlite"), dispatcher=Dispatcher(tmp_path / "d.sqlite")))
    try:
        for _ in range(100):
            if len(done) == 24:
                break
            await asyncio.sleep(0.05)
        info = (await r.xinfo_groups(stream))[0]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await r.delete(stream, triage)
        await r.aclose()
    assert len(done) == 24 and len(seen) == 24, "every entry processed exactly once"
    assert max(done[f"GPS-{i}"] for i in range(20)) - start < 0.5, "TMS events waited behind invoices"
    assert running["max"] == 2, "model-bound concurrency should hit, not exceed, the cap"
    assert info["pending"] == 0


def test_lite_schema_keeps_only_pii_and_amounts():
    from harness.ingest.schemas import lite_schema
    props = lite_schema("carrier_invoice")["properties"]
    assert set(props) == {"invoice_amount", "line_items", "pii"}
    assert set(lite_schema("shipper_payment")["properties"]) == {"amount", "invoices_applied", "pii"}


def test_structured_payload_detection():
    from harness.ingest.schemas import is_structured
    assert is_structured("carrier_invoice", {"invoice_amount": 2150.0, "load_id": "LOAD-4821", "text": "..."})
    assert not is_structured("email", {"from": "a@b.co", "subject": "hi", "text": "..."})


async def test_consumer_recreates_the_group_after_redis_loses_it(tmp_path):
    """XGROUP DESTROY plus DEL stands in for a Redis restart without persistence: the next poll answers NOGROUP,
    the consumer recreates the group, and a new entry is processed instead of the loop backing off forever."""
    r = aioredis.from_url("redis://localhost:6379/0")
    try:
        await r.ping()
    except (aioredis.ConnectionError, OSError):
        pytest.skip("local redis not available")
    stream, triage = f"test-events-{uuid.uuid4().hex[:6]}", f"test-triage-{uuid.uuid4().hex[:6]}"
    seen: list[str] = []

    async def fake_process(pool, vault, dispatcher, ev):
        seen.append(ev["event_id"])
        return {"event_id": ev["event_id"], "case_id": "CASE-1", "status": "not_routed"}

    task = asyncio.create_task(consumer.run_consumer(
        None, stream=stream, group="router", triage=triage, consumer="test", handler=fake_process,
        vault=Vault(tmp_path / "v.sqlite"), dispatcher=Dispatcher(tmp_path / "d.sqlite")))
    try:
        await r.xadd(stream, {"json": json.dumps({"event_id": "BEFORE", "type": "gps.ping"})})
        for _ in range(100):
            if "BEFORE" in seen:
                break
            await asyncio.sleep(0.05)
        await r.xgroup_destroy(stream, "router")
        await r.delete(stream)
        await asyncio.sleep(1.2)  # past the blocking read, so the next poll sees NOGROUP
        await r.xadd(stream, {"json": json.dumps({"event_id": "AFTER", "type": "gps.ping"})})
        for _ in range(160):
            if "AFTER" in seen:
                break
            await asyncio.sleep(0.05)
        groups = await r.xinfo_groups(stream)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await r.delete(stream, triage)
        await r.aclose()
    assert seen == ["BEFORE", "AFTER"], "the entry added after the group was lost must still be processed"
    assert [g["name"] for g in groups] == [b"router"] or [g["name"] for g in groups] == ["router"]
