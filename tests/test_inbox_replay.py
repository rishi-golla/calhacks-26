"""Checks for the shipped, recorded fixture replay and its public export boundary."""
import json
from datetime import datetime
from pathlib import Path

from common.config import AGENTS
from scripts.capture_inbox_replay import SECRET_FIELDS, sanitize

CAPTURE = Path(__file__).resolve().parents[1] / "web/lib/agent-inbox-replay.json"


def test_public_replay_removes_nested_contact_targets_and_capabilities():
    original = {"summary": "confirmed", "findings": {"phone": "+15555550100", "call_token": "private",
                "email": "private@example.test", "items": [{"routed_to": "+15555550101", "amount": 500}]}}
    result = sanitize(original)
    assert result == {"summary": "confirmed", "findings": {"items": [{"amount": 500}]}}
    assert original["findings"]["call_token"] == "private"


def test_recorded_fixture_contains_all_nine_services_and_consistent_actual_handoffs():
    capture = json.loads(CAPTURE.read_text())
    assert capture["metadata"]["mode"] == "fixture"
    assert capture["metadata"]["service_count"] == len(AGENTS) == 9
    assert capture["metadata"]["protocol"] == "A2A 1.0"
    tables = capture["tables"]
    tasks = {row["task_id"]: row for row in tables["task"]}
    assert len(tasks) == capture["metadata"]["task_count"]
    assert {row["agent"] for row in tasks.values()} == set(AGENTS)
    assert len(tables["delegation_edge"]) >= 5
    for edge in tables["delegation_edge"]:
        parent, child = tasks[edge["parent_task_id"]], tasks[edge["task_id"]]
        assert child["parent_task_id"] == parent["task_id"]
        assert parent["case_id"] == child["case_id"] == edge["case_id"]
        assert edge["to_agent"] in AGENTS[edge["from_agent"]].may_delegate_to
    for task in tasks.values():
        assert task["a2a_state"] == "completed" and task["simulated"]
        assert datetime.fromisoformat(task["started_at"]) <= datetime.fromisoformat(task["updated_at"])
        artifact = json.loads(task["artifact_json"])
        assert sanitize(artifact) == artifact
        assert artifact.get("model_used", "fixture/deterministic") in {None, "fixture/deterministic"}
    # Fixtures do not manufacture W2 financial writes, real calls or agent heartbeats.
    assert tables["call"] == tables["review"] == tables["agent"] == []
    assert all(row["kind"] == "a2a_status" for row in tables["audit_event"])


def test_capture_never_contains_contact_values_or_secret_fields():
    capture = json.loads(CAPTURE.read_text())

    def inspect(value):
        if isinstance(value, dict):
            assert not set(value) & SECRET_FIELDS
            for key, child in value.items():
                inspect(json.loads(child) if key == "artifact_json" else child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
    inspect(capture)
