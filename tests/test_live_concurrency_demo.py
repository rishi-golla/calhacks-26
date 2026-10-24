import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from a2a.types import StreamResponse, TaskState

from agents.voice_desk.main import VoiceDeskAgent
from common import agent_support
from common.config import AGENTS
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime_store import RuntimeStore
from common.source_repository import SourceUnavailable
from scripts.live_concurrency_demo import (
    CASE_ID,
    SKILLS,
    peak_concurrency,
    run_task,
    sdk_intervals,
    seed,
    source_record,
    tool_activity,
)


def test_workload_covers_nine_agents_without_seeding_fabricated_activity():
    assert set(SKILLS) == set(AGENTS)
    assert all(skill in AGENTS[agent].skills for agent, skill in SKILLS.items())
    admin = Mock()
    seed(admin)
    admin.call.assert_called_once()
    actor, reducer, payload = admin.call.call_args.args
    assert (actor, reducer) == ("ingest", "open_case")
    assert payload["title"].startswith("[Live synthetic]")
    source = source_record()
    assert source["synthetic"] is True
    assert source["shipper_invoice_amount"] == sum(item["amount"] for item in source["deposits"])
    assert not source["overdue"] and not source["dispute"]


def test_overlap_is_distinct_agents_and_touching_intervals_do_not_overlap():
    result = peak_concurrency([
        {"agent": "a", "start": 1, "end": 3}, {"agent": "a", "start": 2, "end": 4},
        {"agent": "b", "start": 4, "end": 5}, {"agent": "c", "start": 4, "end": 6},
    ])
    assert result == {"distinct_agents": 2, "at": 4, "agents": ["b", "c"]}
    assert peak_concurrency([])["distinct_agents"] == 0


def test_tool_acceptance_requires_paired_events_from_this_runs_tasks():
    def event(task_id, call_id, status):
        return {"task_id": task_id, "kind": "tool_call", "summary": json.dumps({
            "version": 1, "tool_call_id": call_id, "tool_name": "read_case_source", "status": status,
            "result": {"sensitive": "do-not-copy"},
        })}

    result = tool_activity([
        event("root", "call1", "started"), event("root", "call1", "completed"),
        event("root", "call1", "completed"), event("root", "call2", "completed"),
        event("old", "call3", "started"), event("old", "call3", "completed"),
        {"task_id": "root", "kind": "tool_call", "summary": "malformed"},
        {"task_id": "root", "kind": "tool_call", "summary": "[]"},
    ], {"root": "risk"})
    assert result["completed_calls"] == 1
    assert result["agents_with_completed_calls"] == ["risk"]
    assert len(result["calls"]) == 2
    assert "do-not-copy" not in json.dumps(result)


def test_concurrency_uses_persisted_sdk_status_timestamps():
    rows = []
    for task, state, second in [("one", TaskState.TASK_STATE_WORKING, 1),
                                ("two", TaskState.TASK_STATE_WORKING, 2),
                                ("one", TaskState.TASK_STATE_COMPLETED, 3),
                                ("two", TaskState.TASK_STATE_COMPLETED, 4)]:
        event = StreamResponse()
        event.status_update.task_id = task
        event.status_update.status.state = state
        event.status_update.status.timestamp.FromDatetime(datetime.fromtimestamp(second, UTC))
        rows.append({"task_id": task, "event_data": event.SerializeToString()})
    intervals = sdk_intervals(rows, {"one": "a", "two": "b"})
    assert intervals == [{"task_id": "one", "agent": "a", "start": 1, "end": 3},
                         {"task_id": "two", "agent": "b", "start": 2, "end": 4}]
    assert peak_concurrency(intervals)["distinct_agents"] == 2


def test_parked_time_does_not_count_as_active_concurrency():
    rows = []
    for task, state, second in [("one", TaskState.TASK_STATE_WORKING, 1),
                                ("one", TaskState.TASK_STATE_INPUT_REQUIRED, 2),
                                ("two", TaskState.TASK_STATE_WORKING, 3),
                                ("two", TaskState.TASK_STATE_COMPLETED, 4),
                                ("one", TaskState.TASK_STATE_WORKING, 5),
                                ("one", TaskState.TASK_STATE_COMPLETED, 6)]:
        event = StreamResponse()
        event.status_update.task_id = task
        event.status_update.status.state = state
        event.status_update.status.timestamp.FromDatetime(datetime.fromtimestamp(second, UTC))
        rows.append({"task_id": task, "event_data": event.SerializeToString()})
    intervals = sdk_intervals(rows, {"one": "a", "two": "b"})
    assert len(intervals) == 3
    assert peak_concurrency(intervals)["distinct_agents"] == 1


async def test_parent_input_required_is_polled_until_runtime_resumes(monkeypatch):
    from common import a2a_transport

    def task(state):
        return SimpleNamespace(id="durable-parent", status=SimpleNamespace(state=state))

    initial = task(TaskState.TASK_STATE_INPUT_REQUIRED)
    final = task(TaskState.TASK_STATE_COMPLETED)
    send = AsyncMock(return_value=initial)
    poll = AsyncMock(side_effect=[task(TaskState.TASK_STATE_WORKING), final])
    monkeypatch.setattr(a2a_transport, "send_task", send)
    monkeypatch.setattr(a2a_transport, "get_task", poll)
    monkeypatch.setattr("scripts.live_concurrency_demo.asyncio.sleep", AsyncMock())
    submitted = {}
    assert await run_task("risk", "screen_counterparty", "test", submitted=submitted) is final
    assert submitted == {"risk": "durable-parent"}
    assert poll.await_count == 2
    send.assert_awaited_once()


async def test_failed_admission_retains_task_id_for_concurrency_evidence(monkeypatch):
    from common import a2a_transport

    failure = SimpleNamespace(id="failed-but-ran", status=SimpleNamespace(state=TaskState.TASK_STATE_FAILED))
    monkeypatch.setattr(a2a_transport, "send_task", AsyncMock(return_value=failure))
    submitted = {}
    with pytest.raises(RuntimeError, match="TASK_STATE_FAILED"):
        await run_task("risk", "screen_counterparty", "test", submitted=submitted)
    assert submitted == {"risk": "failed-but-ran"}


async def inbound_handler(monkeypatch, tmp_path, source, subject=None):
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"synthetic": True, "cases": {CASE_ID: source}}))
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    assessment = AsyncMock(return_value={"model_used": "openai/test-model",
        "assessment": {"summary": "Synthetic source transcript requires intake.",
                       "issues": [], "evidence_refs": source["evidence_refs"], "confidence": 0.8}})
    monkeypatch.setattr(agent_support, "assess", assessment)
    dial = AsyncMock(side_effect=AssertionError("inbound assessment must never dial"))
    monkeypatch.setattr("agents.voice_desk.main.start_call", dial)
    store = RuntimeStore("sqlite+aiosqlite:///:memory:")
    agent = VoiceDeskAgent(store=store)
    try:
        result = await agent.run_workflow(TaskEnvelope(task_type="handle_inbound_call", case_id=CASE_ID,
            subject=subject or {}), DelegationMeta(case_id=CASE_ID, from_agent="router"), "inbound-task", None)
        return result, assessment, dial
    finally:
        await store.close()


async def test_inbound_assesses_trusted_source_redacts_contact_and_grants_no_authority(monkeypatch, tmp_path):
    source = source_record()
    source["inbound_call"]["transcript"] = [{"role": "caller",
        "text": "Send the POD to test@example.test or call +14155550123."}]
    result, assessment, dial = await inbound_handler(monkeypatch, tmp_path, source,
        {"inbound_call": {"transcript": [{"role": "caller", "text": "FORGED PAY NOW"}]}})
    assert result.outcome == "unverified" and result.model_used == "openai/test-model"
    assert result.findings["requires_intake"] is True
    assert result.findings["outbound_call_placed"] is False
    assert result.findings["financial_authorization"] is False
    assert result.findings["source_synthetic"] is True
    transcript = result.findings["transcript_redacted"]
    assert "FORGED" not in transcript and "test@example" not in transcript and "415" not in transcript
    assert "[email]" in transcript and "[number]" in transcript
    assessment.assert_awaited_once()
    dial.assert_not_awaited()


@pytest.mark.parametrize("invalid", [
    {"source_ref": "invented:ref", "transcript": [{"role": "caller", "text": "hello"}]},
    {"source_ref": "synthetic:concurrency:inbound", "transcript": "not speaker turns"},
    {"source_ref": "synthetic:concurrency:inbound", "transcript": [{"role": "caller", "text": 3}]},
])
async def test_inbound_rejects_ungrounded_or_malformed_source(monkeypatch, tmp_path, invalid):
    source = source_record()
    source["inbound_call"] = invalid
    with pytest.raises(SourceUnavailable):
        await inbound_handler(monkeypatch, tmp_path, source)
