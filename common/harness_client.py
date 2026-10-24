"""Client for the W1 harness (PRD 7.1). OpenAI-compatible with route hints in `metadata`."""
import os

import httpx

HARNESS_URL = os.getenv("HARNESS_URL", "http://localhost:8000")


async def complete(messages: list[dict], *, agent: str, case_id: str, task_type: str,
                   priority: str = "P2", stakes: str = "normal", model: str = "auto",
                   response_format: dict | None = None, tools: list | None = None,
                   max_tokens: int = 512) -> dict:
    body = {"model": model, "messages": messages, "max_tokens": max_tokens,
            "metadata": {"agent": agent, "case_id": case_id, "task_type": task_type,
                         "priority": priority, "stakes": stakes}}
    if response_format:
        body["response_format"] = response_format
    if tools:
        body["tools"] = tools
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{HARNESS_URL}/v1/chat/completions", json=body)
        if r.status_code == 503:
            return {"error": "unavailable"}
        r.raise_for_status()
        return r.json()
