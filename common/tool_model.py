"""Bounded stateless Responses function calling; no provider text authorizes actions."""
from __future__ import annotations

import json
import re
from typing import Any

import httpx
from pydantic import ValidationError

from common.providers import Assessment, ProviderConfig, ProviderConfigurationError, ProviderError, _post

MAX_INPUT_BYTES = 350_000
MAX_RESPONSE_BYTES = 200_000
MAX_ARGUMENT_BYTES = 12_000


def strict_json(text: str) -> Any:
    def reject_constant(_):
        raise ValueError("non-finite JSON")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON property")
            result[key] = value
        return result

    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique)


async def model_turn(config: ProviderConfig, system: str, history: list[dict], tools: list[dict], *,
                     evidence_refs: list[str], require_tool: bool,
                     client: httpx.AsyncClient | None = None) -> dict:
    """Return replayable output plus validated calls or an evidence-bound final assessment."""
    if config.provider != "openai":
        raise ProviderConfigurationError("model-driven tools currently require the OpenAI provider")
    schema = Assessment.model_json_schema()
    schema["properties"]["confidence"].pop("minimum", None)
    schema["properties"]["confidence"].pop("maximum", None)
    refs = sorted(set(evidence_refs))
    if len(refs) > 1000 or sum(map(len, refs)) > 15_000:
        raise ProviderError("tool evidence catalog exceeds limit")
    if refs:
        schema["properties"]["evidence_refs"]["items"]["enum"] = refs
    else:
        schema["properties"]["evidence_refs"]["maxItems"] = 0
    body = {
        "model": config.model, "store": False, "instructions": system,
        "input": history, "tools": tools,
        "tool_choice": "required" if require_tool else ("auto" if tools else "none"),
        "parallel_tool_calls": False, "max_output_tokens": 1800,
        "include": ["reasoning.encrypted_content"],
        "text": {"format": {"type": "json_schema", "name": "freight_tool_assessment",
                            "strict": True, "schema": schema}},
    }
    if len(json.dumps(body, allow_nan=False).encode()) > MAX_INPUT_BYTES:
        raise ProviderError("tool conversation exceeds input limit")
    owned = client is None
    http = client or httpx.AsyncClient(timeout=45)
    try:
        result = await _post(http, f"{config.base_url}/responses",
                             headers={"Authorization": f"Bearer {config.api_key}"}, body=body)
    finally:
        if owned:
            await http.aclose()
    if len(json.dumps(result, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
        raise ProviderError("tool response exceeds size limit")
    if result.get("status") != "completed":
        raise ProviderError("tool response incomplete")
    output = result.get("output")
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        raise ProviderError("invalid tool response output")
    calls = []
    text = ""
    for item in output:
        kind = item.get("type")
        if kind == "function_call":
            call_id, name, raw = item.get("call_id"), item.get("name"), item.get("arguments")
            if not isinstance(call_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", call_id) \
                    or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", name) \
                    or not isinstance(raw, str) or len(raw.encode()) > MAX_ARGUMENT_BYTES:
                raise ProviderError("invalid tool call envelope")
            try:
                args = strict_json(raw)
            except (ValueError, RecursionError):
                raise ProviderError("invalid tool call JSON") from None
            if not isinstance(args, dict):
                raise ProviderError("tool arguments must be an object")
            calls.append({"id": call_id, "name": name, "arguments": args})
        elif kind == "message":
            for part in item.get("content", []):
                if part.get("type") == "refusal":
                    raise ProviderError("provider declined tool task")
                if part.get("type") == "output_text":
                    text += part.get("text", "")
        elif kind != "reasoning":
            raise ProviderError("unexpected provider output type")
    # Serial calls keep mutation order explicit; concurrency is between independent agents.
    if len(calls) > 1:
        raise ProviderError("provider exceeded serial tool call limit")
    if calls and not tools:
        raise ProviderError("provider selected a tool during final-only assessment")
    assessment = None
    if not calls:
        if require_tool:
            raise ProviderError("provider failed to select a required tool")
        try:
            assessment = Assessment.model_validate(strict_json(text))
        except (ValidationError, ValueError, RecursionError):
            raise ProviderError("tool final assessment failed validation") from None
        if set(assessment.evidence_refs) - set(refs):
            raise ProviderError("tool assessment cited unavailable evidence")
    return {"output": output, "calls": calls,
            "assessment": assessment.model_dump() if assessment else None, "usage": result.get("usage", {})}
