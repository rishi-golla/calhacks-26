"""First-hop router: Redis Streams `events` -> owning agent's A2A skill (PRD 7.3). No orchestration:
it starts one task per new case and returns immediately; agents delegate to each other from there."""
import asyncio
import json
import logging
import os

import redis.asyncio as redis

from common import a2a_client, store
from common.config import SKILL_OWNER
from common.envelope import DelegationMeta, TaskEnvelope

ROUTES = {
    "carrier_invoice.received": "process_carrier_invoice",
    "pod.received": "process_carrier_invoice",
    "shipper_payment.received": "reconcile_customer_payment",
    "bank.event": "reconcile_customer_payment",
    "bank_change.requested": "screen_bank_change",
    "voice.inbound.dispute": "process_carrier_invoice",
}
log = logging.getLogger("router")


def plan(ev: dict) -> tuple[str, TaskEnvelope, dict] | None:
    """Pure routing decision: (agent, envelope, metadata) or None for unknown/duplicate events."""
    skill = ROUTES.get(ev.get("type", ""))
    if not skill:
        return None
    p = ev.get("payload", {})
    case_id = p.get("case_id") or ev.get("case_hint") or ev["event_id"]
    key = ev.get("idempotency_key") or f"{ev['type']}:{p.get('invoice_id') or p.get('load_id') or ev['event_id']}"
    if not store.seen_once(f"event:{ev['event_id']}") or not store.seen_once(f"case:{key}"):
        return None
    env = TaskEnvelope(task_type=skill, case_id=case_id, display_id=p.get("load_id"), subject=p,
                       evidence_refs=ev.get("evidence_refs", [f"event:{ev['event_id']}"]),
                       priority=ev.get("priority", "P2"))
    meta = DelegationMeta(case_id=case_id, from_agent="router", idempotency_key=key, priority=env.priority,
                          context_id=case_id)
    return SKILL_OWNER[skill], env, meta.model_dump(exclude_none=True)


async def dispatch(ev: dict) -> str | None:
    p = plan(ev)
    if p is None:
        log.info("skip %s (%s)", ev.get("event_id"), ev.get("type"))
        return None
    agent, env, meta = p
    task = await a2a_client.start_task(agent, env.model_dump(), meta, context_id=env.case_id)
    store.log_event(env.case_id, "router", task.id if task else None, "routed", to=agent, skill=env.task_type,
                    event_id=ev["event_id"])
    return task.id if task else None


async def main() -> None:
    r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    try:
        await r.xgroup_create("events", "router", id="0", mkstream=True)
    except redis.ResponseError:
        pass
    while True:
        resp = await r.xreadgroup("router", "router-1", {"events": ">"}, count=10, block=2000)
        for _stream, entries in resp or []:
            for entry_id, fields in entries:
                try:
                    await dispatch(json.loads(fields[b"json"]))
                except Exception:
                    log.exception("dispatch failed for %s", entry_id)
                    continue
                await r.xack("events", "router", entry_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
