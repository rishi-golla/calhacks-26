"""First-hop router: acknowledge Redis only after durable A2A admission."""
import asyncio
import json
import logging
import os
import socket
import time

import redis.asyncio as redis

from common.a2a_transport import send_task
from common.config import SKILL_OWNER
from common.envelope import DelegationMeta, TaskEnvelope
from common.spacetime_client import SpacetimeClient

POLICY_VERSION = 'first-hop-v1'

ROUTES = {
    "carrier_invoice.received": "process_carrier_invoice",
    # A POD lands at delivery, hours before the carrier bills: it wakes a waiting document chase, never
    # opens settlement for an invoice that does not exist yet.
    "pod.received": "receive_document",
    "shipper_payment.received": "reconcile_customer_payment",
    "bank.event": "reconcile_customer_payment",
    "bank_change.requested": "screen_bank_change",
    "voice.inbound.dispute": "process_carrier_invoice",
}
log = logging.getLogger("router")


def route_event(event: dict) -> tuple[str, TaskEnvelope, DelegationMeta]:
    """W1 owns classification/case resolution; this bridge never invents a case."""
    skill = ROUTES.get(event.get("type"))
    if not skill:
        raise ValueError("event needs an owning intake route")
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        raise TypeError("event payload must be an object")
    case_id = event.get("case_id") or payload.get("case_id")
    if not isinstance(case_id, str) or not 1 <= len(case_id) <= 36:
        raise ValueError("intake must resolve a case_id before routing")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id is required for durable deduplication")
    env = TaskEnvelope(task_type=skill, case_id=case_id, subject=payload,
                       evidence_refs=payload.get("evidence_refs", []), priority=event.get("priority", "P2"))
    meta = DelegationMeta(case_id=case_id, from_agent="router", priority=env.priority,
                          correlation_id=case_id, causation_id=event_id,
                          idempotency_key=f"event:{event_id}")
    return SKILL_OWNER[skill], env, meta


async def record_dispatch(entry_id, event, status, reason, *, agent='', skill='', task_id='', received_ms=None) -> None:
    db = SpacetimeClient(actor='router')
    if db.disabled:
        return
    entry = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
    payload = event.get('payload', {})
    case_id = event.get('case_id') or (payload.get('case_id') if isinstance(payload, dict) else '')
    event_id = event.get('event_id')
    # A malformed event still gets a stable telemetry identity from Redis.
    event_id = event_id if isinstance(event_id, str) and event_id.strip() else f'redis:{entry}'
    queue_ms = min(4294967295, max(0, (received_ms or int(time.time() * 1000)) - int(entry.split('-')[0])))
    # A stable write id makes an ambiguous retry a reducer no-op instead of a `duplicate` row.
    await db.mutate('record_dispatch', {
        'write_id': f'dispatch:{entry}:{status}',
        'event_id': event_id, 'case_id': case_id if isinstance(case_id, str) else '',
        'agent': agent, 'skill': skill, 'task_id': task_id, 'status': status,
        'reason': reason, 'queue_ms': queue_ms, 'policy_version': POLICY_VERSION,
    })


async def process_entry(client, entry_id, fields) -> None:
    received_ms = int(time.time() * 1000)
    event = {}
    try:
        parsed = json.loads(fields.get(b"json", fields.get("json", "")))
        if not isinstance(parsed, dict):
            raise TypeError("event must be an object")
        event = parsed
        if event.get('type') != 'gps.ping':
            agent, env, meta = route_event(event)
    except (ValueError, TypeError) as error:
        await record_dispatch(entry_id, event, 'triage', str(error), received_ms=received_ms)
        # Keep malformed/unclassified intake visible; atomic DLQ+ack avoids message loss.
        async with client.pipeline(transaction=True) as pipeline:
            pipeline.xadd("events:dead_letter", {"entry_id": entry_id, "reason": str(error),
                                                "json": fields.get(b"json", fields.get("json", ""))})
            pipeline.xack("events", "router", entry_id)
            await pipeline.execute()
        return
    if event.get('type') == 'gps.ping':
        # High-volume telemetry: acknowledge without a dispatch row (each row also writes audit history).
        await client.xack('events', 'router', entry_id)
        return
    accepted = await send_task(agent, env, meta)
    if not accepted.id or accepted.context_id != env.case_id:
        raise ValueError("agent acceptance did not preserve the case")
    await record_dispatch(entry_id, event, 'accepted', f"Known event type: {event['type']}",
                          agent=agent, skill=env.task_type, task_id=accepted.id, received_ms=received_ms)
    await client.xack("events", "router", entry_id)
    log.info("event %s admitted as %s by %s", event["event_id"], accepted.id, agent)


async def main() -> None:
    client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    consumer = os.getenv("ROUTER_CONSUMER", f"{socket.gethostname()}-{os.getpid()}")
    try:
        try:
            await client.xgroup_create("events", "router", id="0", mkstream=True)
        except redis.ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise
        cursor = "0-0"
        while True:
            # Recover entries whose process died before acknowledging acceptance.
            claimed = await client.xautoclaim("events", "router", consumer,
                                               min_idle_time=30_000, start_id=cursor, count=10)
            cursor, pending = claimed[:2]
            fresh = await client.xreadgroup("router", consumer, {"events": ">"}, count=10, block=2000)
            entries = [*pending, *(entry for _, group in fresh or [] for entry in group)]
            for entry_id, fields in entries:
                try:
                    await process_entry(client, entry_id, fields)
                except Exception as error:  # noqa: BLE001 -- unacknowledged entry retries through XAUTOCLAIM
                    log.warning("intake %s remains pending: %s", entry_id, type(error).__name__)
    finally:
        await client.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
