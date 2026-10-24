"""First-hop router guarantees (originally W3's bridge tests), now checked against the W1 intake router."""
import pathlib
import tempfile
from unittest.mock import AsyncMock

import pytest
from a2a.helpers import new_task
from a2a.types import TaskState

from common.envelope import TaskEnvelope
from harness.ingest import consumer, dispatch
from harness.ingest.dispatch import Dispatcher
from harness.ingest.pipeline import case_id_for, process
from harness.ingest.vault import Vault
from harness.pool import CONFIG_PATH, Pool


def event():
    return {"event_id": "event-123", "type": "carrier_invoice.received", "priority": "P0",
            "payload": {"case_id": "case-900", "invoice_amount": 987.32}}


@pytest.fixture
def tmp():
    return pathlib.Path(tempfile.mkdtemp())


@pytest.fixture
async def dead_pool(tmp):
    """Models unreachable: the pipeline still routes deterministically (PRD H11)."""
    cfg = tmp / "c.toml"
    cfg.write_text(CONFIG_PATH.read_text().replace("127.0.0.1:800", "127.0.0.1:1800"))
    pool = Pool(cfg)
    await pool.check_health()
    yield pool
    await pool.aclose()


async def test_first_hop_preserves_case_and_stable_event_identity(monkeypatch, tmp):
    sender = AsyncMock(return_value=new_task("task-1", "case-900", TaskState.TASK_STATE_SUBMITTED))
    monkeypatch.setattr(dispatch, "send_task", sender)
    env = TaskEnvelope(task_type="process_carrier_invoice", case_id="case-900", subject={"invoice_amount": 987.32},
                       priority="P0")
    res = await Dispatcher(tmp / "d.sqlite").send("event-123", "process_carrier_invoice", env)
    agent, sent_env, meta = sender.await_args.args
    assert agent == "carrier_ap" and res["task_id"] == "task-1" and sent_env.case_id == "case-900"
    assert meta.idempotency_key == "event:event-123" and meta.from_agent == "router" and meta.visited == []
    assert meta.correlation_id == "case-900" and meta.causation_id == "event-123"
    assert sent_env.subject["invoice_amount"] == 987.32


async def test_case_hint_is_not_an_authoritative_case(monkeypatch, dead_pool, tmp):
    value = event()
    value["payload"].pop("case_id")
    value["case_hint"] = "LOAD-900"
    assert case_id_for(value) is None
    sender = AsyncMock()
    monkeypatch.setattr(dispatch, "send_task", sender)
    res = await process(dead_pool, Vault(tmp / "v.sqlite"), Dispatcher(tmp / "d.sqlite"), value)
    assert res["status"] == "triage" and "case_hint is not authoritative" in res["route"]["reason"]
    sender.assert_not_awaited()


async def test_acceptance_must_preserve_the_case(monkeypatch, tmp):
    monkeypatch.setattr(dispatch, "send_task",
                        AsyncMock(return_value=new_task("task-1", "other-case", TaskState.TASK_STATE_SUBMITTED)))
    env = TaskEnvelope(task_type="process_carrier_invoice", case_id="case-900")
    with pytest.raises(dispatch.DispatchError, match="preserve case"):
        await Dispatcher(tmp / "d.sqlite").send("event-123", "process_carrier_invoice", env)


async def test_failed_admission_is_not_acknowledged(tmp):
    client = AsyncMock()
    statuses = iter([{"event_id": "event-123", "case_id": "case-900", "status": "dispatch_failed", "error": "x"},
                     {"event_id": "event-123", "case_id": "case-900", "status": "accepted"}])
    handler = AsyncMock(side_effect=lambda *a: next(statuses))
    kw = {"stream": "events", "group": "router", "triage": "triage", "handler": handler}
    await consumer._handle(client, None, None, None, b"1-0", event(), **kw)
    client.xack.assert_not_awaited()
    await consumer._handle(client, None, None, None, b"1-0", event(), **kw)
    client.xack.assert_awaited_once_with("events", "router", b"1-0")
