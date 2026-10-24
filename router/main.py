"""First-hop router: Redis Streams `events` -> owning agent's A2A skill (PRD 7.3). No orchestration."""
import asyncio
import json
import logging
import os

import redis.asyncio as redis

ROUTES = {
    "carrier_invoice.received": "process_carrier_invoice",
    "pod.received": "process_carrier_invoice",
    "shipper_payment.received": "reconcile_customer_payment",
    "bank.event": "reconcile_customer_payment",
    "bank_change.requested": "screen_bank_change",
    "voice.inbound.dispute": "process_carrier_invoice",
}
log = logging.getLogger("router")


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
                ev = json.loads(fields[b"json"])
                skill = ROUTES.get(ev["type"])
                log.info("event %s -> %s", ev["type"], skill)
                # TODO(W3): build TaskEnvelope and SendMessage to SKILL_OWNER[skill]
                await r.xack("events", "router", entry_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
