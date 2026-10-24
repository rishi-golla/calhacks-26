from unittest.mock import AsyncMock

import pytest
from a2a.helpers import new_task
from a2a.types import TaskState

from router.main import process_entry, route_event


def event():
    return {"event_id": "event-123", "type": "carrier_invoice.received", "priority": "P0",
            "payload": {"case_id": "case-900", "invoice_amount": 987.32}}


def test_first_hop_preserves_case_and_stable_event_identity():
    agent, env, meta = route_event(event())
    assert agent == "carrier_ap" and env.case_id == "case-900"
    assert meta.idempotency_key == "event:event-123" and meta.from_agent == "router"
    assert env.subject["invoice_amount"] == 987.32 and meta.visited == []


def test_case_hint_is_not_an_authoritative_case():
    value = event()
    value["payload"].pop("case_id")
    value["case_hint"] = "LOAD-900"
    with pytest.raises(ValueError, match="resolve a case_id"):
        route_event(value)


async def test_failed_admission_is_not_acknowledged(monkeypatch):
    import json

    from router import main
    client = AsyncMock()
    sender = AsyncMock(side_effect=TimeoutError("unavailable"))
    monkeypatch.setattr(main, "send_task", sender)
    with pytest.raises(TimeoutError):
        await process_entry(client, b"1-0", {b"json": json.dumps(event())})
    client.xack.assert_not_awaited()
    sender.side_effect = None
    sender.return_value = new_task("task", "case-900", TaskState.TASK_STATE_SUBMITTED)
    await process_entry(client, b"1-0", {b"json": json.dumps(event())})
    client.xack.assert_awaited_once_with("events", "router", b"1-0")


async def test_dispatch_persistence_failure_keeps_accepted_event_pending(monkeypatch):
    import json

    from router import main
    client = AsyncMock()
    monkeypatch.setattr(main, 'send_task', AsyncMock(return_value=new_task(
        'task', 'case-900', TaskState.TASK_STATE_SUBMITTED)))
    telemetry = AsyncMock(side_effect=RuntimeError('telemetry unavailable'))
    monkeypatch.setattr(main, 'record_dispatch', telemetry)
    with pytest.raises(RuntimeError):
        await process_entry(client, b'1-0', {b'json': json.dumps(event())})
    client.xack.assert_not_awaited()
    assert telemetry.call_args.args[2] == 'accepted'
    assert telemetry.call_args.kwargs['task_id'] == 'task'


async def test_gps_is_recorded_without_agent_admission(monkeypatch):
    import json

    from router import main
    client = AsyncMock()
    sender, telemetry = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main, 'send_task', sender)
    monkeypatch.setattr(main, 'record_dispatch', telemetry)
    await process_entry(client, b'1-0', {b'json': json.dumps({'event_id': 'gps-1', 'type': 'gps.ping'})})
    assert telemetry.call_args.args[2] == 'not_routed'
    sender.assert_not_awaited()
    client.xack.assert_awaited_once()


async def test_malformed_intake_records_triage_before_dead_letter(monkeypatch):
    from unittest.mock import MagicMock

    from router import main
    client = AsyncMock()
    pipe = AsyncMock()
    pipe.xadd = MagicMock()
    pipe.xack = MagicMock()
    client.pipeline = MagicMock(return_value=pipe)
    pipe.__aenter__.return_value = pipe
    telemetry = AsyncMock()
    monkeypatch.setattr(main, 'record_dispatch', telemetry)
    await process_entry(client, b'1-0', {b'json': '[]'})
    assert telemetry.call_args.args[2] == 'triage'
    pipe.xadd.assert_called_once()
    pipe.execute.assert_awaited_once()


async def test_dispatch_uses_router_identity_and_redis_queue_time(monkeypatch):
    from unittest.mock import MagicMock

    from router import main
    db = MagicMock(disabled=False)
    db.mutate = AsyncMock()
    factory = MagicMock(return_value=db)
    monkeypatch.setattr(main, 'SpacetimeClient', factory)
    await main.record_dispatch(b'1000-0', {}, 'triage', 'missing event', received_ms=1123)
    factory.assert_called_once_with(actor='router')
    payload = db.mutate.call_args.args[1]
    assert payload['event_id'] == 'redis:1000-0'
    assert payload['queue_ms'] == 123
    assert payload['case_id'] == ''
