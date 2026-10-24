"""First-hop router: Redis Streams `events` -> owning agent's A2A skill (PRD 7.3). No orchestration:
it starts one task per new case and returns immediately; agents delegate to each other from there.
Telemetry events (load.tendered, gps.ping, pod.received) are not routed: they arrive as evidence on the invoice."""
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
    "shipper_payment.received": "reconcile_customer_payment",
    "bank.event": "reconcile_customer_payment",
    "bank_change.requested": "screen_bank_change",
    "voice.inbound.dispute": "process_carrier_invoice",
}
log = logging.getLogger("router")


AMOUNT_FIELDS = ("invoice_amount", "contract_rate", "paid_amount")
MAX_AMOUNT = 1_000_000


def invalid(p: dict) -> str | None:
    for f in AMOUNT_FIELDS:
        if f in p and (isinstance(p[f], bool) or not isinstance(p[f], int | float) or not 0 <= p[f] <= MAX_AMOUNT):
            return f"{f} must be a number in [0, {MAX_AMOUNT}]"
    for li in p.get("line_items", []):
        if not isinstance(li.get("amount"), int | float) or not 0 <= li["amount"] <= MAX_AMOUNT:
            return "line_items[].amount out of range"
    return None


def _keys(ev: dict) -> tuple[str, str]:
    p = ev.get("payload", {})
    key = ev.get("idempotency_key") or f"{ev['type']}:{p.get('invoice_id') or p.get('load_id') or ev['event_id']}"
    return f"event:{ev['event_id']}", f"case:{key}"


def plan(ev: dict) -> tuple[str, TaskEnvelope, dict] | None:
    """Routing decision: (agent, envelope, metadata) or None for unknown, invalid or already-dispatched events.
    Dedupe keys are only recorded by `dispatch` after the first agent accepted the task."""
    skill = ROUTES.get(ev.get("type", ""))
    if not skill:
        return None
    p = ev.get("payload", {})
    if err := invalid(p):
        log.warning("reject %s: %s", ev.get("event_id"), err)
        return None
    case_id = p.get("case_id") or ev.get("case_hint") or ev["event_id"]
    ev_key, case_key = _keys(ev)
    key = case_key.removeprefix("case:")
    if store.is_seen(ev_key) or store.is_seen(case_key):
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
    keys = _keys(ev)
    if not store.claim_all(list(keys)):  # concurrent deliveries: exactly one claims every key
        return None
    try:
        task = await a2a_client.start_task(agent, env.model_dump(), meta, context_id=env.case_id)
        if task is None:
            raise RuntimeError(f"{agent} did not accept {ev['event_id']}")
    except Exception:
        for k in keys:  # release so the redelivered event is retried
            store.forget(k)
        raise
    store.log_event(env.case_id, "router", task.id, "routed", to=agent, skill=env.task_type,
                    event_id=ev["event_id"])
    return task.id


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
