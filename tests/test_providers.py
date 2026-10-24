import json

import httpx
import pytest

from common.config import AGENTS
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
        refs = body["text"]["format"]["schema"]["properties"]["evidence_refs"]
        assert refs["items"]["enum"] == ["source:invoice"]
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
        assert body["tools"][0]["input_schema"]["properties"]["evidence_refs"]["items"]["enum"] == [
            "source:invoice"]
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


@pytest.fixture
def openai_only(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENAI_MODEL", "default-assessment-model")
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ALLOWED_AGENT_MODELS"):
        monkeypatch.delenv(key, raising=False)
    for agent in AGENTS:
        monkeypatch.delenv(f"AGENT_PROVIDER_{agent.upper()}", raising=False)
        monkeypatch.delenv(f"AGENT_MODEL_{agent.upper()}", raising=False)


@pytest.mark.parametrize("agent", list(AGENTS))
def test_all_agents_can_use_one_openai_key(openai_only, agent):
    config = configuration(agent)
    assert config == ProviderConfig(
        "openai", "default-assessment-model", "test-openai-key", "https://api.openai.com/v1")


def test_explicit_anthropic_override_requires_its_own_key(openai_only, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER_RISK", "anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "independent-review-model")
    with pytest.raises(ProviderConfigurationError, match="ANTHROPIC_API_KEY"):
        configuration("risk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    assert configuration("risk") == ProviderConfig(
        "anthropic", "independent-review-model", "test-anthropic-key", "https://api.anthropic.com/v1")
    assert configuration("validator").provider == "openai"


@pytest.mark.parametrize("pin", ["auto", "quality-review-model", "openai/quality-review-model"])
def test_validator_model_override_can_be_pinned(openai_only, monkeypatch, pin):
    monkeypatch.setenv("AGENT_MODEL_VALIDATOR", "quality-review-model")
    config = configuration("validator", pin)
    assert config.provider == "openai" and config.model == "quality-review-model"
    assert config.api_key == "test-openai-key"
    assert configuration("carrier_ap").model == "default-assessment-model"
    with pytest.raises(ProviderConfigurationError, match="catalog"):
        configuration("carrier_ap", "openai/quality-review-model")


def test_openai_only_model_catalog_allows_explicit_pin(openai_only, monkeypatch):
    monkeypatch.setenv("ALLOWED_AGENT_MODELS", "openai/catalog-model")
    config = configuration("validator", "openai/catalog-model")
    assert config.provider == "openai" and config.model == "catalog-model"
    with pytest.raises(ProviderConfigurationError, match="catalog"):
        configuration("validator", "openai/unlisted-model")


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


async def test_no_evidence_catalog_requires_empty_citations():
    def respond(request):
        schema = json.loads(request.content)["text"]["format"]["schema"]
        refs = schema["properties"]["evidence_refs"]
        assert refs["maxItems"] == 0 and "enum" not in refs["items"]
        value = {**assessment(), "evidence_refs": []}
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result, _ = await complete(
            ProviderConfig("openai", "test", "secret", "https://api.openai.com/v1"), "",
            {"evidence": {"nested_document_id": "not-a-citation"}}, client=client)
    assert result.evidence_refs == []


@pytest.mark.parametrize("refs", ["not-a-list", [1], [str(index) for index in range(1001)]])
async def test_invalid_reference_catalog_is_rejected_before_inference(refs):
    def unexpected(_):
        raise AssertionError("invalid catalog must not reach provider")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ProviderError, match="reference"):
            await complete(ProviderConfig("openai", "test", "secret", "https://api.openai.com/v1"), "",
                           {"evidence_refs": refs}, client=client)
