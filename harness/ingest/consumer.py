"""Redis `events` consumer, group `router` (PRD H1, H4, H5). ACK only after a terminal outcome (queued, accepted,
duplicate, not_routed, triage). Failed dispatches stay pending and are reclaimed with XAUTOCLAIM. No entry stays
pending forever on a deterministic failure: a write the module can never accept is dead-lettered at once, and
an event the reducer keeps refusing (HTTP 530, e.g. a human-owned case) is dead-lettered once
ROUTER_MAX_REJECTED_ATTEMPTS reclaims have been refused too. Outages (transport, 429, other 5xx) retry forever.

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
from harness.ingest.telemetry import ENTRY, QUEUE_MS, Undeliverable, queue_ms, record_dispatch, refused
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
# Refused reclaims tolerated per event before its entry is dead-lettered (about 20 minutes at the default reclaim).
MAX_REJECTED_ATTEMPTS = int(os.getenv("ROUTER_MAX_REJECTED_ATTEMPTS", "10"))
ACK_STATUSES = {"queued", "accepted", "duplicate", "not_routed", "triage"}


def needs_model(ev: dict) -> bool:
    etype, payload = ev.get("type") or "", ev.get("payload") or {}
    if not isinstance(etype, str) or not isinstance(payload, dict):
        return False   # malformed: the pipeline dead-letters it as a bad event without a model call
    route = table_route(etype, payload)
    return route is None or route.status == "routed"


async def _handle(r: redis.Redis, pool: Pool, vault: Vault, dispatcher: Dispatcher, entry_id: bytes, ev: dict,
                  *, stream: str, group: str, triage: str, handler=process) -> None:
    received_ms = int(time.time() * 1000)
    entry = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
    # A first-hop enqueue reports this entry's queue time; the local outcome remembers which entry recorded it.
    tokens = QUEUE_MS.set(queue_ms(entry, received_ms)), ENTRY.set(entry)
    try:
        res = await handler(pool, vault, dispatcher, ev)
    except BadEvent as e:
        await _give_up(r, dispatcher, stream, group, entry_id, ev, f"bad event: {e}", received_ms)
        return
    finally:
        QUEUE_MS.reset(tokens[0])
        ENTRY.reset(tokens[1])
    if res["status"] == "dead_letter":
        await _give_up(r, dispatcher, stream, group, entry_id, ev, res["error"], received_ms)
        return
    try:
        await _telemetry(entry_id, ev, res, received_ms, dispatcher)   # raises -> entry stays pending
    except Undeliverable as e:
        await _give_up(r, dispatcher, stream, group, entry_id, ev, str(e), received_ms)
        return
    except Exception as e:
        if not refused(e):
            raise
        res = {**res, "status": "dispatch_failed", "rejected": True, "error": f"dispatch telemetry rejected: {e}"}
    event_id = res.get("event_id") or ev.get("event_id")
    if res.get("rejected") and dispatcher is not None and isinstance(event_id, str):
        count = dispatcher.reject(event_id, res.get("error") or "")
        # A refusal of every write (e.g. the router identity is not provisioned) refuses the triage row too:
        # that is the deployment, not the event, so the entry stays pending.
        if count > MAX_REJECTED_ATTEMPTS and await _give_up(
                r, dispatcher, stream, group, entry_id, ev, f"refused {count} times, last: {res.get('error')}",
                received_ms, row_required=True):
            return
    if res["status"] == "triage":
        await r.xadd(triage, {"json": json.dumps({"event_id": res["event_id"], "case_id": res["case_id"],
                                                  "reason": res["route"]["reason"],
                                                  "envelope": res.get("envelope")}, default=str)})
    if res["status"] in ACK_STATUSES:
        await r.xack(stream, group, entry_id)
        if dispatcher is not None:
            dispatcher.finished(entry, event_id if isinstance(event_id, str) else None)
    else:
        log.warning("leaving %s pending: %s", res["event_id"], res.get("error"))


async def _telemetry(entry_id, ev: dict, res: dict, received_ms: int, pins: Dispatcher | None = None) -> None:
    """`queued` writes nothing: enqueue_first_hop committed its dispatch row with the outbox row."""
    status, reason = res["status"], (res.get("route") or {}).get("reason", "")
    if status == "accepted":
        d = res.get("dispatch") or {}
        if d.get("task_id"):   # the contract requires the accepted A2A task id on an accepted row
            await record_dispatch(entry_id, ev, "accepted", reason, agent=d.get("agent") or "",
                                  skill=d.get("skill") or "", task_id=d["task_id"], received_ms=received_ms,
                                  pins=pins)
    elif status == "duplicate":
        prior = res.get("prior") or res.get("dispatch") or {}
        if prior.get("agent") and prior.get("skill"):   # only a routed (queued or accepted) event has a route
            # A queued event's task is created later by the dispatch worker, so the router may not know it.
            # `reason` is intake memory's P-007 duplicate (another event); otherwise Redis delivered it again.
            await record_dispatch(entry_id, ev, "duplicate", res.get("reason") or "re-delivered event",
                                  agent=prior["agent"], skill=prior["skill"], task_id=prior.get("task_id") or "",
                                  received_ms=received_ms, pins=pins)
    elif status == "triage":
        await record_dispatch(entry_id, ev, "triage", reason, received_ms=received_ms, pins=pins)
    elif status == "not_routed" and ev.get("type") != "gps.ping":
        # GPS pings are high volume: acknowledged without a row (each row also writes audit history).
        await record_dispatch(entry_id, ev, "not_routed", reason, received_ms=received_ms, pins=pins)


async def _give_up(r: redis.Redis, dispatcher: Dispatcher | None, stream: str, group: str, entry_id: bytes,
                   ev, reason: str, received_ms: int | None = None, *, raw=None, row_required: bool = False) -> bool:
    """Dead-letter an entry no retry can deliver, after a `triage` row says why; True once dead-lettered. A refused
    or undeliverable row is skipped, unless `row_required`, which keeps the entry pending instead. An outage
    raises and leaves the entry pending, so the row is not lost."""
    try:
        await record_dispatch(entry_id, ev, "triage", reason, received_ms=received_ms, pins=dispatcher)
    except Exception as e:
        if not (isinstance(e, Undeliverable) or refused(e)):
            raise
        if row_required:
            log.error("leaving %s pending: SpacetimeDB refuses its triage row too (%s); %s", entry_id, e, reason)
            return False
        log.warning("dead-lettering %s without a triage row: %s", entry_id, e)
    await _dead_letter(r, stream, group, entry_id, reason, json.dumps(ev, default=str) if raw is None else raw)
    event_id = ev.get("event_id") if isinstance(ev, dict) else None
    event_id = event_id if isinstance(event_id, str) else None
    if dispatcher is not None:
        if event_id is not None:
            dispatcher.dead_lettered(event_id, reason)
        dispatcher.finished(entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id), event_id)
    log.error("dead-lettered %s to %s:dead_letter: %s", entry_id, stream, reason)
    return True


async def _dead_letter(r: redis.Redis, stream: str, group: str, entry_id: bytes, reason: str, raw) -> None:
    async with r.pipeline(transaction=True) as pipe:  # atomic DLQ + ack: no loss, no double handling
        pipe.xadd(f"{stream}:dead_letter", {"entry_id": entry_id, "reason": reason, "json": raw or ""})
        pipe.xack(stream, group, entry_id)
        await pipe.execute()


def _decode(entry_id: bytes, fields: dict) -> dict | None:
    try:
        ev = json.loads(fields[b"json"])
    except (KeyError, ValueError):
        ev = None
    if not isinstance(ev, dict):   # valid JSON that is not an event object is just as unusable
        log.error("unparseable entry %s", entry_id)
        return None
    return ev


async def run_consumer(pool: Pool, url: str | None = None, *, stream: str = STREAM, group: str = GROUP,
                       triage: str = TRIAGE, consumer: str = CONSUMER, handler=process,
                       vault: Vault | None = None, dispatcher: Dispatcher | None = None) -> None:
    r = redis.from_url(url or os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    vault, dispatcher = vault or Vault(), dispatcher or Dispatcher()
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
    except redis.ResponseError:
        pass
    log.info("consuming %s as %s/%s (first hop %s, model slots %d, max in flight %d)", stream, group, consumer,
             dispatcher.transport, MODEL_CONCURRENCY, MAX_INFLIGHT)
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
                        try:
                            await _give_up(r, dispatcher, stream, group, entry_id, {}, "unparseable",
                                           raw=fields.get(b"json"))
                        except Exception:   # only this entry stays pending; the rest of the batch goes on
                            log.exception("could not dead-letter unparseable entry %s", entry_id)
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
