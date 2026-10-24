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
