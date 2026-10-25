"""Deliver SpacetimeDB outbox work: W2 reviews and W1 first hops enqueued by the router.

Run ``python -m common.review_worker`` with the router service credential and
the same A2A runtime database used by the agent services. Acceptance, retries
and corrections use stable IDs; terminal tasks are never reopened. A
``first_hop`` row is pushed to its agent with idempotency key ``event:<event_id>``
and acknowledged only after its ``dispatch`` row records the accepted task.
``DISPATCH_POLL_SECONDS`` (default 1) sets the poll interval.

A failed row waits ``min(2**attempts, 300)`` seconds from its ``updated_at``
before it is claimed again (at most 15 s for a first hop). Each pass delivers
every due first hop, then claims at most ``DISPATCH_MAX_REVIEW_CLAIMS``
(default 20) review rows. A review row that has failed ``REVIEW_MAX_ATTEMPTS``
(default 10) times with a deterministic error is no longer claimed; it stays
``pending`` with its ``last_error`` for an operator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import Column, MetaData, String, Table, insert, select
from sqlalchemy.exc import IntegrityError

from agents.validator.tools import FINANCIAL_AGENTS, encoded, flag_target, lookup
from common.a2a_transport import send_task
from common.config import AGENTS
from common.envelope import DelegationMeta, TaskEnvelope
from common.first_hop import first_hop_meta
from common.runtime_store import RuntimeStore, stable_id, task_id_for
from common.source_repository import SourceRepository, SourceUnavailable, fixture_mode
from common.spacetime_client import SpacetimeClient, SpacetimeHTTPError

log = logging.getLogger(__name__)
REVIEW_KINDS = {"review_output", "review_flag", "review_followup"}
FIRST_HOP = "first_hop"
LEASE_SECONDS = 60
REDUCER_REJECTED = 530   # SpacetimeDB's status for a reducer that threw: retrying the same write cannot succeed
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
DEFAULT_RANK = PRIORITY_RANK[TaskEnvelope.model_fields["priority"].default]
BACKOFF_CAP_SECONDS = 300
FIRST_HOP_BACKOFF_CAP_SECONDS = 15
# A review's model loop can spend most of the envelope's default 120 s and still need the deterministic fallback
# (agents/validator/main.py) to finish; reviews get the 300 s first hops get (ROUTER_TASK_DEADLINE_S).
REVIEW_DEADLINE_S = max(1, min(600, int(os.getenv("REVIEW_TASK_DEADLINE_S", "300"))))
# Nack reasons that repeat identically on every attempt, so an exhausted review row is left alone.
DETERMINISTIC_ERRORS = {"SourceUnavailable", "ValueError", "TypeError", "KeyError"}
metadata = MetaData()
corrections = Table("freight_review_correction", metadata,
    Column("lineage_id", String(36), primary_key=True),
    Column("outbox_id", String(200), nullable=False),
    Column("source_task_id", String(36), nullable=False),
    Column("correction_task_id", String(36), nullable=False, unique=True),
    Column("agent", String(80), nullable=False))


def priority_rank(row: dict) -> int:
    """First hops rank by their envelope priority (P0 first); review rows and unreadable payloads rank P2."""
    if row.get("kind") == FIRST_HOP:
        try:
            return PRIORITY_RANK.get(json.loads(row["payload_json"])["envelope"]["priority"], DEFAULT_RANK)
        except (KeyError, TypeError, ValueError):
            pass
    return DEFAULT_RANK


def nack_reason(error: Exception) -> str:
    """Exception type (no server or destination text); a reducer rejection is marked as deterministic.

    A SourceUnavailable also names its code-authored reason in parentheses, so an operator can tell a missing
    source record from a missing task history. The type stays the first word, which ``exhausted`` reads."""
    name = type(error).__name__
    if isinstance(error, SpacetimeHTTPError):
        return f"{name} {error.status_code}" + (" rejected" if error.status_code == REDUCER_REJECTED else "")
    if isinstance(error, SourceUnavailable) and str(error):
        return f"{name} ({str(error)[:160]})"
    return name


def backoff_seconds(row: dict) -> int:
    """Wait after a failed attempt: 2, 4, 8 ... seconds, capped at 300 (15 for a first hop); none before the first."""
    attempts = int(row.get("attempts", 0))
    cap = FIRST_HOP_BACKOFF_CAP_SECONDS if row.get("kind") == FIRST_HOP else BACKOFF_CAP_SECONDS
    return min(2 ** min(attempts, 16), cap) if attempts > 0 else 0


def _timestamp(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def leased(row: dict, now: datetime) -> bool:
    expires = _timestamp(row.get("lease_expires_at"))
    return row.get("status") == "processing" and expires is not None and expires > now


def backing_off(row: dict, now: datetime) -> bool:
    updated = _timestamp(row.get("updated_at"))
    return updated is not None and (now - updated).total_seconds() < backoff_seconds(row)


def exhausted(row: dict, max_attempts: int) -> bool:
    """A review row that keeps failing the same way; first hops are never given up on."""
    return (row.get("kind") in REVIEW_KINDS and int(row.get("attempts", 0)) >= max_attempts
            and str(row.get("last_error", "")).partition(" ")[0] in DETERMINISTIC_ERRORS)


def _count(name: str, default: str, minimum: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        value = minimum - 1
    if value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def poll_seconds() -> float:
    try:
        seconds = float(os.getenv("DISPATCH_POLL_SECONDS", "1"))
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("DISPATCH_POLL_SECONDS must be a positive number of seconds")
    return seconds


def require_credentials() -> None:
    """Live delivery authenticates to the agents and to SpacetimeDB as the router; fail before polling."""
    if os.getenv("FREIGHTOPS_MODE", "fixture") != "live":
        return
    missing = [name for name in ("A2A_TOKEN_ROUTER", "SPACETIME_TOKEN_ROUTER") if not os.getenv(name)]
    if missing:
        raise ValueError(f"live outbox delivery requires {' and '.join(missing)} (the router service identity)")


class ReviewWorker:
    def __init__(self, *, db=None, store=None, submit=None, worker_id: str | None = None):
        self.db = db or SpacetimeClient(actor="router")
        # Every worker authenticates as the router, and the module keys write receipts by identity and write_id.
        # A claim write_id shared by two workers made the second claim a receipt replay that reported success, so
        # both delivered the row (Oct 25: a second fleet ran the same tasks). This id keeps a claim this worker's.
        self.worker_id = worker_id or uuid.uuid4().hex
        self.store = store or RuntimeStore()
        self.submit = submit or send_task
        self.max_review_claims = _count("DISPATCH_MAX_REVIEW_CLAIMS", "20", 0)
        self.review_max_attempts = _count("REVIEW_MAX_ATTEMPTS", "10", 1)
        self._initialized = False

    async def initialize(self):
        if not self._initialized:
            await self.store.initialize()
            async with self.store.schema_lock(), self.store.engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
            self._initialized = True

    async def lineage(self, task_id: str, case_id: str) -> str:
        visited = set()
        while task_id:
            if task_id in visited or len(visited) >= 100:
                raise ValueError("review task lineage is cyclic or too long")
            visited.add(task_id)
            async with self.store.engine.connect() as conn:
                corrected = (await conn.execute(select(corrections.c.lineage_id).where(
                    corrections.c.correction_task_id == task_id))).scalar_one_or_none()
            if corrected:
                return corrected
            original = await self.store.get(task_id)
            if not original or original["envelope"]["case_id"] != case_id:
                raise SourceUnavailable("original review lineage is unavailable or crosses cases")
            parent = original["delegation"].get("parent_task_id")
            if not parent:
                return task_id
            task_id = parent
        raise SourceUnavailable("review lineage has no root")

    async def reserve_correction(self, row: dict, target: dict) -> tuple[TaskEnvelope, DelegationMeta] | None:
        original = await self.store.get(target["task_id"])
        if not original or original["status"] not in {"completed", "failed", "canceled", "rejected"}:
            raise SourceUnavailable("terminal source task history is unavailable")
        env = TaskEnvelope.model_validate(original["envelope"])
        if env.case_id != row["case_id"] or original["agent"] != target["agent"]:
            raise ValueError("correction task identity does not match the stored source")
        if env.task_type not in AGENTS[target["agent"]].skills:
            raise ValueError("correction source skill is not owned by the destination")
        root = await self.lineage(target["task_id"], env.case_id)
        key = f"review-correction:{root}"
        meta = DelegationMeta(case_id=env.case_id, from_agent="router", idempotency_key=key,
                              correlation_id=env.case_id, causation_id=row["id"], priority=env.priority)
        correction_id = task_id_for(target["agent"], "router", env.case_id, key)
        try:
            async with self.store.engine.begin() as conn:
                await conn.execute(insert(corrections).values(lineage_id=root, outbox_id=row["id"],
                    source_task_id=target["task_id"], correction_task_id=correction_id, agent=target["agent"]))
        except IntegrityError:
            async with self.store.engine.connect() as conn:
                existing = (await conn.execute(select(corrections).where(
                    corrections.c.lineage_id == root))).mappings().one()
            if existing["outbox_id"] != row["id"]:
                return None
            if existing["source_task_id"] != target["task_id"] or existing["agent"] != target["agent"]:
                raise ValueError("correction reservation conflicts with original delivery")
        return env, meta

    async def deliver_first_hop(self, row: dict, payload: dict) -> str:
        """Push a routed event to its agent and record the accepted task before the caller acknowledges."""
        env = TaskEnvelope.model_validate(payload["envelope"])
        event_id, agent = payload["event_id"], payload["agent"]
        if agent != row.get("target_agent") or env.case_id != payload["case_id"] or env.task_type != payload["skill"]:
            raise ValueError("first hop route does not match its outbox row and envelope")
        meta = first_hop_meta(event_id, env)
        if not await self.db.query_rows(
                f"SELECT id FROM source_record WHERE id = {encoded(f'case:{env.case_id}')}"):
            raise SourceUnavailable("first hop case has no source record yet")
        # A redelivery after a lost acknowledgement hits the agent's event:<event_id> key and returns the same task.
        accepted = await self.submit(agent, env, meta)
        expected_id = task_id_for(agent, meta.from_agent, env.case_id, meta.idempotency_key)
        if accepted.id != expected_id or accepted.context_id != env.case_id:
            raise ValueError("A2A acceptance does not match the stable first-hop task ID")
        # Every field is fixed by the outbox row, so a retry after a lost response is a receipt no-op rather than
        # a second observation that would turn the accepted row into a duplicate.
        await self.db.mutate("record_dispatch", {
            "write_id": stable_id("first-hop-accepted", row["id"]), "event_id": event_id,
            "case_id": env.case_id, "agent": agent, "skill": env.task_type, "task_id": accepted.id,
            "status": "accepted", "reason": "delivered by dispatch worker", "queue_ms": payload["queue_ms"],
            "policy_version": payload["policy_version"]})
        return "accepted"

    async def deliver(self, row: dict) -> str:
        """Return only after durable destination acceptance or an explicit safe suppression."""
        await self.initialize()
        kind = row["kind"]
        payload = json.loads(row.get("payload_json", "{}"))
        if not isinstance(payload, dict):
            raise TypeError("review outbox payload must be an object")
        if kind == FIRST_HOP:
            return await self.deliver_first_hop(row, payload)
        if kind == "review_followup":
            if row.get("target_agent") not in FINANCIAL_AGENTS:
                raise ValueError("review correction has an unsupported destination")
            cases = await lookup(self.db, "case", "case_id", row["case_id"])
            if not cases:
                raise SourceUnavailable("correction case is missing")
            if cases[0].get("status") == "human_owned":
                return "human_owned"
            if "rerun_task" not in payload.get("actions", []):
                return "no_rerun_requested"
            targets = await lookup(self.db, "task", "task_id", row["task_id"])
            if not targets or targets[0].get("agent") != row["target_agent"]:
                raise SourceUnavailable("correction target does not match its task")
            correction = await self.reserve_correction(row, targets[0])
            if correction is None:
                return "lineage_correction_limit"
            env, meta = correction
            destination = row["target_agent"]
        elif kind in {"review_output", "review_flag"}:
            if row.get("target_agent") != "validator":
                raise ValueError("reviews must target Settlement Quality Review")
            subject = {"target_task_id": row.get("task_id", "")}
            case_id = row.get("case_id", "")
            if kind == "review_flag":
                flags = await lookup(self.db, "flag", "id", str(payload.get("flag_id", "")))
                if not flags:
                    raise SourceUnavailable("queued flag is missing")
                flag = flags[0]
                subject = {"flag_id": flag["id"]}
                target_id = await flag_target(self.db, flag)
                if target_id:
                    target = await lookup(self.db, "task", "task_id", target_id)
                    case_id = case_id or (target[0]["case_id"] if target else "")
                if fixture_mode():
                    subject["flag"] = flag
                    subject["target_task_id"] = target_id or ""
            if not case_id:
                raise SourceUnavailable("review flag needs a case with resolvable task provenance")
            if fixture_mode():
                targets = await lookup(self.db, "task", "task_id", subject.get("target_task_id", ""))
                if targets:
                    subject["target_task"] = targets[0]
                    subject["artifact"] = json.loads(targets[0].get("artifact_json", "{}"))
                    original = await self.store.get(targets[0]["task_id"])
                    if original:
                        subject["source"] = await SourceRepository().load(
                            TaskEnvelope.model_validate(original["envelope"]))
            env = TaskEnvelope(task_type=kind, case_id=case_id, subject=subject, deadline_s=REVIEW_DEADLINE_S)
            meta = DelegationMeta(case_id=case_id, from_agent="router", idempotency_key=f"outbox:{row['id']}",
                                  correlation_id=case_id, causation_id=row["id"])
            destination = "validator"
        else:
            raise ValueError("unsupported review outbox kind")
        accepted = await self.submit(destination, env, meta)
        expected_id = task_id_for(destination, meta.from_agent, env.case_id, meta.idempotency_key)
        if accepted.id != expected_id or accepted.context_id != env.case_id:
            raise ValueError("A2A acceptance does not match the stable review task ID")
        return "accepted"

    async def claim_and_deliver(self, row: dict) -> bool:
        """One claim/deliver/ack cycle; False when the row was not delivered (it stays pending or leased)."""
        attempt = str(int(row.get("attempts", 0)) + 1)
        try:
            await self.db.mutate("claim_outbox", {"id": row["id"], "lease_seconds": LEASE_SECONDS,
                "write_id": stable_id("review-claim", row["id"], attempt, self.worker_id)})
        except Exception as error:  # noqa: BLE001 -- competing leases and outages retry on the next poll
            log.warning("outbox lease unavailable for %s: %s", row["id"], nack_reason(error))
            return False
        try:
            outcome = await self.deliver(row)
            await self.db.mutate("ack_outbox", {"id": row["id"], "success": True,
                "write_id": stable_id("review-ack", row["id"], attempt)})
            log.info("outbox delivery %s: %s", row["id"], outcome)
            return True
        except Exception as error:  # noqa: BLE001 -- retain outbox work after any destination failure
            # A repeated stable A2A key reconciles uncertain acceptance on retry.
            reason = nack_reason(error)
            log.warning("outbox delivery retry %s: %s", row["id"], reason)
            try:
                await self.db.mutate("ack_outbox", {"id": row["id"], "success": False, "error": reason,
                    "write_id": stable_id("review-nack", row["id"], attempt)})
            except Exception:  # noqa: BLE001 -- an uncertain acknowledgement recovers after lease expiry
                log.warning("outbox lease for %s will expire before retry", row["id"])
            return False

    async def run_once(self) -> int:
        """Deliver every due first hop, then at most `max_review_claims` review rows; return the delivered count."""
        await self.initialize()
        items = await self.db.query_rows("SELECT * FROM routing_outbox WHERE status <> 'done'")
        now = datetime.now(UTC)
        candidates = sorted((row for row in items if row.get("kind") in REVIEW_KINDS | {FIRST_HOP}),
                            key=lambda row: (priority_rank(row), row.get("kind") != FIRST_HOP,
                                             str(row.get("created_at", ""))))
        counts = Counter()
        # First hops go first even when they rank below a review row, so a review backlog never delays intake.
        for phase in (FIRST_HOP, "review"):
            for row in candidates:
                if (row["kind"] == FIRST_HOP) != (phase == FIRST_HOP):
                    continue
                if leased(row, now):
                    counts[f"{phase}_leased"] += 1
                elif exhausted(row, self.review_max_attempts):
                    counts["review_exhausted"] += 1
                elif backing_off(row, now):
                    counts[f"{phase}_backoff"] += 1
                elif phase == "review" and counts["review_claimed"] >= self.max_review_claims:
                    counts["review_deferred"] += 1
                else:
                    counts[f"{phase}_claimed"] += 1
                    counts[f"{phase}_delivered"] += await self.claim_and_deliver(row)
        level = logging.INFO if counts["first_hop_claimed"] or counts["review_claimed"] else logging.DEBUG
        log.log(level, "outbox pass: first_hop due=%d delivered=%d backoff=%d leased=%d; review claimed=%d "
                "delivered=%d backoff=%d exhausted=%d deferred=%d leased=%d",
                *(counts[key] for key in ("first_hop_claimed", "first_hop_delivered", "first_hop_backoff",
                                          "first_hop_leased", "review_claimed", "review_delivered",
                                          "review_backoff", "review_exhausted", "review_deferred",
                                          "review_leased")))
        return counts["first_hop_delivered"] + counts["review_delivered"]


async def main():
    interval = poll_seconds()
    require_credentials()
    worker = ReviewWorker()
    if worker.db.disabled:
        raise ValueError("outbox delivery requires SPACETIME_DISABLED=0 and the router service identity")
    try:
        while True:
            try:
                await worker.run_once()
            except Exception as error:  # noqa: BLE001 -- daemon must survive temporary transport outages
                log.warning("outbox unavailable: %s", nack_reason(error))
            await asyncio.sleep(interval)
    finally:
        await worker.store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
