import json

import httpx
import pytest

from common.providers import ProviderConfig, ProviderConfigurationError, ProviderError
from common.tool_model import model_turn

CONFIG = ProviderConfig("openai", "test-model", "secret-key", "https://api.openai.com/v1")
TOOLS = [{"type": "function", "name": "inspect", "description": "Inspect", "strict": True,
          "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}]


def call(arguments="{}"):
    return {"type": "function_call", "call_id": "call_1", "name": "inspect", "arguments": arguments}


async def test_stateless_turn_preserves_encrypted_reasoning_and_function_calls():
    reasoning = {"type": "reasoning", "id": "r_1", "summary": [], "encrypted_content": "opaque"}
    history = [{"role": "user", "content": "Inspect"}, reasoning,
               call(), {"type": "function_call_output", "call_id": "call_1", "output": "{}"}]

    def respond(request):
        body = json.loads(request.content)
        assert body["input"] == history
        assert body["store"] is False
        assert body["parallel_tool_calls"] is False
        assert body["tool_choice"] == "required"
        assert body["include"] == ["reasoning.encrypted_content"]
        assert body["tools"] == TOOLS
        return httpx.Response(200, json={"status": "completed", "output": [reasoning, call()],
                                        "usage": {"output_tokens": 50}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await model_turn(CONFIG, "system", history, TOOLS, evidence_refs=[],
                                  require_tool=True, client=client)
    assert result["output"] == [reasoning, call()]
    assert result["calls"] == [{"id": "call_1", "name": "inspect", "arguments": {}}]
    assert result["assessment"] is None


@pytest.mark.parametrize("arguments", ['{"amount":NaN}', '{"amount":1,"amount":2}', '[]', '{}junk'])
async def test_untrusted_argument_json_is_rejected(arguments):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "status": "completed", "output": [call(arguments)]}))) as client:
        with pytest.raises(ProviderError):
            await model_turn(CONFIG, "", [], TOOLS, evidence_refs=[], require_tool=True, client=client)


async def test_final_citations_must_come_from_tool_evidence():
    final = {"summary": "Done", "issues": [], "evidence_refs": ["fabricated"], "confidence": 1}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(final)}]}]}))) as client:
        with pytest.raises(ProviderError, match="unavailable evidence"):
            await model_turn(CONFIG, "", [], TOOLS, evidence_refs=["source:1"],
                             require_tool=False, client=client)


async def test_final_cannot_replace_required_action():
    final = {"summary": "Paid", "issues": [], "evidence_refs": [], "confidence": 1}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(final)}]}]}))) as client:
        with pytest.raises(ProviderError, match="required tool"):
            await model_turn(CONFIG, "", [], TOOLS, evidence_refs=[], require_tool=True, client=client)


@pytest.mark.parametrize("response", [
    {"status": "incomplete", "output": []},
    {"status": "completed", "output": [call(), call()]},
    {"status": "completed", "output": [{"type": "web_search_call"}]},
    {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
])
async def test_invalid_provider_responses_never_execute_tools(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as client:
        with pytest.raises(ProviderError):
            await model_turn(CONFIG, "", [], TOOLS, evidence_refs=[], require_tool=True, client=client)


async def test_input_size_limit_precedes_request():
    with pytest.raises(ProviderError, match="input limit"):
        await model_turn(CONFIG, "x" * 400_000, [], TOOLS, evidence_refs=[], require_tool=True)


async def test_other_providers_fail_explicitly():
    config = ProviderConfig("anthropic", "test", "secret", "https://api.anthropic.com/v1")
    with pytest.raises(ProviderConfigurationError, match="OpenAI"):
        await model_turn(config, "", [], TOOLS, evidence_refs=[], require_tool=True)


async def test_final_only_turn_disables_tools_and_rejects_provider_tool_calls():
    def respond(request):
        body = json.loads(request.content)
        assert body["tools"] == []
        assert body["tool_choice"] == "none"
        return httpx.Response(200, json={"status": "completed", "output": [call()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError, match="final-only"):
            await model_turn(CONFIG, "", [], [], evidence_refs=[], require_tool=False, client=client)
