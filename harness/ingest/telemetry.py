"""Intake dispatch telemetry: one SpacetimeDB `dispatch` row per event via the W2 `record_dispatch` reducer (router
role), ported from the bridge router on main. The row is part of durable acceptance (PRD H4): if the write fails the
Redis entry stays pending and is retried; both the A2A send and this write are idempotent, so retries are safe.
A first hop enqueued through SpacetimeDB gets its `queued` row from `enqueue_first_hop` instead, with the same
queue time and policy version."""
import time
from contextvars import ContextVar

from common.spacetime_client import SpacetimeClient

POLICY_VERSION = "w1-route-v1"   # matches harness/config.toml policy_version
# Queue time of the Redis entry the current task is handling: set by the consumer, read by the first-hop enqueue.
QUEUE_MS: ContextVar[int] = ContextVar("queue_ms", default=0)


def queue_ms(entry_id, received_ms: int | None = None) -> int:
    """Redis entry creation (its id's millisecond part) to processing start, clamped to a u32."""
    entry = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
    return min(4294967295, max(0, (received_ms or int(time.time() * 1000)) - int(entry.split("-")[0])))


async def record_dispatch(entry_id, event: dict, status: str, reason: str, *, agent: str = "", skill: str = "",
                          task_id: str = "", received_ms: int | None = None,
                          policy_version: str = POLICY_VERSION) -> None:
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
    await db.mutate("record_dispatch", {
        "write_id": f"dispatch:{entry}:{status}",
        "event_id": event_id, "case_id": case_id if isinstance(case_id, str) else "",
        "agent": agent, "skill": skill, "task_id": task_id, "status": status,
        "reason": reason[:500], "queue_ms": queue_ms(entry, received_ms), "policy_version": policy_version,
    })
