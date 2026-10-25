"""Validator-written lessons are read back by later tasks under the same scope."""
import json
from types import SimpleNamespace

import pytest

from agents.validator.tools import deterministic_review
from common import tool_loop
from common.envelope import TaskEnvelope
from common.learning import memory_scope, task_memory_scope
from common.providers import ProviderConfig, infer
from common.spacetime_client import SpacetimeClient, SpacetimeTransportError
from tests.test_validator import cash_bundle

SKILL = "assess_financial_impact"


class Agent:
    name = "cfo"


def validator_lesson():
    """The real Validator review of a wrong CFO result, as W2 would store it."""
    bundle = cash_bundle()
    bundle["source"]["claim_type"] = "detention"
    bundle["artifact"]["findings"]["net_cash"] = 999.88
    lesson = deterministic_review(bundle).lessons[0]
    assert lesson.agent == "cfo" and lesson.type == "memory"
    assert lesson.scope == {"carrier_id": "carrier-varied", "claim_type": "detention", "skill": SKILL}
    return {"id": "lesson-carrier-varied", "agent": "cfo", "scope": json.dumps(lesson.scope),
            "text": lesson.text_or_diff, "version": 1, "active": True}


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """A W2-enabled environment whose memory table holds the Validator's lesson."""
    rows = [validator_lesson(),
            {"id": "skill-wide", "agent": "cfo", "scope": json.dumps({"skill": SKILL}),
             "text": "Recompute totals in exact cents.", "version": 1, "active": True}]
    dataset = {"cases": {
        "case-same": {"carrier_id": "carrier-varied", "claim_type": "detention", "lane": "OAK-PHX"},
        "case-other": {"carrier_id": "carrier-other", "claim_type": "detention"},
    }}
    path = tmp_path / "source.json"
    path.write_text(json.dumps(dataset))
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    reads = []

    async def query_rows(self, sql):
        assert sql == "SELECT * FROM agent_memory WHERE agent = 'cfo' AND active = true"
        reads.append(sql)
        return [dict(row) for row in rows]

    # The real get_agent_memory matching rule runs over these rows.
    monkeypatch.setattr(SpacetimeClient, "query_rows", query_rows)
    return reads


def envelope(case_id):
    return TaskEnvelope(task_type=SKILL, case_id=case_id)


def live_reads(monkeypatch):
    async def config(self, agent):
        return SimpleNamespace(enabled=True, model="openai/test")

    async def history(self, agent):
        return []

    monkeypatch.setattr(SpacetimeClient, "get_agent_config", config)
    monkeypatch.setattr(SpacetimeClient, "get_instruction_history", history)


def test_memory_scope_is_the_validator_write_scope():
    source = {"carrier_id": "C-311", "lane": "", "claim_type": "detention", "amount": 10}
    assert memory_scope(source, SKILL) == {"carrier_id": "C-311", "claim_type": "detention", "skill": SKILL}
    assert memory_scope(None, SKILL) == {"skill": SKILL}


async def test_unavailable_source_falls_back_to_skill_only_scope(stored):
    assert await task_memory_scope(envelope("case-missing")) == {"skill": SKILL}
    assert await task_memory_scope(envelope("case-same")) == {
        "carrier_id": "carrier-varied", "lane": "OAK-PHX", "claim_type": "detention", "skill": SKILL}


async def tool_loop_memory_ids(monkeypatch, case_id):
    live_reads(monkeypatch)
    monkeypatch.setattr(tool_loop, "mode", lambda: "live")
    monkeypatch.setattr(tool_loop, "configuration", lambda *_: ProviderConfig(
        "openai", "test", "key", "https://api.openai.com/v1"))
    settings = await tool_loop._settings(Agent(), envelope(case_id))
    return [memory["id"] for memory in settings["memories"]]


async def test_tool_loop_loads_validator_lesson_for_same_carrier_claim_and_skill(stored, monkeypatch):
    assert await tool_loop_memory_ids(monkeypatch, "case-same") == ["lesson-carrier-varied", "skill-wide"]


async def test_tool_loop_never_loads_another_carriers_lesson(stored, monkeypatch):
    assert await tool_loop_memory_ids(monkeypatch, "case-other") == ["skill-wide"]
    assert await tool_loop_memory_ids(monkeypatch, "case-missing") == ["skill-wide"]


async def test_infer_loads_validator_lesson_in_fixture_mode_when_w2_is_enabled(stored):
    result = await infer("cfo", envelope("case-same"), {})
    assert result["simulated"] is True
    assert result["memory_ids"] == ["lesson-carrier-varied", "skill-wide"]


async def test_infer_never_loads_another_carriers_lesson(stored):
    assert (await infer("cfo", envelope("case-other"), {}))["memory_ids"] == ["skill-wide"]
    # Handler findings cannot widen the scope to a carrier the source record does not name.
    other = await infer("cfo", envelope("case-other"), {"carrier_id": "carrier-varied"})
    assert other["memory_ids"] == ["skill-wide"]


async def test_infer_reads_no_memory_when_w2_is_disabled(stored, monkeypatch):
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    assert (await infer("cfo", envelope("case-same"), {}))["memory_ids"] == []
    assert stored == []


async def test_fixture_memory_outage_is_advisory_but_live_outage_is_not(stored, monkeypatch):
    async def outage(self, sql):
        raise SpacetimeTransportError(timeout=True, may_have_committed=False)

    monkeypatch.setattr(SpacetimeClient, "query_rows", outage)
    assert (await infer("cfo", envelope("case-same"), {}))["memory_ids"] == []
    live_reads(monkeypatch)
    monkeypatch.setattr("common.providers.mode", lambda: "live")
    with pytest.raises(SpacetimeTransportError):
        await infer("cfo", envelope("case-same"), {})
