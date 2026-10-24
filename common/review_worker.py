"""Deliver W2 review outbox work separately from W1 business-event intake.

Run ``python -m common.review_worker`` with the router service credential and
the same A2A runtime database used by the agent services. Acceptance, retries
and corrections use stable IDs; terminal tasks are never reopened.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import Column, MetaData, String, Table, insert, select
from sqlalchemy.exc import IntegrityError

from agents.validator.tools import FINANCIAL_AGENTS, flag_target, lookup
from common.a2a_transport import send_task
from common.config import AGENTS
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime_store import RuntimeStore, stable_id, task_id_for
from common.source_repository import SourceRepository, SourceUnavailable, fixture_mode
from common.spacetime_client import SpacetimeClient

log = logging.getLogger(__name__)
metadata = MetaData()
corrections = Table("freight_review_correction", metadata,
    Column("lineage_id", String(36), primary_key=True),
    Column("outbox_id", String(200), nullable=False),
    Column("source_task_id", String(36), nullable=False),
    Column("correction_task_id", String(36), nullable=False, unique=True),
    Column("agent", String(80), nullable=False))


class ReviewWorker:
    def __init__(self, *, db=None, store=None, submit=None):
        self.db = db or SpacetimeClient(actor="router")
        self.store = store or RuntimeStore()
        self.submit = submit or send_task
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

    async def deliver(self, row: dict) -> str:
        """Return only after durable destination acceptance or an explicit safe suppression."""
        await self.initialize()
        kind = row["kind"]
        payload = json.loads(row.get("payload_json", "{}"))
        if not isinstance(payload, dict):
            raise TypeError("review outbox payload must be an object")
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
            env = TaskEnvelope(task_type=kind, case_id=case_id, subject=subject)
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

    async def run_once(self) -> int:
        await self.initialize()
        items = await self.db.query_rows("SELECT * FROM routing_outbox WHERE status <> 'done'")
        delivered = 0
        for row in items:
            if row.get("kind") not in {"review_output", "review_flag", "review_followup"}:
                continue
            if row.get("status") == "processing":
                expires = datetime.fromisoformat(row["lease_expires_at"])
                if expires > datetime.now(UTC):
                    continue
            attempt = str(int(row.get("attempts", 0)) + 1)
            try:
                await self.db.mutate("claim_outbox", {"id": row["id"], "lease_seconds": 60,
                    "write_id": stable_id("review-claim", row["id"], attempt)})
            except Exception as error:  # noqa: BLE001 -- competing leases and outages retry on the next poll
                log.warning("review lease unavailable: %s", type(error).__name__)
                continue
            try:
                outcome = await self.deliver(row)
                await self.db.mutate("ack_outbox", {"id": row["id"], "success": True,
                    "write_id": stable_id("review-ack", row["id"], attempt)})
                log.info("review delivery %s: %s", row["id"], outcome)
                delivered += 1
            except Exception as error:  # noqa: BLE001 -- retain outbox work after any destination failure
                # A repeated stable A2A key reconciles uncertain acceptance on retry.
                log.warning("review delivery retry: %s", type(error).__name__)
                try:
                    await self.db.mutate("ack_outbox", {"id": row["id"], "success": False,
                        "error": type(error).__name__,
                        "write_id": stable_id("review-nack", row["id"], attempt)})
                except Exception:  # noqa: BLE001 -- an uncertain acknowledgement recovers after lease expiry
                    log.warning("review lease will expire before retry")
        return delivered


async def main():
    worker = ReviewWorker()
    if worker.db.disabled:
        raise ValueError("review delivery requires SPACETIME_DISABLED=0 and the router service identity")
    try:
        while True:
            try:
                await worker.run_once()
            except Exception as error:  # noqa: BLE001 -- daemon must survive temporary transport outages
                log.warning("review outbox unavailable: %s", type(error).__name__)
            await asyncio.sleep(5)
    finally:
        await worker.store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
