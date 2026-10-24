"""Cartesia contract and recovery checks without dialing or provider credentials."""
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from common import voice
from common.envelope import DelegationMeta, TaskEnvelope


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("A2A_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'calls.sqlite'}")
    for key, value in {
        "CARTESIA_API_KEY": "secret-test-key", "CARTESIA_AGENT_ID_VERIFICATION": "agent_test",
        "CARTESIA_FROM_NUMBER_ID": "ap_test", "DEMO_ALLOWLIST_PHONES": "+15005550006",
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
    monkeypatch.setattr(voice.SpacetimeClient, "mutate", AsyncMock())
    return TaskEnvelope(task_type="place_call", case_id="case", subject={
        "purpose": "verification", "counterparty_id": "carrier", "questions": ["Was the load delivered?"],
        "facts": {"load_id": "load"}})


def test_polling_voice_configuration_needs_no_public_callback(configured, monkeypatch):
    monkeypatch.delenv("CARTESIA_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://your-tunnel.example.com")
    status = voice.voice_configuration_status("verification")
    assert status["available"] and status["completion_mode"] == "polling"
    assert "secret-test-key" not in json.dumps(status)
    assert voice.call_config("verification")[1:] == ("agent_test", "ap_test")
    monkeypatch.setenv("CARTESIA_WEBHOOK_TOOLS_ENABLED", "1")
    status = voice.voice_configuration_status("verification")
    assert not status["available"]
    assert "CARTESIA_WEBHOOK_SECRET" not in status["missing_configuration"]
    assert "PUBLIC_BASE_URL" in status["invalid_configuration"]


@pytest.mark.parametrize("target", ["+15555550100", "5550100", "http://localhost", "+15005550006,invalid"])
def test_placeholder_and_invalid_phone_targets_are_not_ready(configured, monkeypatch, target):
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", target)
    assert "DEMO_ALLOWLIST_PHONES" in voice.voice_configuration_status("verification")["invalid_configuration"]


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))


async def test_outbound_contract_uses_only_demo_target_and_never_redials(configured, monkeypatch):
    requests = []

    def response(request):
        requests.append(request)
        assert str(request.url) == "https://api.cartesia.ai/agents/calls"
        assert request.headers["Cartesia-Version"] == "2026-08-14"
        body = json.loads(request.content)
        assert body["agent_id"] == "agent_test" and body["from_number_id"] == "ap_test"
        assert body["outbound_calls"][0]["to_number"] == "+15005550006"
        variables = body["outbound_calls"][0]["dynamic_variables"]
        assert variables["case_id"] == "case" and variables["task_id"] == "task"
        assert variables["call_token"] and variables["attempt_id"]
        return httpx.Response(200, json={"calls": [{"agent_call_id": "ac_test"}]})

    mock_http(monkeypatch, response)
    meta = DelegationMeta(case_id="case", from_agent="verification")
    attempt_id, result = await voice.start_call(configured, meta, "task")
    assert result is None
    assert "team_demo_authorized" not in voice.SpacetimeClient.mutate.await_args.args[1]
    again, result = await voice.start_call(configured, meta, "task")
    assert again == attempt_id and result is None and len(requests) == 1
    store = voice.CallStore()
    await store.initialize()
    row = await store.get(attempt_id)
    assert row["provider_id"] == "ac_test" and row["status"] == "ringing"
    assert "call_token" not in json.loads(row["payload"])
    await store.close()


async def test_outbound_voice_receives_complete_source_brief_not_supplied_facts(configured, monkeypatch):
    source_path = Path(os.environ["SOURCE_DATA_PATH"])
    data = json.loads(source_path.read_text())
    scenario = json.loads((Path(__file__).resolve().parents[1] / "sim/fixtures/team_call_source.json").read_text())
    record = scenario["cases"]["SYN-CARTESIA-TEAM-CALL"]
    record.update(case_id="case", carrier_id="carrier")
    data["cases"]["case"] = record
    source_path.write_text(json.dumps(data))
    configured.subject["facts"] = {"load_reference": "FORGED", "invoice_amount": 999999,
                                   "bank_account": "never-forward-this", "email": "fake@example.test"}
    captured = {}

    def response(request):
        variables = json.loads(request.content)["outbound_calls"][0]["dynamic_variables"]
        captured.update(variables)
        return httpx.Response(200, json={"calls": [{"agent_call_id": "ac_context"}]})

    mock_http(monkeypatch, response)
    attempt_id, _ = await voice.start_call(configured, DelegationMeta(case_id="case", from_agent="verification"), "context-task")
    facts = json.loads(captured["facts"])
    assert captured["load_reference"] == facts["load_reference"] == "GG-1042"
    assert facts["route"]["origin"]["city"] == "Oakland"
    assert facts["route"]["destination"]["city"] == "Sacramento"
    assert facts["cargo"]["pallets"] == 18 and facts["invoice"]["amount"] == 1225
    assert facts["claim"]["supported_amount"] == 125 and facts["claim"]["billable_minutes"] == 75
    assert facts["contacts"]["email"] is None and facts["synthetic"] is True
    assert all(value not in captured["facts"] for value in ("FORGED", "never-forward-this", "fake@example.test"))
    store = voice.CallStore()
    try:
        assert json.loads((await store.get(attempt_id))["payload"])["facts"] == facts
    finally:
        await store.close()


@pytest.mark.parametrize("status,body", [(200, {"calls": [{"error": "private provider detail"}]}),
                                          (403, {"error": "private provider detail"})])
async def test_explicit_provider_rejection_is_terminal_not_uncertain(configured, monkeypatch, status, body):
    mock_http(monkeypatch, lambda _: httpx.Response(status, json=body))
    attempt_id, result = await voice.start_call(configured, DelegationMeta(case_id="case", from_agent="verification"), "reject")
    assert not result.reached and result.findings["call_accepted"] is False
    store = voice.CallStore()
    await store.initialize()
    row = await store.get(attempt_id)
    assert row["status"] == "failed" and row["result"]
    assert "private provider detail" not in row["result"]
    await store.close()


async def test_uncertain_attempt_recovers_by_capability_without_redial(configured, monkeypatch):
    captured = {}

    def timeout(request):
        captured.update(json.loads(request.content)["outbound_calls"][0]["dynamic_variables"])
        raise httpx.ReadTimeout("private timeout detail")

    mock_http(monkeypatch, timeout)
    attempt_id, result = await voice.start_call(configured, DelegationMeta(case_id="case", from_agent="verification"), "uncertain")
    assert result is None
    store = voice.CallStore()
    await store.initialize()
    row = await store.get(attempt_id)
    assert row["status"] == "uncertain"
    read = AsyncMock(return_value={"data": [
        {"id": "ac_wrong", "dynamic_variables": {**captured, "call_token": "forged"}},
        {"id": "ac_right", "dynamic_variables": captured}], "has_more": False})
    monkeypatch.setattr(voice, "cartesia_get", read)
    fetch = AsyncMock(return_value={"id": "ac_right", "status": "completed", "transcript": []})
    monkeypatch.setattr(voice, "fetch_call", fetch)
    assert (await voice.recover_uncertain_call(store, row))["id"] == "ac_right"
    assert (await store.get(attempt_id))["provider_id"] == "ac_right"
    assert read.call_args.args == ("/agents/calls",)
    assert read.call_args.kwargs["params"]["agent_id"] == "agent_test"
    await store.close()


async def test_assistant_only_or_null_transcript_never_claims_contact_reached(configured, monkeypatch):
    store = voice.CallStore()
    await store.initialize()
    row = await store.prepare("silent", configured, "silent", {
        "questions": [], "intended_to": "+14155550123", "routed_to": "+15005550006"}, "token")
    await store.set("silent", provider_id="ac_silent")
    row = await store.get("silent")
    infer = AsyncMock()
    monkeypatch.setattr(voice, "infer", infer)
    assert await voice.finish_call(store, row, {"id": "ac_silent", "status": "completed", "transcript": [
        {"role": "assistant", "text": "Hi, I am an AI assistant."}, {"role": "user", "text": None}]})
    row = await store.get("silent")
    assert row["status"] == "no_answer" and not json.loads(row["result"])["reached"]
    infer.assert_not_awaited()
    await store.close()


async def test_provider_read_error_does_not_expose_response_body(configured, monkeypatch):
    mock_http(monkeypatch, lambda _: httpx.Response(401, json={"error": "secret-test-key"}))
    with pytest.raises(voice.VoiceError, match="Cartesia HTTP 401") as failure:
        await voice.fetch_call("ac_test")
    assert "secret-test-key" not in str(failure.value)


async def test_case_unrelated_counterparty_cannot_be_dialed(configured, monkeypatch):
    source = AsyncMock(side_effect=AssertionError("must reject before contacting source directory"))
    monkeypatch.setattr("common.source_repository.SourceRepository.get_company", source)
    env = configured.model_copy(update={"subject": {**configured.subject, "counterparty_id": "other"}})
    with pytest.raises(voice.VoiceError, match="assigned case"):
        await voice.start_call(env, DelegationMeta(case_id="case", from_agent="verification"), "wrong-case")
    source.assert_not_awaited()


async def test_gateway_polling_recovers_uncertain_call_and_delivers_same_task(configured, monkeypatch):
    import importlib
    gateway = importlib.import_module("gateway.app")
    store = voice.CallStore()
    await store.initialize()
    await store.prepare("lost-response", configured, "original-task", {
        "questions": [], "intended_to": "+14155550123", "routed_to": "+15005550006", "persona": "agent_test",
    }, "token")
    await store.set("lost-response", status="uncertain")

    async def recover(store, row):
        assert row["task_id"] == "original-task" and not row["provider_id"]
        await store.set(row["id"], provider_id="ac_recovered")
        return {"id": "ac_recovered", "status": "completed", "end_reason": "dial_no_answer", "transcript": []}

    recovered = AsyncMock(side_effect=recover)
    delivered = AsyncMock()
    monkeypatch.setattr(gateway, "recover_uncertain_call", recovered)
    monkeypatch.setattr(gateway, "deliver_result", delivered)
    await gateway.reconcile_once(store)
    recovered.assert_awaited_once()
    delivered.assert_awaited_once()
    row = delivered.await_args.args[1]
    assert row["task_id"] == "original-task" and row["provider_id"] == "ac_recovered"
    assert row["status"] == "no_answer" and not json.loads(row["result"])["reached"]
    await store.close()


def test_gateway_rejects_malformed_and_oversized_callback_bodies(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from gateway.app import app
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("A2A_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CARTESIA_WEBHOOK_SECRET", "test-webhook-secret")
    with TestClient(app) as client:
        headers = {"x-webhook-secret": "test-webhook-secret"}
        assert client.post("/voice/webhook", headers=headers, json=[]).status_code == 400
        assert client.post("/voice/webhook", headers=headers, content=b"not JSON").status_code == 400
        assert client.post("/voice/webhook", headers=headers, content=b"x" * 32_001).status_code == 413


@pytest.mark.parametrize("body", [[], {"calls": None}, {"calls": [None]}, {"calls": []},
                                   {"calls": [{"agent_call_id": "ac_wrong", "number": "+14155559999"}]}])
async def test_malformed_creation_response_stays_uncertain_without_redial(configured, monkeypatch, body):
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    mock_http(monkeypatch, response)
    meta = DelegationMeta(case_id="case", from_agent="verification")
    attempt_id, result = await voice.start_call(configured, meta, "malformed")
    assert result is None
    again, result = await voice.start_call(configured, meta, "malformed")
    assert again == attempt_id and result is None and len(requests) == 1
    store = voice.CallStore()
    await store.initialize()
    assert (await store.get(attempt_id))["status"] == "uncertain"
    await store.close()
