"""Model tools cannot widen role authority or replace ingested source facts."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.carrier_ap.main import CarrierAPAgent
from common.config import AGENTS
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.runtime import FreightAgent
from common.runtime_store import RuntimeStore
from common.tool_registry import ToolContext, registry_for


@pytest.fixture(autouse=True)
def fixture_environment(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    monkeypatch.delenv("SOURCE_DATA_PATH", raising=False)
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.delenv("CARTESIA_WEBHOOK_TOOLS_ENABLED", raising=False)


def context(role, skill=None, subject=None, agent=None):
    env = TaskEnvelope(task_type=skill or AGENTS[role].skills[0], case_id="CASE-TOOLS", subject=subject or {})
    return ToolContext(agent or SimpleNamespace(name=role), env,
                       DelegationMeta(case_id=env.case_id, from_agent="router"), "tool-task")


def source():
    return {"case_id": "CASE-TOOLS", "load_id": "LOAD-TOOLS", "invoice_id": "INV-TOOLS",
        "carrier_id": "CARRIER-TOOLS", "shipper_id": "SHIPPER-TOOLS", "shipper_invoice_id": "AR-TOOLS",
        "invoice_amount": 721.25, "contract_rate": 721.25, "shipper_invoice_amount": 950,
        "period": "2026-10", "paid_amount": 0, "evidence_refs": ["source:tools"],
        "documents": [{"kind": "rate_confirmation", "ref": "source:rate"},
                      {"kind": "pod", "ref": "source:pod", "signed": True}]}


@pytest.mark.parametrize("role", AGENTS)
def test_every_role_has_narrow_tools_and_guarded_completion(role):
    tools = registry_for(context(role))
    assert {"read_load_record", "integration_availability", "execute_current_skill"}.issubset(tools)
    assert len(tools) >= 5
    assert not {"sql", "mutate", "shell", "transfer", "send_email"}.intersection(tools)
    for tool in tools.values():
        assert tool.parameters["additionalProperties"] is False
        assert set(tool.parameters["required"]) == set(tool.parameters["properties"])
    if role != "validator":
        assert "check_review_quality" not in tools
    if role != "voice_desk":
        assert "read_inbound_transcript" not in tools


@pytest.mark.parametrize("arguments", [None, [], {"case_id": "other"}, {"amount": 1}, {"phone": "+15555550000"}])
def test_tool_parameters_reject_attempts_to_override_context(arguments):
    tool = registry_for(context("carrier_ap"))["execute_current_skill"]
    with pytest.raises((TypeError, ValueError)):
        tool.validate(arguments)


def test_role_cannot_execute_another_roles_skill():
    with pytest.raises(ValueError):
        registry_for(context("cfo", "propose_payment"))
    with pytest.raises(ValueError):
        registry_for(context("shipper_ar"))["delegate_specialist"].validate({"skill": "place_call"})


async def test_live_tool_reads_authoritative_amounts_and_no_raw_bank_details(tmp_path, monkeypatch):
    actual = {**source(), "bank_account": "SECRET-ACCOUNT"}
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"synthetic": True, "cases": {"CASE-TOOLS": actual}}))
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    ctx = context("carrier_ap", subject={"invoice_amount": 1, "contract_rate": 1})
    tools = registry_for(ctx)
    result = await tools["match_carrier_invoice"].handler(ctx, {})
    assert result["invoice_amount"] == 721.25
    assert result["source_simulated"] is True
    record = await tools["read_load_record"].handler(ctx, {})
    assert "SECRET-ACCOUNT" not in json.dumps(record)


async def test_delegation_preserves_case_and_reuses_mandatory_workflow_dependency():
    calls = []

    async def delegate(skill, env, meta, task_id, *, invocation_key):
        calls.append((skill, env, meta, task_id, invocation_key))
        return [{"outcome": "verified"}]

    ctx = context("shipper_ar", "chase_overdue", source(), SimpleNamespace(name="shipper_ar", delegate=delegate))
    tool = registry_for(ctx)["delegate_specialist"]
    result = await tool.handler(ctx, tool.validate({"skill": "assess_financial_impact"}))
    assert result["agent"] == "cfo"
    assert calls[0][1].case_id == ctx.env.case_id
    assert calls[0][1].task_type == "assess_financial_impact"
    assert calls[0][4] == "receivable_cash_impact"
    with pytest.raises(ValueError):
        await tool.handler(ctx, {"skill": "propose_payment"})


def test_risk_delegation_only_advertises_the_assigned_verification():
    ctx = context("risk", "screen_bank_change")
    tool = registry_for(ctx)["delegate_specialist"]
    assert set(tool.parameters["properties"]["skill"]["enum"]) == {"verify_bank_change", "check_document_packet"}
    with pytest.raises(ValueError):
        tool.validate({"skill": "verify_dispute"})


async def test_guarded_completion_preserves_active_hold_payment_gate():
    store = RuntimeStore("sqlite+aiosqlite:///:memory:")
    agent = CarrierAPAgent(store=store)
    ctx = context("carrier_ap", "propose_payment", {**source(), "holds": [{"status": "active"}]}, agent)
    try:
        result = await registry_for(ctx)["execute_current_skill"].handler(ctx, {})
        assert result["artifact"]["outcome"] == "hold"
        assert result["artifact"]["findings"]["payment_blocked"] is True
        assert "payment_proposal" not in result["artifact"]["findings"]
        assert "P-002" in result["artifact"]["policy_ids"]
    finally:
        await store.close()


async def test_deposit_matching_rejects_repeated_ids():
    deposits = [{"id": "DEP-1", "invoice_id": "AR-TOOLS", "shipper_id": "SHIPPER-TOOLS", "amount": 100}] * 2
    ctx = context("shipper_ar", subject={**source(), "deposits": deposits})
    with pytest.raises(ValueError, match="duplicate"):
        await registry_for(ctx)["match_customer_deposits"].handler(ctx, {})


async def test_outbound_call_without_configuration_is_unavailable_not_a_mock(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    monkeypatch.setenv("MOCK_INTEGRATIONS", "1")

    async def forbidden_workflow(*args):
        raise AssertionError("unconfigured live call must not reach any provider")

    ctx = context("voice_desk", "place_call", {"purpose": "verification"},
                  SimpleNamespace(name="voice_desk", run_workflow=forbidden_workflow))
    tools = registry_for(ctx)
    availability = await tools["integration_availability"].handler(ctx, {})
    assert availability["outbound_voice"]["available"] is False
    assert availability["external_carrier_lookup"]["available"] is False
    result = await tools["execute_current_skill"].handler(ctx, {})
    assert result["artifact"]["outcome"] == "unverified"
    assert result["artifact"]["findings"]["outbound_call_placed"] is False
    assert "CARTESIA_API_KEY" in result["artifact"]["findings"]["missing_configuration"]


@pytest.mark.parametrize("callback", ["https://your-tunnel.example.com", "http://live-tunnel.app",
                                    "https://localhost", "https://operator:secret@live-tunnel.app"])
async def test_voice_availability_rejects_placeholder_or_invalid_callback(monkeypatch, callback):
    monkeypatch.setenv("CARTESIA_WEBHOOK_TOOLS_ENABLED", "1")
    for key in ("CARTESIA_API_KEY", "CARTESIA_AGENT_ID_VERIFICATION", "CARTESIA_FROM_NUMBER_ID",
                "CARTESIA_WEBHOOK_SECRET"):
        monkeypatch.setenv(key, "configured-test-value")
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", "+14155551234")
    monkeypatch.setenv("PUBLIC_BASE_URL", callback)
    ctx = context("voice_desk", "place_call", {"purpose": "verification"})
    result = await registry_for(ctx)["integration_availability"].handler(ctx, {})
    assert result["outbound_voice"]["available"] is False
    assert result["outbound_voice"]["invalid_configuration"] == ["PUBLIC_BASE_URL"]
    assert "configured-test-value" not in json.dumps(result)


async def test_voice_availability_requires_nonempty_demo_allowlist(monkeypatch):
    for key in ("CARTESIA_API_KEY", "CARTESIA_AGENT_ID_VERIFICATION", "CARTESIA_FROM_NUMBER_ID",
                "CARTESIA_WEBHOOK_SECRET"):
        monkeypatch.setenv(key, "configured-test-value")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://live-tunnel.app")
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", " , ")
    ctx = context("voice_desk", "place_call", {"purpose": "verification"})
    result = await registry_for(ctx)["integration_availability"].handler(ctx, {})
    assert result["outbound_voice"]["available"] is False
    assert result["outbound_voice"]["missing_configuration"] == ["DEMO_ALLOWLIST_PHONES"]


async def test_carrier_tools_preserve_all_three_mandatory_dependency_slots():
    store = RuntimeStore("sqlite+aiosqlite:///:memory:")
    agent = CarrierAPAgent(store=store)
    seen = {}

    async def delegate(skill, env, meta, task_id, *, invocation_key):
        seen.setdefault(invocation_key, skill)
        assert len(seen) <= 3, "optional model checks must not consume required delegation capacity"
        outcome = {"collect_documents": "verified", "investigate_charge": "suspicious", "review_hold": "hold"}
        return [{"outcome": outcome[skill]}]

    agent.delegate = delegate
    ctx = context("carrier_ap", subject={**source(), "invoice_amount": 950, "documents": []}, agent=agent)
    tools = registry_for(ctx)
    try:
        assert "delegate_specialist" not in tools
        await tools["inspect_document_packet"].handler(ctx, {})
        result = await tools["execute_current_skill"].handler(ctx, {})
        assert result["artifact"]["outcome"] == "hold"
        assert seen == {"settlement_packet": "collect_documents", "charge_audit": "investigate_charge",
                        "settlement_decision": "review_hold"}
    finally:
        await store.close()


def test_collections_only_offers_the_workflows_reusable_cash_dependency():
    tool = registry_for(context("shipper_ar", "chase_overdue"))["delegate_specialist"]
    assert tool.parameters["properties"]["skill"]["enum"] == ["assess_financial_impact"]
    assert "delegate_specialist" not in registry_for(context("shipper_ar", "set_credit_limit"))


def test_external_tools_are_role_scoped_and_do_not_accept_arbitrary_targets():
    for role in AGENTS:
        tools = registry_for(context(role))
        assert ("lookup_carrier_authority" in tools) == (role in {"verification", "risk", "carrier_ap"})
        assert ("inspect_company_website" in tools) == (role in {"verification", "documents", "shipper_ar"})
        for name in ("lookup_carrier_authority", "inspect_company_website"):
            if name in tools:
                with pytest.raises(ValueError):
                    tools[name].validate({"url": "https://attacker.invalid", "usdot": "999"})


class ExternalToolAgent(FreightAgent):
    def __init__(self, name, outcome="verified"):
        super().__init__(store=RuntimeStore("sqlite+aiosqlite:///:memory:"))
        self.name, self.outcome = name, outcome

    async def run_workflow(self, env, meta, task_id, updater):
        return ResultArtifact(outcome=self.outcome, evidence_refs=["source:tools"],
                              findings={"bank_details_changed": False})


def live_source(monkeypatch, tmp_path, *, carrier=None, shipper=None):
    path = tmp_path / "external-source.json"
    path.write_text(json.dumps({"cases": {"CASE-TOOLS": source()}, "companies": {
        "CARRIER-TOOLS": {"id": "CARRIER-TOOLS", **(carrier or {})},
        "SHIPPER-TOOLS": {"id": "SHIPPER-TOOLS", **(shipper or {})},
    }}))
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    return path


async def test_apify_lookup_binds_ingested_identifiers_and_freezes_observation(monkeypatch, tmp_path):
    from integrations import apify
    path = live_source(monkeypatch, tmp_path, carrier={"usdot": "123456", "mc": "MC123", "verified": False,
                                                       "name": "Source Haul", "legal_name": "SOURCE HAUL LLC"})
    before = path.read_text()
    lookup = AsyncMock(return_value={"available": True, "identity_match": True,
        "records": [{"usdot": "123456", "authority_confirmed_active": True}],
        "evidence_refs": ["apify://runs/test-run"], "contact_authority_verified": True,
        "financial_authorization": True})
    monkeypatch.setattr(apify, "lookup_carrier", lookup)
    agent = ExternalToolAgent("verification", outcome="unverified")
    ctx = context("verification", "verify_dispute", {"usdot": "999", "mc": "MC999"}, agent)
    try:
        tools = registry_for(ctx)
        first = await tools["lookup_carrier_authority"].handler(ctx, {})
        assert await tools["lookup_carrier_authority"].handler(ctx, {}) == first
        lookup.assert_awaited_once_with(agent, ctx.env, ctx.task_id, usdot="123456", mc="MC123",
                                        names=("SOURCE HAUL LLC", "Source Haul"))
        assert not first["contact_authority_verified"] and not first["financial_authorization"]
        artifact = (await tools["execute_current_skill"].handler(ctx, {}))["artifact"]
        assert artifact["outcome"] == "unverified", "public evidence cannot promote domain authorization"
        assert artifact["findings"]["external_checks"]["apify_carrier"] == first
        assert "apify://runs/test-run" in artifact["evidence_refs"]
        assert path.read_text() == before, "scraped records never overwrite the verified directory"
    finally:
        await agent.store.close()


@pytest.mark.parametrize("external,expected_signal", [
    ({"available": False, "records": []}, "external_carrier_lookup_unavailable"),
    ({"available": True, "identity_match": False, "records": []}, "external_carrier_identity_mismatch"),
    ({"available": True, "records": [{"authority_confirmed_active": False}]}, "external_carrier_authority_not_active"),
    ({"available": True, "records": [{"authority_confirmed_active": None}]}, "external_carrier_authority_unconfirmed"),
    # Another company's active authority under the same USDOT confirms nothing about this carrier.
    ({"available": True, "identity_match": False,
      "records": [{"authority_confirmed_active": True, "identity_match": False}]},
     "external_carrier_authority_unconfirmed"),
    ({"available": True, "records": [{"authority_confirmed_active": True, "out_of_service": True}]},
     "external_carrier_out_of_service"),
])
@pytest.mark.parametrize("role,skill,before,after", [
    ("verification", "verify_dispute", "verified", "unverified"),
    ("risk", "screen_counterparty", "clear", "suspicious"),
])
async def test_failed_or_negative_fmcsa_observation_cannot_return_verified(
        monkeypatch, tmp_path, external, expected_signal, role, skill, before, after):
    from integrations import apify
    live_source(monkeypatch, tmp_path, carrier={"usdot": "123456"})
    monkeypatch.setattr(apify, "lookup_carrier", AsyncMock(return_value=external))
    agent = ExternalToolAgent(role, outcome=before)
    ctx = context(role, skill, agent=agent)
    try:
        tools = registry_for(ctx)
        await tools["lookup_carrier_authority"].handler(ctx, {})
        artifact = (await tools["execute_current_skill"].handler(ctx, {}))["artifact"]
        assert artifact["outcome"] == after
        assert expected_signal in artifact["findings"]["external_risk_signals"]
        assert artifact["findings"]["bank_details_changed"] is False
    finally:
        await agent.store.close()


async def test_missing_carrier_identifier_reports_unavailable_without_network(monkeypatch, tmp_path):
    from integrations import apify
    live_source(monkeypatch, tmp_path)
    lookup = AsyncMock(side_effect=AssertionError("missing source identifier must not call Apify"))
    monkeypatch.setattr(apify, "lookup_carrier", lookup)
    agent = ExternalToolAgent("verification")
    ctx = context("verification", "verify_dispute", {"usdot": "forged"}, agent)
    try:
        result = await registry_for(ctx)["lookup_carrier_authority"].handler(ctx, {})
        assert result["available"] is False and result["missing_source_fields"]
        lookup.assert_not_awaited()
    finally:
        await agent.store.close()


@pytest.mark.parametrize("first_error", ["apify_run_pending", "apify_http_503"])
async def test_new_tool_call_can_resume_pending_lookup_then_freeze_terminal_evidence(
        monkeypatch, tmp_path, first_error):
    from integrations import apify
    live_source(monkeypatch, tmp_path, carrier={"usdot": "123456"})
    pending = {"available": False, "error": first_error, "run_id": "same-paid-run",
               "can_resume_lookup": True, "evidence_refs": []}
    complete = {"available": True, "run_id": "same-paid-run", "identity_match": True,
                "records": [{"usdot": "123456", "authority_confirmed_active": True}],
                "evidence_refs": ["apify://runs/same-paid-run"]}
    lookup = AsyncMock(side_effect=[pending, complete])
    monkeypatch.setattr(apify, "lookup_carrier", lookup)
    agent = ExternalToolAgent("verification")
    ctx = context("verification", "verify_dispute", agent=agent)
    try:
        tools = registry_for(ctx)
        first = await tools["lookup_carrier_authority"].handler(ctx, {})
        assert first["can_resume_lookup"] is True
        assert await agent.store.get_step(ctx.task_id, "external:apify_carrier") is None
        unfinished = (await tools["execute_current_skill"].handler(ctx, {}))["artifact"]
        assert unfinished["outcome"] == "unverified"
        assert unfinished["findings"]["external_checks"]["apify_carrier"]["error"] == "external_lookup_incomplete"
        second = await tools["lookup_carrier_authority"].handler(ctx, {})
        assert second["available"] is True and second["run_id"] == first["run_id"]
        assert await tools["lookup_carrier_authority"].handler(ctx, {}) == second
        assert lookup.await_count == 2, "only pending observations poll again; terminal data stays frozen"
        finished = (await tools["execute_current_skill"].handler(ctx, {}))["artifact"]
        assert finished["outcome"] == "verified"
        assert finished["findings"]["external_checks"]["apify_carrier"]["available"] is True
        assert "apify://runs/same-paid-run" in finished["evidence_refs"]
    finally:
        await agent.store.close()


async def test_company_scrape_uses_ingested_shippers_website_not_task_url(monkeypatch, tmp_path):
    from integrations import apify
    live_source(monkeypatch, tmp_path, carrier={"website": "https://carrier-company.com"},
                shipper={"website": "https://shipper-company.com"})
    scrape = AsyncMock(return_value={"available": True, "records": [{"emails": ["billing@shipper-company.com"]}],
        "evidence_refs": ["https://shipper-company.com"], "contact_authority_verified": False})
    monkeypatch.setattr(apify, "scrape_company", scrape)
    agent = ExternalToolAgent("shipper_ar", outcome="clear")
    ctx = context("shipper_ar", "chase_overdue", {"website": "https://attacker.invalid"}, agent)
    try:
        result = await registry_for(ctx)["inspect_company_website"].handler(ctx, {})
        scrape.assert_awaited_once_with(agent, ctx.env, ctx.task_id, url="https://shipper-company.com")
        assert result["financial_authorization"] is False
        assert result["contact_authority_verified"] is False
    finally:
        await agent.store.close()


async def test_readiness_uses_role_purpose_and_is_not_a_phone_call(monkeypatch):
    from common import voice
    observed = []

    def status(purpose):
        observed.append(purpose)
        return {"available": True, "missing_configuration": [], "invalid_configuration": [],
                "completion_mode": "polling"}

    monkeypatch.setattr(voice, "voice_configuration_status", status)
    ctx = context("shipper_ar", "chase_overdue", {"purpose": "approvals"})
    result = await registry_for(ctx)["inspect_outreach_readiness"].handler(ctx, {})
    assert observed == ["collections"] and result["purpose"] == "collections"
    assert result["call_placed"] is False


async def test_integration_availability_exposes_apify_configuration_and_deferred_sponsors(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "test-secret-do-not-display")
    ctx = context("verification", "verify_dispute")
    result = await registry_for(ctx)["integration_availability"].handler(ctx, {})
    assert result["external_carrier_lookup"]["available"] is True
    assert result["company_website_lookup"]["available"] is True
    assert result["external_credit_and_sanctions"]["available"] is False
    assert result["bank_transfer"]["available"] is False
    assert "test-secret-do-not-display" not in json.dumps(result)


@pytest.mark.parametrize("skill,expected", [("process_carrier_invoice", ("lookup_carrier_authority",)),
                                            ("propose_payment", ("lookup_carrier_authority",)),
                                            ("answer_payment_status", ())])
def test_carrier_settlement_checks_the_carrier_first_only_when_settling(skill, expected):
    from common.tool_registry import first_calls
    ctx = context("carrier_ap", skill)
    registry = registry_for(ctx)
    assert "lookup_carrier_authority" in registry
    assert first_calls(ctx, registry) == expected
    # A requirement is never imposed for a tool the role was not granted.
    assert first_calls(ctx, {}) == ()
