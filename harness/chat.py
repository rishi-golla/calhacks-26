"""Chat completion core shared by POST /v1/chat/completions and the ingest pipeline (PRD v1.0 7.1 shape)."""
import asyncio
import json
import logging
import math
import re
import time

import httpx

from harness import jsonschema_lite
from harness.pool import Backend, Plan, Pool, Unavailable

log = logging.getLogger("harness.chat")
DECISION_KEYS = ("outcome", "verdict", "decision", "label", "skill", "agree")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class BadRequest(Exception):
    pass


def _finite(text: str) -> float:
    v = float(text)
    if not math.isfinite(v):
        raise ValueError(f"non-finite number {text[:20]}")
    return v


def _no_constants(name: str) -> float:
    raise ValueError(f"{name} is not valid JSON")


def parse_json(content: str | None) -> object:
    """Strict JSON: NaN/Infinity (or numbers that overflow to inf) can't be re-serialized, so they count
    as invalid output and go through the normal retry/fallback instead of a 500."""
    if content is None:
        raise ValueError("empty content")
    return json.loads(_FENCE.sub("", content.strip()), parse_constant=_no_constants, parse_float=_finite)


def check_format(content: str | None, fmt: dict | None) -> tuple[object | None, str | None]:
    """Returns (parsed, error). Only json_object / json_schema formats are checked."""
    if not fmt or fmt.get("type") not in ("json_object", "json_schema"):
        return None, None
    try:
        parsed = parse_json(content)
    except ValueError as e:
        return None, f"not valid JSON: {e}"
    if fmt["type"] == "json_schema":
        schema = (fmt.get("json_schema") or {}).get("schema") or {}
        errs = jsonschema_lite.errors(parsed, schema)
        if errs:
            return parsed, "; ".join(errs[:5])
    elif not isinstance(parsed, dict):
        return parsed, "expected a JSON object"
    return parsed, None


async def _run_chain(pool: Pool, chain: list[Backend], payload: dict, priority: str,
                     deadline: float) -> tuple[dict, Backend, int, object | None, list[str]]:
    """Try each healthy backend in order. Returns (response, backend, queue_ms, parsed_json, notes)."""
    fmt = payload.get("response_format")
    notes: list[str] = []
    for b in chain:
        if not b.healthy:
            notes.append(f"{b.served_name} down")
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        msgs = payload["messages"]
        for attempt in (1, 2):
            try:
                resp, queue_ms, _ = await pool.call(b, {**payload, "messages": msgs}, priority,
                                                    min(b.timeout_s, deadline - time.monotonic()))
            except (TimeoutError, httpx.TimeoutException):
                notes.append(f"{b.served_name} timeout")
                break
            except httpx.HTTPStatusError as e:
                if 400 <= e.response.status_code < 500:
                    raise BadRequest(e.response.text[:500]) from e
                pool.mark_down(b)
                notes.append(f"{b.served_name} http {e.response.status_code}")
                break
            except httpx.HTTPError as e:
                pool.mark_down(b)
                notes.append(f"{b.served_name} {type(e).__name__}")
                break
            content = resp["choices"][0]["message"].get("content")
            parsed, err = check_format(content, fmt)
            if err is None:
                if parsed is not None:
                    resp["choices"][0]["message"]["content"] = json.dumps(parsed)
                if attempt == 2:
                    notes.append(f"{b.served_name} json repaired on retry")
                return resp, b, queue_ms, parsed, notes
            notes.append(f"{b.served_name} invalid json (attempt {attempt}): {err[:120]}")
            msgs = [*payload["messages"], {"role": "assistant", "content": content or ""},
                    {"role": "user", "content": f"That reply was invalid ({err[:300]}). "
                                                "Reply with only the corrected JSON, nothing else."}]
            if deadline - time.monotonic() <= 0:
                break
    raise Unavailable("; ".join(notes) or "no backend")


def _vote_key(parsed: object | None, content: str | None) -> object:
    if isinstance(parsed, dict):
        picked = {k: parsed[k] for k in DECISION_KEYS if k in parsed}
        return picked or parsed
    if parsed is not None:
        return parsed
    return " ".join((content or "").lower().split())


async def complete(pool: Pool, body: dict) -> dict:
    """Route, call, validate. Raises Unavailable (-> 503), KeyError (unknown model), BadRequest."""
    t0 = time.monotonic()
    md = body.get("metadata") or {}
    payload = {k: v for k, v in body.items() if k not in ("metadata", "model")}
    if not payload.get("messages"):
        raise BadRequest("messages required")
    priority = md.get("priority", "P2")
    budget = md.get("max_latency_ms")
    deadline = t0 + (budget / 1000 if budget else pool.default_deadline_s)
    plan: Plan = pool.plan(body, md)

    if not plan.ensemble:
        resp, b, queue_ms, _, notes = await _run_chain(pool, plan.chains[0], payload, priority, deadline)
        ensemble = None
    else:
        # Give each ensemble member a distinct primary backend where possible.
        claimed: set[str] = set()
        chains = []
        for chain in plan.chains:
            c = [x for x in chain if x.key not in claimed] or chain
            first = next((x for x in c if x.healthy), None)
            if first:
                claimed.add(first.key)
            chains.append(c)
        results = await asyncio.gather(*(_run_chain(pool, c, payload, priority, deadline) for c in chains),
                                       return_exceptions=True)
        for r in results:
            if isinstance(r, BadRequest):
                raise r
        ok = [r for r in results if not isinstance(r, BaseException)]
        if not ok:
            raise Unavailable("ensemble: all members failed")
        resp, b, queue_ms, _, notes = ok[0]
        votes = [{"model": r[1].served_name,
                  "answer": _vote_key(r[3], r[0]["choices"][0]["message"].get("content"))} for r in ok]
        keys = [json.dumps(v["answer"], sort_keys=True, default=str) for v in votes]
        agreement = len(set(keys)) == 1 and len(votes) >= 2   # one vote cannot confirm a high-stakes call
        ensemble = {"models": [v["model"] for v in votes], "agreement": agreement, "votes": votes}
        if len(votes) < 2:
            notes.append("ensemble degraded to 1 vote")

    reason = plan.reason + (f" [{'; '.join(notes)}]" if notes else "")
    resp["harness"] = {"model_used": b.served_name, "route_reason": reason, "ensemble": ensemble,
                       "latency_ms": int((time.monotonic() - t0) * 1000), "queue_ms": queue_ms}
    log.info(json.dumps({"agent": md.get("agent"), "task_type": md.get("task_type"),
                         "case_id": md.get("case_id"), "policy": pool.policy_version,
                         "usage": resp.get("usage"), **resp["harness"]}, default=str))
    return resp
