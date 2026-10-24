import json

import httpx
import pytest

from common.envelope import TaskEnvelope
from common.providers import (
    ProviderConfig,
    ProviderConfigurationError,
    ProviderError,
    complete,
    configuration,
    infer,
)


def assessment(ref="source:invoice"):
    return {"summary": "Invoice requires supporting receipt.", "issues": ["missing_receipt"],
            "evidence_refs": [ref], "confidence": 0.8}


async def test_openai_uses_direct_responses_and_strict_schema():
    def respond(request):
        body = json.loads(request.content)
        assert request.url == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer test-secret"
        assert body["store"] is False and body["text"]["format"]["strict"] is True
        assert body["model"] == "configured-model"
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(assessment())}]}],
            "usage": {"input_tokens": 10, "output_tokens": 20}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result, usage = await complete(
            ProviderConfig("openai", "configured-model", "test-secret", "https://api.openai.com/v1"),
            "Evidence only", {"evidence_refs": ["source:invoice"]}, client=client)
    assert result.confidence == 0.8 and usage["input_tokens"] == 10


async def test_anthropic_normalizes_named_tool_result():
    def respond(request):
        body = json.loads(request.content)
        assert request.url == "https://api.anthropic.com/v1/messages"
        assert body["tool_choice"]["name"] == "submit_assessment"
        return httpx.Response(200, json={"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "name": "submit_assessment", "input": assessment()}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result, _ = await complete(
            ProviderConfig("anthropic", "configured-model", "secret", "https://api.anthropic.com/v1"),
            "Evidence only", {"evidence_refs": ["source:invoice"]}, client=client)
    assert result.issues == ["missing_receipt"]


@pytest.mark.parametrize("response", [
    {"status": "incomplete", "output": []},
    {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
    {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(assessment("fabricated:evidence"))}]}]},
    {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": "not json"}]}]},
])
async def test_provider_failures_never_become_success(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as c:
        with pytest.raises(ProviderError):
            await complete(ProviderConfig("openai", "test", "secret", "https://api.openai.com/v1"),
                           "", {"evidence_refs": ["source:invoice"]}, client=c)


def test_live_missing_key_and_unsupported_pin_fail(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("OPENAI_MODEL", "configured-model")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderConfigurationError, match="OPENAI_API_KEY"):
        configuration("carrier_ap")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    with pytest.raises(ProviderConfigurationError, match="catalog"):
        configuration("carrier_ap", "unconfigured-model")


async def test_fixture_inference_is_explicit_and_needs_no_keys(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    result = await infer("carrier_ap", TaskEnvelope(task_type="process_carrier_invoice", case_id="ANY-ID"), {})
    assert result["simulated"] is True and result["model_used"] == "fixture/deterministic"
    assert result["assessment"]["confidence"] == 0


async def test_call_confirmation_requires_an_exact_transcript_quote():
    from common.providers import CallAssessment
    value = {**assessment(), "claim_confirmed": True, "authorized_change": None,
             "supporting_quote": "invented words"}
    response = {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(value)}]}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as c:
        with pytest.raises(ProviderError, match="transcript quote"):
            await complete(ProviderConfig("openai", "test", "secret", "https://api.openai.com/v1"), "",
                           {"evidence_refs": ["source:invoice"], "evidence": {"transcript": "No."}},
                           client=c, response_type=CallAssessment)


async def test_call_confirmation_cannot_quote_the_agents_own_statement():
    from common.providers import CallAssessment
    value = {**assessment(), "claim_confirmed": True, "authorized_change": True,
             "supporting_quote": "You approved this change."}
    response = {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(value)}]}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as c:
        with pytest.raises(ProviderError, match="transcript quote"):
            await complete(ProviderConfig("openai", "test", "secret", "https://api.openai.com/v1"), "",
                           {"evidence_refs": ["source:invoice"], "evidence": {
                               "transcript": "assistant: You approved this change.\nuser: No.",
                               "counterparty_transcript": "user: No."}}, client=c, response_type=CallAssessment)
