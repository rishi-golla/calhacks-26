"""No Redis entry stays pending forever (T4b): pinned telemetry writes, writes the module can never accept, and a
bound on reducer refusals. Outages stay unbounded. The SpacetimeDB fake models the module's write receipts."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.envelope import TaskEnvelope
from common.spacetime_client import SpacetimeHTTPError, SpacetimeTransportError
from harness.ingest import consumer, dispatch, telemetry
from harness.ingest.dispatch import Dispatcher
from harness.ingest.pipeline import BadEvent, process
from harness.ingest.telemetry import MAX_PAYLOAD, Undeliverable, check_limits, record_dispatch
from harness.ingest.vault import Vault
from harness.tests.test_first_hop import SKILL, FakeSpacetime, event

KW = {"stream": "events", "group": "router", "triage": "triage"}


@pytest.fixture
def stdb(monkeypatch):
    fake = FakeSpacetime()
    monkeypatch.setattr(dispatch, "SpacetimeClient", fake.client)
    monkeypatch.setattr(telemetry, "SpacetimeClient", fake.client)
    monkeypatch.setattr(dispatch, "send_task", AsyncMock(side_effect=AssertionError("no A2A in spacetime mode")))
    return fake


@pytest.fixture
def redis():
    r, pipe = AsyncMock(), AsyncMock()
    pipe.xadd, pipe.xack = MagicMock(), MagicMock()
    pipe.__aenter__.return_value = pipe
    r.pipeline = MagicMock(return_value=pipe)
    r.dead = pipe.xadd   # the atomic dead-letter write
    return r


@pytest.fixture
def router(tmp_path, dead_pool):
    vault, d = Vault(tmp_path / "v.sqlite"), Dispatcher(tmp_path / "d.sqlite", transport="spacetime")
    return lambda r, entry, ev, **kw: consumer._handle(r, dead_pool, vault, d, entry, ev, **(KW | kw)), d


def calls(fake: FakeSpacetime, reducer: str) -> list[dict]:
    return [p for req, p in zip(fake.requests, fake.payloads(), strict=True) if req.url.path.endswith(f"/{reducer}")]


def dead_letter(r) -> dict:
    (stream, fields), _ = r.dead.call_args
    assert stream == "events:dead_letter"
    return fields


# 1. Telemetry writes are pinned per (entry, status)

async def test_committed_but_lost_telemetry_write_converges_on_retry(stdb, tmp_path):
    d = Dispatcher(tmp_path / "d.sqlite")
    stdb.outcomes = ["timeout"]   # committed, response lost
    with pytest.raises(SpacetimeTransportError):
        await record_dispatch(b"1000-0", event(), "triage", "classifier unavailable", received_ms=1100, pins=d)
    # The reclaim re-measures the queue time and may re-classify: the pinned first attempt is resent.
    await record_dispatch(b"1000-0", event(), "triage", "low confidence 0.41", received_ms=121_100, pins=d)
    assert stdb.bodies[0] == stdb.bodies[1]
    assert stdb.payloads()[1]["queue_ms"] == 100 and stdb.payloads()[1]["reason"] == "classifier unavailable"


async def test_without_a_pin_the_same_retry_is_refused_forever(stdb):
    stdb.outcomes = ["timeout"]
    with pytest.raises(SpacetimeTransportError):
        await record_dispatch(b"1000-0", event(), "triage", "x", received_ms=1100)
    with pytest.raises(SpacetimeHTTPError) as refused:
        await record_dispatch(b"1000-0", event(), "triage", "x", received_ms=121_100)
    assert refused.value.status_code == 530


async def test_the_consumer_pins_its_telemetry(stdb, redis, tmp_path):
    d = Dispatcher(tmp_path / "d.sqlite")
    reasons = iter(["classifier unavailable", "low confidence 0.41"])
    handler = AsyncMock(side_effect=lambda *a: {"event_id": "EVT-1", "case_id": "CASE-900", "status": "triage",
                                                "route": {"reason": next(reasons)}})
    stdb.outcomes = ["timeout"]
    with pytest.raises(SpacetimeTransportError):   # an outage: the entry stays pending
        await consumer._handle(redis, None, None, d, b"1000-0", event(), **KW, handler=handler)
    redis.xack.assert_not_awaited()
    await consumer._handle(redis, None, None, d, b"1000-0", event(), **KW, handler=handler)
    assert stdb.bodies[0] == stdb.bodies[1]
    redis.xack.assert_awaited_once_with("events", "router", b"1000-0")


# 2. A write the module can never accept is dead-lettered at once

def test_check_limits_mirrors_the_module_caps():
    check_limits("r", {"write_id": "w" * 200, "event_id": "e" * 4096, "envelope": {"x": "y" * 1000}})
    with pytest.raises(Undeliverable, match="write_id is 201 characters"):
        check_limits("r", {"write_id": "w" * 201})
    with pytest.raises(Undeliverable, match="event_id is 4097 characters"):
        check_limits("r", {"write_id": "w", "event_id": "e" * 4097})
    with pytest.raises(Undeliverable, match="non-finite"):
        check_limits("r", {"write_id": "w", "envelope": {"amount": float("nan")}})
    with pytest.raises(Undeliverable, match="module limit 262144"):
        check_limits("r", {"write_id": "w", "envelope": {"text": "é" * (MAX_PAYLOAD // 6)}})   # é is 6 bytes


async def test_an_overlong_event_id_is_dead_lettered_once_without_an_enqueue(stdb, redis, router):
    handle, d = router
    long_id = "EVT-" + "x" * 296
    await handle(redis, b"1-0", event(long_id))
    assert calls(stdb, "enqueue_first_hop") == []
    fields = dead_letter(redis)
    assert redis.dead.call_count == 1 and "write_id is 310 characters (module limit 200)" in fields["reason"]
    assert json.loads(fields["json"])["event_id"] == long_id
    [triage] = calls(stdb, "record_dispatch")   # the dashboard sees why
    assert triage["status"] == "triage" and triage["event_id"] == long_id and "310 characters" in triage["reason"]
    assert d.get(long_id)["status"] == "dead_letter" and d.pinned(long_id) is None
    redis.xack.assert_not_awaited()   # acknowledged inside the atomic dead-letter pipeline instead


async def test_an_oversized_envelope_is_dead_lettered(stdb, redis, router):
    handle, d = router
    big = event()
    big["payload"]["notes"] = "n" * (MAX_PAYLOAD + 1)
    await handle(redis, b"1-0", big)
    assert calls(stdb, "enqueue_first_hop") == []
    assert "enqueue_first_hop payload is" in dead_letter(redis)["reason"]
    assert d.get("EVT-1")["status"] == "dead_letter"


async def test_an_id_too_long_for_any_row_is_dead_lettered_without_telemetry(stdb, redis, router):
    handle, d = router
    await handle(redis, b"1-0", event("E" * 5000))
    assert stdb.requests == []   # neither the enqueue nor the triage row could ever be accepted
    assert "write_id is 5010 characters" in dead_letter(redis)["reason"]
    assert d.get("E" * 5000)["status"] == "dead_letter"


@pytest.mark.parametrize("raw", [b"[1, 2]", b'"text"', b"not json"])
def test_json_that_is_not_an_event_object_is_unparseable(raw):
    assert consumer._decode(b"1-0", {b"json": raw}) is None


async def test_a_malformed_type_is_a_bad_event_not_a_crash(stdb, redis, router, dead_pool, tmp_path):
    handle, _ = router
    weird = {**event(), "type": ["carrier_invoice.received"]}
    assert consumer.needs_model(weird) is False
    with pytest.raises(BadEvent):
        await process(dead_pool, Vault(tmp_path / "v2.sqlite"), Dispatcher(tmp_path / "d2.sqlite"), weird)
    await handle(redis, b"1-0", weird)
    assert dead_letter(redis)["reason"].startswith("bad event")


# 3. Reducer refusals are bounded; outages are not

async def test_the_eleventh_refusal_dead_letters_and_success_resets_the_count(stdb, redis, router, monkeypatch):
    monkeypatch.setattr(consumer, "MAX_REJECTED_ATTEMPTS", 10)
    handle, d = router
    stdb.outcomes = [530] * 11   # a human-owned case refuses every time
    for attempt in range(1, 11):
        await handle(redis, b"1-0", event())
        assert d.rejections("EVT-1") == attempt and redis.dead.call_count == 0
    redis.xack.assert_not_awaited()
    await handle(redis, b"1-0", event())
    reason = dead_letter(redis)["reason"]
    assert redis.dead.call_count == 1 and reason.startswith("refused 11 times, last: first hop for EVT-1 rejected")
    assert "HTTP 530" in reason
    assert len(calls(stdb, "enqueue_first_hop")) == 11 and len(set(stdb.bodies[:11])) == 1
    assert d.rejections("EVT-1") == 0 and d.get("EVT-1")["status"] == "dead_letter"
    [triage] = calls(stdb, "record_dispatch")
    assert triage["status"] == "triage" and triage["reason"].startswith("refused 11 times")

    # A missing case converges once ingest opens it: the count resets on success.
    stdb.outcomes = [530] * 3
    for _ in range(3):
        await handle(redis, b"2-0", event("EVT-2"))
    assert d.rejections("EVT-2") == 3
    await handle(redis, b"2-0", event("EVT-2"))
    redis.xack.assert_awaited_once_with("events", "router", b"2-0")
    assert d.rejections("EVT-2") == 0 and d.get("EVT-2")["status"] == "queued"


async def test_a_dead_lettered_event_replays_with_a_fresh_count(stdb, redis, router, monkeypatch):
    monkeypatch.setattr(consumer, "MAX_REJECTED_ATTEMPTS", 0)
    handle, d = router
    stdb.outcomes = [530]
    await handle(redis, b"1-0", event())
    assert redis.dead.call_count == 1 and d.pinned("EVT-1") is not None
    await handle(redis, b"9-0", event())   # replayed after a person released the case
    assert d.get("EVT-1")["status"] == "queued"
    first, replay = [json.dumps(p) for p in calls(stdb, "enqueue_first_hop")]
    assert first == replay   # the pinned payload, so a committed earlier attempt would be a no-op


@pytest.mark.parametrize("outage", [503, 429, 402, 408, "timeout", "connect"])
async def test_outages_are_never_bounded(stdb, redis, router, monkeypatch, outage):
    monkeypatch.setattr(consumer, "MAX_REJECTED_ATTEMPTS", 1)
    handle, d = router
    stdb.outcomes = [outage] * 5
    for _ in range(5):
        await handle(redis, b"1-0", event())
    assert redis.dead.call_count == 0 and d.rejections("EVT-1") == 0 and d.get("EVT-1")["status"] == "retry"
    redis.xack.assert_not_awaited()


async def test_refused_telemetry_is_bounded_too(stdb, redis, tmp_path, monkeypatch):
    # e.g. a skill the published module does not know: every accepted row is refused.
    monkeypatch.setattr(consumer, "MAX_REJECTED_ATTEMPTS", 2)
    d = Dispatcher(tmp_path / "d.sqlite")
    d.record("EVT-1", "CASE-900", SKILL, "carrier_ap", "task-1", "accepted", "ok", None)
    handler = AsyncMock(return_value={"event_id": "EVT-1", "case_id": "CASE-900", "status": "accepted",
                                      "route": {"reason": "7.3 table"},
                                      "dispatch": {"agent": "carrier_ap", "skill": SKILL, "task_id": "task-1"}})
    stdb.outcomes = [530] * 3   # three accepted rows; the module accepts the give-up's triage row
    for attempt in (1, 2):
        await consumer._handle(redis, None, None, d, b"1-0", event(), **KW, handler=handler)
        assert d.rejections("EVT-1") == attempt and redis.dead.call_count == 0
    await consumer._handle(redis, None, None, d, b"1-0", event(), **KW, handler=handler)
    assert dead_letter(redis)["reason"].startswith("refused 3 times, last: dispatch telemetry rejected")
    row = d.get("EVT-1")
    assert row["status"] == "accepted" and row["task_id"] == "task-1"   # the delivered task is not forgotten


async def test_other_telemetry_failures_still_leave_the_entry_pending(redis, monkeypatch, tmp_path):
    monkeypatch.setattr(consumer, "record_dispatch", AsyncMock(side_effect=RuntimeError("unavailable")))
    handler = AsyncMock(return_value={"event_id": "EVT-1", "case_id": None, "status": "triage",
                                      "route": {"reason": "unlabeled"}})
    with pytest.raises(RuntimeError):
        await consumer._handle(redis, None, None, Dispatcher(tmp_path / "d.sqlite"), b"1-0", event(), **KW,
                               handler=handler)
    redis.xack.assert_not_awaited()
    redis.dead.assert_not_called()


# A reclaim of the same entry finishes its recorded outcome; another entry for the event is a duplicate.

async def test_a_triage_outcome_survives_a_failed_telemetry_write(stdb, redis, router):
    handle, _ = router
    unattached = event()
    unattached["payload"].pop("case_id")   # no upstream case: triage
    stdb.outcomes = ["timeout"]   # the triage row committed, the response was lost
    with pytest.raises(SpacetimeTransportError):
        await handle(redis, b"1-0", unattached)
    redis.xadd.assert_not_awaited()
    redis.xack.assert_not_awaited()
    await handle(redis, b"1-0", unattached)   # reclaimed: still triage, not its own duplicate
    first, retry = calls(stdb, "record_dispatch")
    assert first == retry and first["status"] == "triage"   # the pin makes the retry a receipt no-op
    (stream, item), _ = redis.xadd.await_args
    assert stream == "triage" and json.loads(item["json"])["event_id"] == "EVT-1"
    redis.xack.assert_awaited_once_with("events", "router", b"1-0")
    await handle(redis, b"2-0", unattached)   # Redis delivers the event again: a duplicate, no second item
    assert redis.xadd.await_count == 1 and len(calls(stdb, "record_dispatch")) == 2


async def test_an_accepted_outcome_survives_a_failed_telemetry_write(stdb, redis, dead_pool, tmp_path, monkeypatch):
    from a2a.helpers import new_task
    from a2a.types import TaskState
    monkeypatch.setattr(dispatch, "send_task",
                        AsyncMock(return_value=new_task("task-1", "CASE-900", TaskState.TASK_STATE_SUBMITTED)))
    vault, d = Vault(tmp_path / "v.sqlite"), Dispatcher(tmp_path / "d.sqlite", transport="a2a")
    stdb.outcomes = [503]
    with pytest.raises(SpacetimeHTTPError):
        await consumer._handle(redis, dead_pool, vault, d, b"1-0", event(), **KW)
    await consumer._handle(redis, dead_pool, vault, d, b"1-0", event(), **KW)
    statuses = [p["status"] for p in calls(stdb, "record_dispatch")]
    assert statuses == ["accepted", "accepted"] and calls(stdb, "record_dispatch")[1]["task_id"] == "task-1"
    redis.xack.assert_awaited_once()


async def test_a_lost_ack_is_finished_without_a_duplicate_row(stdb, redis, router):
    handle, _ = router
    redis.xack.side_effect = [ConnectionError("lost"), None]
    with pytest.raises(ConnectionError):
        await handle(redis, b"1-0", event())
    await handle(redis, b"1-0", event())
    assert len(calls(stdb, "enqueue_first_hop")) == 1 and calls(stdb, "record_dispatch") == []
    assert redis.xack.await_count == 2


# Refusals of every write are the deployment, not the event

async def test_a_router_whose_every_write_is_refused_keeps_its_entries(stdb, redis, router, monkeypatch):
    monkeypatch.setattr(consumer, "MAX_REJECTED_ATTEMPTS", 1)
    handle, d = router
    stdb.outcomes = [530] * 20   # e.g. the router identity is not provisioned: the triage row is refused too
    for _ in range(5):
        await handle(redis, b"1-0", event())
    assert redis.dead.call_count == 0 and d.rejections("EVT-1") == 5
    redis.xack.assert_not_awaited()


async def test_a_committed_enqueue_whose_pin_was_lost_is_queued(stdb, redis, router, tmp_path):
    handle, _ = router
    await handle(redis, b"1-0", event())
    # ingest.sqlite is gone (or another router host reclaims the entry): the recomputed payload differs.
    fresh = Dispatcher(tmp_path / "other.sqlite", transport="spacetime")
    token = telemetry.QUEUE_MS.set(99_999)
    try:
        res = await fresh.send("EVT-1", SKILL, TaskEnvelope(task_type=SKILL, case_id="CASE-900",
                                                             subject={"recomputed": True}))
    finally:
        telemetry.QUEUE_MS.reset(token)
    assert res["status"] == "queued" and fresh.get("EVT-1")["reason"] == "ok (already enqueued)"
    assert stdb.queries == ["SELECT id FROM routing_outbox WHERE id = 'first_hop:EVT-1'"]


# Form limits as the module measures them

def test_limits_count_utf16_units_and_events_need_real_ids():
    check_limits("r", {"write_id": "first_hop:" + "😀" * 95})   # 200 UTF-16 units, as JS counts them
    with pytest.raises(Undeliverable, match="202 characters"):
        check_limits("r", {"write_id": "first_hop:" + "😀" * 96})


async def test_a_blank_event_id_is_a_bad_event(dead_pool, tmp_path):
    with pytest.raises(BadEvent):
        await process(dead_pool, Vault(tmp_path / "v.sqlite"), Dispatcher(tmp_path / "d.sqlite"), event("   "))


async def test_an_acknowledged_entry_drops_its_pins(stdb, redis, router, tmp_path):
    handle, d = router
    unattached = event()
    unattached["payload"].pop("case_id")
    await handle(redis, b"1-0", unattached)
    redis.xack.assert_awaited_once()
    assert d._db.execute("SELECT count(*) FROM write_pin").fetchone()[0] == 0


async def test_one_poison_entry_does_not_hold_up_its_batch(monkeypatch, tmp_path):
    import asyncio
    import uuid

    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6379/0")
    try:
        await r.ping()
    except (aioredis.ConnectionError, OSError):
        pytest.skip("local redis not available")
    stream = f"test-poison-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(consumer, "record_dispatch", AsyncMock(side_effect=RuntimeError("telemetry down")))
    seen = []

    async def handler(pool, vault, dispatcher, ev):
        seen.append(ev["event_id"])
        return {"event_id": ev["event_id"], "case_id": None, "status": "not_routed", "route": {"reason": "gps"}}

    await r.xadd(stream, {"json": "[not an event]"})
    await r.xadd(stream, {"json": json.dumps({"event_id": "GPS-1", "type": "gps.ping"})})
    task = asyncio.create_task(consumer.run_consumer(
        None, stream=stream, group="router", triage=f"{stream}:triage", consumer="test", handler=handler,
        vault=Vault(tmp_path / "v.sqlite"), dispatcher=Dispatcher(tmp_path / "d.sqlite")))
    try:
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        info = (await r.xinfo_groups(stream))[0]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await r.delete(stream, f"{stream}:triage", f"{stream}:dead_letter")
        await r.aclose()
    assert seen == ["GPS-1"] and info["pending"] == 1   # only the poison entry waits (its triage row failed)
