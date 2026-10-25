"""Intake dispatch telemetry: one SpacetimeDB `dispatch` row per event via the W2 `record_dispatch` reducer (router
role), ported from the bridge router on main. The row is part of durable acceptance (PRD H4): if the write fails the
Redis entry stays pending and is retried; both the A2A send and this write are idempotent, so retries are safe.
A first hop enqueued through SpacetimeDB gets its `queued` row from `enqueue_first_hop` instead, with the same
queue time and policy version.

The module's write receipt rejects a reused write id whose contents changed, so with a pin store (the dispatcher's
ingest.sqlite) a retry resends the first attempt's payload rather than a re-measured one."""
import json
import time
from contextvars import ContextVar

from common.spacetime_client import REDUCER_REFUSED_STATUS, SpacetimeClient, SpacetimeHTTPError

POLICY_VERSION = "w1-route-v1"   # matches harness/config.toml policy_version
# Queue time of the Redis entry the current task is handling: set by the consumer, read by the first-hop enqueue.
QUEUE_MS: ContextVar[int] = ContextVar("queue_ms", default=0)
# That entry's id, stored with the event's local outcome: a reclaim of the same entry finishes that outcome
# (it was never acknowledged), while another entry for the event is a duplicate.
ENTRY: ContextVar[str | None] = ContextVar("entry", default=None)
# Module caps: execute() takes a write id of at most 200 characters, text fields of at most 4096, and
# payloadFrom() at most 256 KiB of JSON.
MAX_WRITE_ID, MAX_TEXT, MAX_PAYLOAD = 200, 4096, 262144


class Undeliverable(ValueError):
    """A router write the module refuses on its form alone (an over-long id or field, an oversized payload,
    non-finite numbers). No retry can succeed, so the consumer dead-letters the entry."""


def check_limits(reducer: str, payload: dict) -> None:
    for key, value in payload.items():
        cap = MAX_WRITE_ID if key == "write_id" else MAX_TEXT
        length = len(value.encode("utf-16-le")) // 2 if isinstance(value, str) else 0   # JS string length
        if length > cap:
            raise Undeliverable(f"{reducer} {key} is {length} characters (module limit {cap})")
    try:   # the client's encoding; it escapes non-ASCII, so characters are bytes
        size = len(json.dumps(payload, allow_nan=False, separators=(",", ":")))
    except ValueError:
        raise Undeliverable(f"{reducer} payload has non-finite numbers") from None
    if size > MAX_PAYLOAD:
        raise Undeliverable(f"{reducer} payload is {size} bytes (module limit {MAX_PAYLOAD})")


def refused(error: BaseException) -> bool:
    """The reducer threw: deterministic, unlike transport errors, 429, 402, 408 and other 5xx."""
    return isinstance(error, SpacetimeHTTPError) and error.status_code == REDUCER_REFUSED_STATUS


def queue_ms(entry_id, received_ms: int | None = None) -> int:
    """Redis entry creation (its id's millisecond part) to processing start, clamped to a u32."""
    entry = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
    return min(4294967295, max(0, (received_ms or int(time.time() * 1000)) - int(entry.split("-")[0])))


async def record_dispatch(entry_id, event: dict, status: str, reason: str, *, agent: str = "", skill: str = "",
                          task_id: str = "", received_ms: int | None = None,
                          policy_version: str = POLICY_VERSION, pins=None) -> None:
    """Raises Undeliverable before any call when the module would refuse the row on its form."""
    db = SpacetimeClient(actor="router")
    if db.disabled:
        return
    entry = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
    payload = event.get("payload", {}) if isinstance(event, dict) else {}
    case_id = (event.get("case_id") if isinstance(event, dict) else None) or (
        payload.get("case_id") if isinstance(payload, dict) else "")
    event_id = event.get("event_id") if isinstance(event, dict) else None
    # A malformed event still gets a stable telemetry identity from Redis.
    event_id = event_id if isinstance(event_id, str) and event_id.strip() else f"redis:{entry}"
    # A stable write id makes an ambiguous retry a reducer no-op instead of a `duplicate` row.
    row = {
        "write_id": f"dispatch:{entry}:{status}",
        "event_id": event_id, "case_id": case_id if isinstance(case_id, str) else "",
        "agent": agent, "skill": skill, "task_id": task_id, "status": status,
        "reason": reason[:500], "queue_ms": queue_ms(entry, received_ms), "policy_version": policy_version,
    }
    check_limits("record_dispatch", row)
    await db.mutate("record_dispatch", pins.pin_write(row["write_id"], row) if pins is not None else row)
