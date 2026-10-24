"""Redis `events` consumer, group `router` (PRD H1, H4, H5). ACK only after a terminal outcome (accepted,
duplicate, not_routed, triage). Failed dispatches stay pending and are reclaimed with XAUTOCLAIM."""
import asyncio
import json
import logging
import os
import socket

import redis.asyncio as redis

from harness.ingest.dispatch import Dispatcher
from harness.ingest.pipeline import P_ORDER, BadEvent, process
from harness.ingest.vault import Vault
from harness.pool import Pool

log = logging.getLogger("harness.consumer")
STREAM, GROUP, TRIAGE = "events", "router", "triage"
CONSUMER = os.getenv("ROUTER_CONSUMER", f"asus-{socket.gethostname()}")
RECLAIM_IDLE_MS = int(os.getenv("ROUTER_RECLAIM_IDLE_MS", "30000"))
ACK_STATUSES = {"accepted", "duplicate", "not_routed", "triage"}


async def _handle(r: redis.Redis, pool: Pool, vault: Vault, dispatcher: Dispatcher,
                  entry_id: bytes, ev: dict | None) -> None:
    if ev is None:
        return
    try:
        res = await process(pool, vault, dispatcher, ev)
    except BadEvent as e:
        await r.xadd(TRIAGE, {"json": json.dumps({"reason": f"bad event: {e}", "event": ev}, default=str)})
        await r.xack(STREAM, GROUP, entry_id)
        return
    if res["status"] == "triage":
        await r.xadd(TRIAGE, {"json": json.dumps({"event_id": res["event_id"], "case_id": res["case_id"],
                                                  "reason": res["route"]["reason"],
                                                  "envelope": res.get("envelope")}, default=str)})
    if res["status"] in ACK_STATUSES:
        await r.xack(STREAM, GROUP, entry_id)
    else:
        log.warning("leaving %s pending: %s", res["event_id"], res.get("error"))


def _decode(entry_id: bytes, fields: dict) -> dict | None:
    try:
        return json.loads(fields[b"json"])
    except (KeyError, ValueError):
        log.error("unparseable entry %s", entry_id)
        return None


async def run_consumer(pool: Pool, url: str | None = None) -> None:
    r = redis.from_url(url or os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    vault, dispatcher = Vault(), Dispatcher()
    try:
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError:
        pass
    log.info("consuming %s as %s/%s", STREAM, GROUP, CONSUMER)
    backoff = 1.0
    while True:
        try:
            _, claimed, _ = await r.xautoclaim(STREAM, GROUP, CONSUMER, RECLAIM_IDLE_MS, "0-0", count=10)
            fresh = await r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=10, block=2000)
            entries = list(claimed) + [e for _s, es in fresh or [] for e in es]
            batch = []
            for entry_id, fields in entries:
                ev = _decode(entry_id, fields)
                if ev is None:  # poison entry: park it in triage instead of retrying forever
                    await r.xadd(TRIAGE, {"json": json.dumps({"reason": "unparseable", "id": entry_id.decode()})})
                    await r.xack(STREAM, GROUP, entry_id)
                    continue
                batch.append((entry_id, ev))
            batch.sort(key=lambda x: P_ORDER.get(x[1].get("priority"), 2))  # P0 first
            await asyncio.gather(*(_handle(r, pool, vault, dispatcher, i, ev) for i, ev in batch))
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("consumer loop error; backing off %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def main() -> None:
    pool = Pool()
    await pool.check_health()
    health = asyncio.create_task(pool.health_loop())
    try:
        await run_consumer(pool)
    finally:
        health.cancel()
        await pool.aclose()
