"""A call that cannot be placed ends at once, unreached, and its caller decides without it (live, Oct 25 2026).

Voice Desk ran a full tool loop before learning a call was impossible: the live `demo_target` table was empty
(W2 refuses every routed number, P-011), contacts had no timezone, and it was outside calling hours. The guarded
workflow raised, the model retried until `max_messages`, and the task and its parents failed.
"""
import json
from unittest.mock import AsyncMock

import pytest

from agents.voice_desk.main import VoiceDeskAgent, not_reachable
from common import voice
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import ChildFailed
from common.runtime_store import RuntimeStore
from common.spacetime_client import SpacetimeClient, SpacetimeHTTPError

ROUTED = "+15005550006"
META = DelegationMeta(case_id="case", from_agent="verification")


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    monkeypatch.setenv("A2A_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'calls.sqlite'}")
    for key, value in {
        "CARTESIA_API_KEY": "secret-test-key", "CARTESIA_AGENT_ID_VERIFICATION": "agent_test",
        "CARTESIA_FROM_NUMBER_ID": "ap_test", "DEMO_ALLOWLIST_PHONES": ROUTED,
        "CARTESIA_WEBHOOK_TOOLS_ENABLED": "0",
    }.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"cases": {"case": {"carrier_id": "carrier"}}, "companies": {
        "carrier": {"id": "carrier", "phone": "+14155550123", "verified": True,
                    "source": "fmcsa", "source_url": "https://safer.fmcsa.dot.gov/record",
                    "timezone": "America/Los_Angeles"}}}))
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    monkeypatch.setattr(voice, "check_call_window", lambda _: None)
    targets = []

    async def query_rows(_self, sql):
        assert sql == f"SELECT * FROM demo_target WHERE phone = '{ROUTED}'"
        return targets
    monkeypatch.setattr(SpacetimeClient, "query_rows", query_rows)
    env = TaskEnvelope(task_type="place_call", case_id="case", subject={
        "purpose": "verification", "counterparty_id": "carrier", "questions": ["Did you ask for it?"]})
    return env, targets


async def test_preflight_names_the_ledger_routing_gate(configured):
    env, targets = configured
    assert "demo_target" in await voice.call_preflight(env, META)
    targets.append({"phone": ROUTED, "label": "team", "enabled": False})
    assert "demo_target" in await voice.call_preflight(env, META)
    targets[0]["enabled"] = True
    assert await voice.call_preflight(env, META) is None


async def test_preflight_names_hours_configuration_and_directory(configured, monkeypatch):
    env, targets = configured
    targets.append({"phone": ROUTED, "enabled": True})

    def closed(_company):
        raise voice.CallRefused("outreach is outside the configured calling hours")
    monkeypatch.setattr(voice, "check_call_window", closed)
    assert await voice.call_preflight(env, META) == "outreach is outside the configured calling hours"
    monkeypatch.setattr(voice, "check_call_window", lambda _: None)
    unknown = env.model_copy(update={"subject": {**env.subject, "counterparty_id": "stranger"}})
    assert "assigned case" in await voice.call_preflight(unknown, META)
    monkeypatch.delenv("CARTESIA_API_KEY")
    assert await voice.call_preflight(env, META) == \
        "outbound voice configuration is incomplete or invalid: CARTESIA_API_KEY"
    monkeypatch.setenv("CARTESIA_API_KEY", "secret-test-key")
    forged = DelegationMeta(case_id="case", from_agent="cfo")
    assert "purpose" in await voice.call_preflight(env, forged)


async def test_preflight_leaves_a_transient_read_failure_to_the_call(configured, monkeypatch):
    env, _ = configured

    async def outage(_self, _sql):
        raise RuntimeError("Maincloud unreachable")
    monkeypatch.setattr(SpacetimeClient, "query_rows", outage)
    assert await voice.call_preflight(env, META) is None


async def test_an_unreachable_call_ends_before_any_model_turn(configured, monkeypatch, tmp_path):
    env, _ = configured
    tool_loop = AsyncMock(side_effect=AssertionError("no model turn for an impossible call"))
    monkeypatch.setattr("common.tool_loop.run_tool_loop", tool_loop)
    agent = VoiceDeskAgent(store=RuntimeStore(f"sqlite+aiosqlite:///{tmp_path}/runtime.sqlite3"))
    try:
        result = await agent.handle(env, META, "task", None)
        assert result.outcome == "unverified" and result.findings["reachable"] is False
        assert result.findings["call_result"]["reached"] is False
        assert "demo_target" in result.findings["call_result"]["findings"]["reason"]
        tool_loop.assert_not_awaited()
        # Decided once: a resumed call wait never re-decides reachability.
        assert (await agent.store.get_step("task", "call_preflight"))["value"] == result.findings["reason"]
    finally:
        await agent.store.close()


async def test_a_call_the_ledger_refuses_ends_unreached_and_is_not_retried(configured, monkeypatch):
    env, _ = configured
    refusal = SpacetimeHTTPError(530, mutation=True)
    refusal.reason = "P-011: routed number is not on the demo allowlist"
    mutation = AsyncMock(side_effect=refusal)
    monkeypatch.setattr(voice.SpacetimeClient, "mutate", mutation)
    attempt, result = await voice.start_call(env, META, "task")
    assert result is not None and result.reached is False
    assert result.findings["end_reason"] == "ledger_refused" and "P-011" in result.findings["reason"]
    again, replayed = await voice.start_call(env, META, "task")
    assert again == attempt and replayed == result and mutation.await_count == 1
    store = voice.CallStore()
    try:
        row = await store.get(attempt)
        assert row["status"] == "refused" and row["delivered"] == "1"
    finally:
        await store.close()


async def test_a_transient_ledger_failure_still_raises(configured, monkeypatch):
    env, _ = configured
    monkeypatch.setattr(voice.SpacetimeClient, "mutate", AsyncMock(side_effect=SpacetimeHTTPError(503, mutation=True)))
    with pytest.raises(SpacetimeHTTPError):
        await voice.start_call(env, META, "task")


async def test_the_guarded_workflow_turns_a_refused_call_into_a_result(monkeypatch, tmp_path):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    agent = VoiceDeskAgent(store=RuntimeStore(f"sqlite+aiosqlite:///{tmp_path}/runtime.sqlite3"))
    env = TaskEnvelope(task_type="place_call", case_id="c", subject={"purpose": "approvals", "counterparty_id": "x"})
    try:
        result = await agent.run_workflow(env, META, "task", None)
        assert result == not_reachable("caller is not permitted to request this call purpose")
    finally:
        await agent.store.close()


async def test_verification_without_a_possible_callback_is_unverified(monkeypatch, tmp_path):
    from agents.verification.main import VerificationAgent
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"cases": {"case": {"carrier_id": "carrier", "bank_change_requested": True}},
        "companies": {"carrier": {"id": "carrier", "name": "Carrier", "phone": "+14155550123", "verified": True,
                                  "source": "fmcsa", "source_url": "https://safer.fmcsa.dot.gov/record",
                                  "authority_status": "ACTIVE", "insurance_verified": True}}}))
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    agent = VerificationAgent(store=RuntimeStore(f"sqlite+aiosqlite:///{tmp_path}/runtime.sqlite3"))
    agent.delegate = AsyncMock(side_effect=ChildFailed("rejected", "voice_desk refused the delegation"))
    env = TaskEnvelope(task_type="verify_bank_change", case_id="case")
    try:
        result = await agent.run_workflow(env, DelegationMeta(case_id="case", from_agent="risk"), "task", None)
        assert result.outcome == "unverified"
        assert result.findings["callback_unavailable"] is True
        assert result.findings["reason"] == "voice_desk refused the delegation"
        assert result.findings["bank_details_changed"] is False and "P-004" in result.policy_ids
    finally:
        await agent.store.close()
