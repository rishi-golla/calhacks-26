"""Redis `events` consumer, group `router` (PRD H1, H4, H5). ACK only after a terminal outcome (accepted,
duplicate, not_routed, triage). Failed dispatches stay pending and are reclaimed with XAUTOCLAIM.

Each entry runs as its own task, so events that need no model (TMS events, parked PODs) finish at once
instead of waiting behind invoice extraction. Model-bound entries share MODEL_CONCURRENCY slots, which
lets vLLM batch them; MAX_INFLIGHT bounds how much of a backlog is held in memory."""
import asyncio
import json
import logging
import os
import socket
import time

import redis.asyncio as redis

from harness.ingest.dispatch import Dispatcher
from harness.ingest.pipeline import P_ORDER, BadEvent, process
from harness.ingest.routing import table_route
from harness.ingest.telemetry import record_dispatch
from harness.ingest.vault import Vault
from harness.pool import Pool

log = logging.getLogger("harness.consumer")
# Malformed entries go to "<stream>:dead_letter" (W3's events:dead_letter); `triage` is for people to classify.
STREAM, GROUP, TRIAGE = "events", "router", "triage"
CONSUMER = os.getenv("ROUTER_CONSUMER", f"asus-{socket.gethostname()}")
# Longer than the worst-case pipeline (extract + verify budgets), so live work isn't treated as abandoned.
RECLAIM_IDLE_MS = int(os.getenv("ROUTER_RECLAIM_IDLE_MS", "120000"))
MODEL_CONCURRENCY = int(os.getenv("ROUTER_MODEL_CONCURRENCY", "8"))
MAX_INFLIGHT = int(os.getenv("ROUTER_MAX_INFLIGHT", "64"))
ACK_STATUSES = {"accepted", "duplicate", "not_routed", "triage"}


def needs_model(ev: dict) -> bool:
    route = table_route(ev.get("type") or "", ev.get("payload") or {})
    return route is None or route.status == "routed"


async def _handle(r: redis.Redis, pool: Pool, vault: Vault, dispatcher: Dispatcher, entry_id: bytes, ev: dict,
                  *, stream: str, group: str, triage: str, handler=process) -> None:
    received_ms = int(time.time() * 1000)
    try:
        res = await handler(pool, vault, dispatcher, ev)
    except BadEvent as e:
        await record_dispatch(entry_id, ev, "triage", f"bad event: {e}", received_ms=received_ms)
        await _dead_letter(r, stream, group, entry_id, f"bad event: {e}", json.dumps(ev, default=str))
        return
    await _telemetry(entry_id, ev, res, received_ms)   # raises -> entry stays pending (durable acceptance)
    if res["status"] == "triage":
        await r.xadd(triage, {"json": json.dumps({"event_id": res["event_id"], "case_id": res["case_id"],
                                                  "reason": res["route"]["reason"],
                                                  "envelope": res.get("envelope")}, default=str)})
    if res["status"] in ACK_STATUSES:
        await r.xack(stream, group, entry_id)
    else:
        log.warning("leaving %s pending: %s", res["event_id"], res.get("error"))


async def _telemetry(entry_id, ev: dict, res: dict, received_ms: int) -> None:
    status, reason = res["status"], (res.get("route") or {}).get("reason", "")
    if status == "accepted":
        d = res.get("dispatch") or {}
        if d.get("task_id"):   # the contract requires the accepted A2A task id on an accepted row
            await record_dispatch(entry_id, ev, "accepted", reason, agent=d.get("agent") or "",
                                  skill=d.get("skill") or "", task_id=d["task_id"], received_ms=received_ms)
    elif status == "duplicate":
        prior = res.get("prior") or res.get("dispatch") or {}
        if prior.get("task_id"):   # only an accepted event has a dispatch row to mark as duplicate
            await record_dispatch(entry_id, ev, "duplicate", "re-delivered event", agent=prior.get("agent") or "",
                                  skill=prior.get("skill") or "", task_id=prior["task_id"], received_ms=received_ms)
    elif status == "triage":
        await record_dispatch(entry_id, ev, "triage", reason, received_ms=received_ms)
    elif status == "not_routed" and ev.get("type") != "gps.ping":
        # GPS pings are high volume: acknowledged without a row (each row also writes audit history).
        await record_dispatch(entry_id, ev, "not_routed", reason, received_ms=received_ms)


async def _dead_letter(r: redis.Redis, stream: str, group: str, entry_id: bytes, reason: str, raw) -> None:
    async with r.pipeline(transaction=True) as pipe:  # atomic DLQ + ack: no loss, no double handling
        pipe.xadd(f"{stream}:dead_letter", {"entry_id": entry_id, "reason": reason, "json": raw or ""})
        pipe.xack(stream, group, entry_id)
        await pipe.execute()


def _decode(entry_id: bytes, fields: dict) -> dict | None:
    try:
        return json.loads(fields[b"json"])
    except (KeyError, ValueError):
        log.error("unparseable entry %s", entry_id)
        return None


async def run_consumer(pool: Pool, url: str | None = None, *, stream: str = STREAM, group: str = GROUP,
                       triage: str = TRIAGE, consumer: str = CONSUMER, handler=process,
                       vault: Vault | None = None, dispatcher: Dispatcher | None = None) -> None:
    r = redis.from_url(url or os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    vault, dispatcher = vault or Vault(), dispatcher or Dispatcher()
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
    except redis.ResponseError:
        pass
    log.info("consuming %s as %s/%s (model slots %d, max in flight %d)", stream, group, consumer,
             MODEL_CONCURRENCY, MAX_INFLIGHT)
    slots = asyncio.Semaphore(MODEL_CONCURRENCY)
    inflight: dict[bytes, asyncio.Task] = {}

    async def work(entry_id: bytes, ev: dict) -> None:
        try:
            if needs_model(ev):
                async with slots:
                    await _handle(r, pool, vault, dispatcher, entry_id, ev, stream=stream, group=group,
                                  triage=triage, handler=handler)
            else:
                await _handle(r, pool, vault, dispatcher, entry_id, ev, stream=stream, group=group,
                              triage=triage, handler=handler)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("entry %s failed; left pending for reclaim", entry_id)
        finally:
            inflight.pop(entry_id, None)

    backoff = 1.0
    try:
        while True:
            try:
                room = MAX_INFLIGHT - len(inflight)
                if room <= 0:
                    await asyncio.wait(list(inflight.values()), return_when=asyncio.FIRST_COMPLETED)
                    continue
                _, claimed, _ = await r.xautoclaim(stream, group, consumer, RECLAIM_IDLE_MS, "0-0", count=room)
                claimed = [e for e in claimed if e[0] not in inflight]  # still running here, not abandoned
                fresh = await r.xreadgroup(group, consumer, {stream: ">"}, count=room, block=1000)
                batch = []
                for entry_id, fields in list(claimed) + [e for _s, es in fresh or [] for e in es]:
                    ev = _decode(entry_id, fields)
                    if ev is None:  # poison entry: dead-letter it instead of retrying forever
                        await record_dispatch(entry_id, {}, "triage", "unparseable event")
                        await _dead_letter(r, stream, group, entry_id, "unparseable", fields.get(b"json"))
                        continue
                    batch.append((entry_id, ev))
                batch.sort(key=lambda x: P_ORDER.get(x[1].get("priority"), 2))  # P0 queues for slots first
                for entry_id, ev in batch:
                    inflight[entry_id] = asyncio.create_task(work(entry_id, ev))
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("consumer loop error; backing off %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
    finally:
        for t in inflight.values():
            t.cancel()
        await r.aclose()


async def main() -> None:
    pool = Pool()
    await pool.check_health()
    health = asyncio.create_task(pool.health_loop())
    try:
        await run_consumer(pool)
    finally:
        health.cancel()
        await pool.aclose()
