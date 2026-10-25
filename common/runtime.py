"""Shared executor: durable stages, guarded delegation, and recoverable waits."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import datetime

import httpx
from a2a.client.errors import A2AClientError
from a2a.helpers import get_data_parts, new_data_part, new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Task, TaskState
from a2a.utils.errors import (
    A2AError,
    ContentTypeNotSupportedError,
    InvalidParamsError,
    InvalidRequestError,
    MethodNotFoundError,
    UnsupportedOperationError,
    VersionNotSupportedError,
)
from google.protobuf.json_format import MessageToDict

from common.config import AGENTS, MAX_DELEGATION_DEPTH, MAX_FANOUT, SKILL_OWNER
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.runtime_store import LeaseLost, RuntimeStore, projection_target, stable_id, task_id_for, wire_form
from common.source_repository import SourceUnavailable

log = logging.getLogger(__name__)
TERMINAL_STATES = {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED,
                   TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_REJECTED}
WAIT_STATES = {TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED}
_execution: ContextVar[tuple[str, str, int] | None] = ContextVar("freight_execution", default=None)
# The time.monotonic() at which the executor's asyncio.timeout(env.deadline_s) ends the running handler.
_deadline: ContextVar[float | None] = ContextVar("freight_deadline", default=None)


def task_deadline() -> float | None:
    """The executor's absolute deadline (time.monotonic()) for the handler running in this context, or None
    outside an executor run. Retries and optional model calls budget against it, not a clock of their own."""
    return _deadline.get()


TERMINAL_NAMES = frozenset({"completed", "failed", "canceled", "rejected"})
# A peer that answers a delegation with one of these refused that exact request; resending it is refused again.
# Transport failures and InternalError (e.g. "task admitted; retry") stay retryable.
PEER_REFUSALS = (InvalidParamsError, InvalidRequestError, MethodNotFoundError, UnsupportedOperationError,
                 ContentTypeNotSupportedError, VersionNotSupportedError)
# Refusals that a repaired credential, a restart or a moment's wait can clear: never final. "task admitted" is
# what a child running an older build answers while its snapshot is still being written.
RETRYABLE_REFUSALS = ("task admitted", "caller identity does not match", "service authentication",
                      "service configuration is not ready")
CLOSED_DEPENDENCY_STATES = frozenset({"failed", "canceled", "rejected"})


def final_refusal(error: Exception) -> bool:
    """Whether a peer's answer refuses this exact delegation for good, so resending it is pointless."""
    return isinstance(error, PEER_REFUSALS) and not str(error).lower().startswith(RETRYABLE_REFUSALS)
# A reserved child that its service never admitted is closed this long after its delegation deadline.
RESERVATION_GRACE_S = 60.0
_PRIVATE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\+?\d[\d ()-]{6,}\d|\b(?:sk|pk)-[A-Za-z0-9_-]{8,}\b")


def refusal_text(error: Exception) -> str:
    """A peer's refusal as safe, bounded text. Its first line only: a validation error's later lines echo the
    refused input, which can carry case facts. Contact details and keys are redacted all the same."""
    lines = str(error).splitlines()
    return _PRIVATE.sub("[redacted]", lines[0] if lines else type(error).__name__)[:150]


def minimal_state_projection(payload: dict, target: str) -> dict:
    """The same task transition with its state and a compact artifact, for a full terminal row the module refused.

    It carries every column needed to create the row, so it also lands on a database that never saw the task's
    earlier rows. Memory and instruction references, the usual reasons for a refusal, are left out; the artifact
    keeps the outcome so review (queued by the module on this transition) does not see an empty result."""
    keep = ("task_id", "case_id", "agent", "skill", "parent_task_id", "display_id", "a2a_state", "business_status",
            "outcome", "confidence", "model_used")
    row = {key: payload[key] for key in keep if key in payload}
    refs = [ref for ref in payload.get("evidence_refs", []) if isinstance(ref, str) and len(ref) <= 500][:50]
    policies = [item for item in payload.get("policy_ids", []) if isinstance(item, str)][:20]
    if "artifact_json" in payload or "outcome" in payload:
        findings = (payload.get("artifact_json") or {}).get("findings") or {}
        # A failure's own reason is short service-authored text: keep it so a failed row still says why.
        reason = {key: findings[key][:300] for key in ("error", "detail") if isinstance(findings.get(key), str)}
        row.update(evidence_refs=refs, policy_ids=policies, artifact_json={
            "outcome": payload.get("outcome", ""), "confidence": payload.get("confidence", 0),
            "evidence_refs": refs, "policy_ids": policies, "findings": {"details_omitted": True, **reason}})
    return {**row, "write_id": stable_id("projection-state", payload["write_id"]), "_target": target,
            "_fallback": True}


def projection_rejected(status_code: int) -> bool:
    """SpacetimeDB answers 530 when a reducer throws; other 4xx (except timeouts and rate limits) are the
    request itself being refused. Both are deterministic for the same write ID and payload."""
    return status_code == 530 or (400 <= status_code < 500 and status_code not in {408, 429})


class DelegationRefused(ValueError):
    pass


class ChildFailed(RuntimeError):
    """A delegated child ended without a result: it failed or was canceled, or its service never admitted it (the
    peer refused the delegation, or the reservation expired). ``reason`` is short service-authored text."""

    def __init__(self, state: str, reason: str = ""):
        self.state = state
        self.reason = reason[:200]
        super().__init__(f"child task {state}")


def failure_reason(results: list) -> str:
    """The error a failed child or a closed reservation recorded in its result, if any."""
    for item in results if isinstance(results, list) else []:
        if isinstance(item, dict) and isinstance(item.get("error"), str):
            detail = item.get("detail")
            return detail if isinstance(detail, str) and detail else item["error"]
    return ""


def failure_details(error: Exception, deadline_s: float) -> dict:
    """The safe error and detail a task failure records. Every failure carries both: the dashboard and the
    Validator read ``findings.error`` and ``findings.detail`` to say why a task ended without a result."""
    from common.providers import ProviderError
    failure = {"error": type(error).__name__}
    if isinstance(error, ProviderError):  # its message is safe: no response, key or prompt
        detail = str(error)
    elif getattr(error, "reason", ""):  # a refused reducer's, a child's or the guarded workflow's bounded text
        detail = str(error.reason)
    elif isinstance(error, TimeoutError):
        detail = f"task deadline of {deadline_s}s exceeded"
    elif isinstance(error, (ChildFailed, DelegationRefused, SourceUnavailable)):
        # Code-authored: "child task failed", "max delegation depth", "case is absent from the configured source
        # dataset", ... SourceUnavailable never interpolates source values, only fixed text and safe client errors.
        detail = str(error)
    else:
        # Other messages may carry source data or credentials; name the failure's kind only.
        detail = f"{type(error).__name__} raised while executing the task"
    failure["detail"] = detail[:200]
    return failure


def failure_artifact(error: str, detail: str = "", **extra) -> dict:
    """The artifact_json of a task that ended without a result, so the dashboard can show why."""
    findings = {"error": error[:200], **({"detail": detail[:300]} if detail else {}), **extra}
    return {"outcome": "", "confidence": 0, "findings": findings, "evidence_refs": [], "policy_ids": []}


class RecordedFailure(Exception):
    """A previous run of this task failed; only recording the failure in the A2A store remains."""

    def __init__(self, failure: dict):
        self.failure = failure
        super().__init__(failure.get("error", "failed"))


class TaskParked(Exception):
    def __init__(self, business_status: str, wait_kind: str, wait_key: str, details: dict):
        self.business_status = business_status
        self.wait_kind = wait_kind
        self.wait_key = wait_key
        self.details = details
        super().__init__(business_status)


def _meta_dict(message) -> dict:
    md = getattr(message, "metadata", None)
    return MessageToDict(md) if md is not None and hasattr(md, "DESCRIPTOR") else dict(md or {})


class FreightAgent(AgentExecutor):
    name: str = ""
    # Environment settings live service readiness requires for this role.
    live_required_env: tuple[str, ...] = ()

    def __init__(self, *, store: RuntimeStore | None = None):
        self.store = store or RuntimeStore()
        self._capacity = asyncio.Semaphore(int(os.getenv("A2A_CONCURRENCY", "4")))
        self.wake_recovery: Callable[[], None] = lambda: None
        self._projection_lock = asyncio.Lock()
        # Dashboard projections are durable in the outbox the moment they are queued; delivering them is a
        # SpacetimeDB round trip per row (seconds against Maincloud), so by default a task only signals the
        # delivery worker (projection_loop) instead of waiting for the write. A2A_PROJECTION_DELIVERY=inline
        # restores waiting, for tests or a local database.
        self._projection_wakeup = asyncio.Event()
        self.inline_projections = os.getenv("A2A_PROJECTION_DELIVERY", "background") == "inline"
        self._held_warned_at = float("-inf")
        # The server shares its admission lock (build_app), so closing a never-admitted reservation and admitting
        # that same child can never interleave.
        self.admission_lock = asyncio.Lock()

    async def confirm_state(self, task_id: str, state: int) -> None:
        """Bound to the SDK store by build_app; direct handler tests have no SDK queue."""

    async def handle(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str,
                     updater: TaskUpdater) -> ResultArtifact:
        raise NotImplementedError(f"No handler implemented for {self.name}")

    async def _assert_execution(self, task_id: str) -> None:
        execution = _execution.get()
        if execution and execution[0] == task_id:
            await self.store.assert_lease(*execution)

    async def step(self, task_id: str, key: str, operation: Callable[[], Awaitable]) -> object:
        """Checkpoint an idempotent stage. Mutations must also use stable write IDs."""
        await self._assert_execution(task_id)
        cached = await self.store.get_step(task_id, key)
        if cached is not None:
            return cached["value"]
        value = await operation()
        await self._assert_execution(task_id)
        await self.store.set_step(task_id, key, {"value": value})
        return value

    async def park(self, task_id: str, business_status: str, wait_kind: str, wait_key: str,
                   details: dict | None = None) -> None:
        if not business_status.startswith("awaiting_"):
            raise ValueError("park requires an awaiting business status")
        await self._assert_execution(task_id)
        await self.store.update(task_id, wait_kind=wait_kind, wait_key=wait_key, wait_details=details or {})
        raise TaskParked(business_status, wait_kind, wait_key, details or {})

    async def notify_waiters(self, wait_key: str, key: str, data: dict) -> list[str]:
        """Deliver an event to this agent's own tasks parked on wait_key, as an A2A resume would.

        For business waits (documents:<case>) whose task IDs only this agent knows. The input is a
        wakeup; resumed work re-reads trusted sources. Duplicate keys are delivered once.
        """
        woken = []
        for row in await self.store.waiting_on(self.name, wait_key):
            # The recovery loop resumes waiting tasks with pending input, as it does for child results.
            await self.store.put_input(row["id"], wait_key, key, data)
            woken.append(row["id"])
        if woken:
            self.wake_recovery()
        return woken

    async def resume_data(self, task_id: str, wait_key: str) -> dict | None:
        return await self.store.get_input(task_id, wait_key)

    async def _renew(self, task_id: str, owner: str, generation: int, worker: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.store.renew(task_id, owner, generation)
            except LeaseLost:
                worker.cancel()
                return

    async def execute(self, ctx: RequestContext, q: EventQueue) -> None:
        task_id = ctx.task_id
        row = await self.store.get(task_id)
        if row is None:
            raise ValueError("task has no durable admission")
        env = TaskEnvelope.model_validate(row["envelope"])
        meta = DelegationMeta.model_validate(row["delegation"])
        updater = TaskUpdater(q, task_id, env.case_id)
        if ctx.current_task is None:
            await q.enqueue_event(new_task(task_id, env.case_id, TaskState.TASK_STATE_SUBMITTED,
                                           history=[ctx.message] if ctx.message else []))
        async with self._capacity:
            owner = str(uuid.uuid4())
            generation = await self.store.claim(task_id, owner)
            if generation is None:
                return
            input_ids = await self.store.pending_inputs(task_id)
            token = _execution.set((task_id, owner, generation))
            heartbeat = asyncio.create_task(self._renew(task_id, owner, generation, asyncio.current_task()))
            final_status = "queued"
            try:
                if env.task_type not in AGENTS[self.name].skills:
                    raise ValueError("skill is not owned by this service")
                recorded = await self.store.get_step(task_id, "__failure")
                if recorded is not None:
                    # An earlier run already failed (and projected it) but the A2A store did not record it:
                    # finish recording that failure instead of running the task again.
                    raise RecordedFailure(recorded)
                await updater.update_status(TaskState.TASK_STATE_WORKING,
                    metadata={"business_status": "investigating", "agent": self.name})
                await self._project(task_id, env, meta, "working", "investigating")
                cached = await self.store.get_step(task_id, "__result")
                if cached is None:
                    deadline = _deadline.set(time.monotonic() + env.deadline_s)
                    try:
                        async with asyncio.timeout(env.deadline_s):
                            result = await self.handle(env, meta, task_id, updater)
                    finally:
                        _deadline.reset(deadline)
                    result = ResultArtifact.model_validate(result)
                    if result.outcome == "not_implemented":
                        raise ValueError("unimplemented skills cannot complete successfully")
                    await self._assert_execution(task_id)
                    await self.store.set_step(task_id, "__result", result.model_dump())
                else:
                    result = ResultArtifact.model_validate(cached)
                await self._project(task_id, env, meta, "completed", "resolved", result)
                await updater.add_artifact(parts=[new_data_part(result.model_dump())],
                    artifact_id=stable_id("artifact", task_id, "result"), name=f"{self.name}_result")
                await updater.update_status(TaskState.TASK_STATE_COMPLETED,
                    metadata={"business_status": "resolved", "agent": self.name})
                await self.confirm_state(task_id, TaskState.TASK_STATE_COMPLETED)
                final_status = "completed"
            except TaskParked as wait:
                await self._project(task_id, env, meta, "input-required", wait.business_status)
                await updater.update_status(TaskState.TASK_STATE_INPUT_REQUIRED, metadata={
                    "business_status": wait.business_status, "agent": self.name,
                    "wait_kind": wait.wait_kind, "wait_key": wait.wait_key, **wait.details,
                })
                try:
                    await self.confirm_state(task_id, TaskState.TASK_STATE_INPUT_REQUIRED)
                    final_status = "waiting"
                except LeaseLost:
                    final_status = "queued"
            except asyncio.CancelledError:
                current = await self.store.get(task_id)
                final_status = "canceled" if current and current["status"] == "canceled" else "queued"
                raise
            except LeaseLost:
                final_status = "queued"
            except Exception as error:  # noqa: BLE001 -- executor boundary records a safe failure artifact
                replay = isinstance(error, RecordedFailure)
                if replay:
                    failure = error.failure
                    log.info("%s task %s: recording its earlier failure in the A2A store", self.name, task_id)
                else:
                    failure = failure_details(error, env.deadline_s)
                    log.warning("%s task %s failed: %s (%s)", self.name, task_id, failure["error"],
                                failure["detail"])
                # Record the failure durably and queue the dashboard's terminal row before any SDK call that could
                # raise, so a failure the SDK does not record still never leaves the SpacetimeDB task `working`.
                # Only the current lease holder may declare the task failed.
                try:
                    await self._assert_execution(task_id)
                    owns_lease = True
                except LeaseLost:
                    owns_lease = False
                if owns_lease and not replay:
                    try:
                        await self.store.set_step(task_id, "__failure", failure)
                        await self._project(task_id, env, meta, "failed", "failed", failure=failure)
                    except Exception:  # noqa: BLE001 -- retain primary failure if projection is unavailable
                        log.warning("Failed task projection remains unavailable for %s", task_id)
                try:
                    await updater.add_artifact(parts=[new_data_part(failure)],
                        artifact_id=stable_id("artifact", task_id, "error"), name="error")
                    await updater.failed()
                    await self.confirm_state(task_id, TaskState.TASK_STATE_FAILED)
                    final_status = "failed" if owns_lease else "queued"
                except LeaseLost:
                    final_status = "queued"
                except Exception as sdk_error:
                    # Stay recoverable until the A2A snapshot is terminal. Raising lets the SDK persist the failed
                    # snapshot itself; recovery then copies that terminal state into this store. If even that
                    # write fails, the next run finds `__failure` and records it without executing the task again.
                    log.warning("%s task %s failure could not be recorded through the A2A event queue: %s",
                                self.name, task_id, type(sdk_error).__name__)
                    final_status = "queued"
                    raise
            finally:
                async def cleanup():
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                    if final_status in {"waiting", "completed", "failed"}:
                        await self.store.consume_inputs(task_id, input_ids)
                    await self.store.release(task_id, owner, generation, status=final_status)
                    self.wake_recovery()
                cleanup_task = asyncio.create_task(cleanup())
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await cleanup_task
                    raise
                finally:
                    _execution.reset(token)

    async def _project(self, task_id: str, env: TaskEnvelope, meta: DelegationMeta,
                       state: str, business_status: str, result: ResultArtifact | None = None,
                       failure: dict | None = None) -> None:
        if os.getenv("SPACETIME_DISABLED", "1") == "1":
            return
        execution = _execution.get()
        generation = str(execution[2]) if execution else "0"
        payload = {"write_id": stable_id("projection", task_id, generation, state, business_status),
            "task_id": task_id, "case_id": env.case_id, "agent": self.name, "skill": env.task_type,
            "a2a_state": state, "business_status": business_status,
            "parent_task_id": meta.parent_task_id or "", "display_id": env.display_id or ""}
        if result:
            payload.update(outcome=result.outcome, confidence=result.confidence,
                model_used=result.model_used or "", artifact_json=result.model_dump(),
                evidence_refs=result.evidence_refs, policy_ids=result.policy_ids)
        elif failure:
            payload["artifact_json"] = failure_artifact(str(failure.get("error", "failed")),
                                                        str(failure.get("detail", "")))
        cached = await self.store.get_step(task_id, "final_assessment")
        if cached and isinstance(cached.get("value"), dict):
            inference = cached["value"]
            payload.update(instr_version=inference.get("instr_version", 0),
                           memory_ids=inference.get("memory_ids", []))
        from common.policy import check
        if check("upsert_task", self.name, payload).result != "allow":
            raise PermissionError("task projection denied by policy")
        await self.store.queue_projection(self.name, {**payload, "_target": projection_target()})
        await self.deliver_projections(task_id)

    async def deliver_projections(self, task_id: str) -> None:
        """Queued rows are durable already; wait for the write only when inline delivery is configured."""
        if self.inline_projections:
            await self.flush_projections(task_id)
        else:
            self._projection_wakeup.set()

    async def projection_loop(self, interval: float = 1.0) -> None:
        """Drain the activity outbox off every task's critical path: on each signal and at least once per interval."""
        while True:
            self._projection_wakeup.clear()
            try:
                await self.flush_projections()
            except Exception as error:  # noqa: BLE001 -- a rejected row is logged and retried, never fatal here
                log.warning("Projection delivery pass deferred: %s", type(error).__name__)
            try:
                await asyncio.wait_for(self._projection_wakeup.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def flush_projections(self, task_id: str | None = None) -> None:
        """Deliver persisted activity in task order; ambiguous retries keep the same write ID. A row the module
        rejects outright blocks only its own task's later rows, so one bad projection cannot stall every task.

        A row is delivered only to the SpacetimeDB database it was written for. Pointing a service at another
        database (a local rehearsal on the same runtime store) leaves the other database's rows pending instead of
        sending them where their tasks do not exist, where every one would be rejected and dropped for good."""
        from common.policy import check
        from common.spacetime_client import SpacetimeClient, SpacetimeHTTPError, SpacetimeTransportError
        blocked = set()
        async with self._projection_lock:
            # One client per pass, and the target it actually writes to, so a row is never sent elsewhere.
            client = SpacetimeClient(actor=self.name)
            current = f"{client.url}/{client.database}"
            await self._warn_held_projections(current)
            for event in await self.store.pending_projections(self.name, task_id, target=current):
                if event["task_id"] in blocked:
                    continue
                payload = json.loads(event["payload"])
                operation = payload.pop("_operation", "upsert_task")
                embedded = payload.pop("_target", None)
                fallback = payload.pop("_fallback", False)
                if embedded and embedded != current:
                    blocked.add(event["task_id"])  # an unmigrated row names another database: never send it here
                    continue
                if operation not in {"upsert_task", "append_audit"}:
                    raise ValueError("unsupported activity outbox operation")
                if check(operation, self.name, payload).result != "allow":
                    raise PermissionError("task projection denied by policy")
                try:
                    await client.mutate(operation, payload)
                    await self.store.acknowledge_projection(event["id"])
                except (SpacetimeTransportError, SpacetimeHTTPError) as error:
                    blocked.add(event["task_id"])
                    if isinstance(error, SpacetimeHTTPError) and projection_rejected(error.status_code):
                        # The module refused this exact write (a reducer that threw, or a malformed row). It is
                        # deterministic, so the row is terminal; the task's later rows get their own attempt next pass.
                        await self.store.reject_projection(event["id"])
                        reason = getattr(error, "reason", "") or "no reason given"
                        terminal = operation == "upsert_task" and payload.get("a2a_state") in TERMINAL_NAMES
                        if terminal and not fallback:
                            # Never drop a task's final state: retry with only the state, which leaves every other
                            # column as stored, so a refused artifact, memory or instruction reference cannot
                            # keep the dashboard showing the task as running.
                            await self.store.queue_projection(self.name, minimal_state_projection(payload, current))
                            log.warning("Terminal task projection %s rejected (HTTP %s: %s); retrying state only",
                                        event["id"], error.status_code, reason)
                        elif terminal:
                            log.error("Task %s final state %s could not be recorded in SpacetimeDB (HTTP %s: %s)",
                                      payload.get("task_id"), payload.get("a2a_state"), error.status_code, reason)
                        else:
                            log.warning("Task projection rejected by SpacetimeDB (HTTP %s: %s); row %s is terminal",
                                        error.status_code, reason, event["id"])
                        continue
                    log.warning("Task projection queued for retry: %s", type(error).__name__)

    async def _warn_held_projections(self, current: str, every_s: float = 300) -> None:
        """Say how many rows wait for another database, at most every `every_s`: they never reach this one."""
        now = time.monotonic()
        if now - self._held_warned_at < every_s:
            return
        self._held_warned_at = now
        held = await self.store.held_projections(self.name, current)
        if held:
            log.warning("%d %s activity rows are held for another SpacetimeDB database; they are delivered when "
                        "this service points at that database again", held, self.name)

    async def sweep_orphaned_tasks(self, *, stale_s: float = 120) -> int:
        """Repair SpacetimeDB task rows left non-terminal although this service's durable execution ended.

        The runtime store is the execution authority. When it says a task is completed, failed or canceled, no
        projection for the task is still pending, and the dashboard row has not moved for ``stale_s``, the row is
        an orphan (its final projection was lost or refused): queue the final state again. A reserved child this
        service never admitted is closed too (see close_unadmitted_reservation). Returns rows repaired."""
        from common.spacetime_client import SpacetimeClient
        if os.getenv("SPACETIME_DISABLED", "1") == "1":
            return 0
        db = SpacetimeClient(actor=self.name)
        name = self.name.replace("'", "''")
        rows = []
        for state in ("submitted", "working", "input-required", "auth-required"):
            rows.extend(await db.query_rows(
                f"SELECT * FROM task WHERE agent = '{name}' AND a_2_a_state = '{state}'"))
        now = time.time()
        target = projection_target()
        repaired = 0
        for row in rows:
            task_id = row.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            try:
                updated = datetime.fromisoformat(str(row.get("updated_at", ""))).timestamp()
            except ValueError:
                continue
            if now - updated < stale_s:
                continue
            execution = await self.store.get(task_id)
            if execution is None and row.get("a2a_state") == "submitted" and row.get("parent_task_id"):
                repaired += await self.close_unadmitted_reservation(row, now, target)
                continue
            if execution is None or execution["status"] not in TERMINAL_NAMES:
                continue
            if await self.store.pending_projections(self.name, task_id, target=target):
                continue  # its own final row is still on the way
            payload = {"task_id": task_id, "case_id": row.get("case_id") or execution["envelope"]["case_id"],
                       "agent": self.name, "skill": row.get("skill") or execution["envelope"]["task_type"],
                       "parent_task_id": row.get("parent_task_id") or "",
                       "a2a_state": execution["status"],
                       "business_status": "resolved" if execution["status"] == "completed" else "failed"}
            if execution["status"] == "rejected":
                payload["business_status"] = "rejected"
            cached = await self.store.get_step(task_id, "__result") if execution["status"] == "completed" else None
            if cached is not None:
                result = ResultArtifact.model_validate(cached)
                payload.update(outcome=result.outcome, confidence=result.confidence,
                               model_used=result.model_used or "", artifact_json=result.model_dump(),
                               evidence_refs=result.evidence_refs, policy_ids=result.policy_ids)
            if execution["status"] == "failed":
                # A failure recorded only in the A2A store (the executor never wrote `__failure`) still says so.
                failure = await self.store.get_step(task_id, "__failure") or {
                    "error": "failed", "detail": "the task failed without a recorded reason"}
                payload["artifact_json"] = failure_artifact(str(failure.get("error") or "failed"),
                                                            str(failure.get("detail") or ""))
            payload["write_id"] = stable_id("projection-sweep", task_id, execution["status"])
            if await self.store.projection_exists(payload["write_id"], target):
                continue  # already repaired once; a second refusal was logged by delivery
            await self.store.queue_projection(self.name, {**payload, "_target": target})
            log.warning("Task %s was %s in SpacetimeDB but %s here; final state queued again",
                        task_id, row.get("a2a_state"), execution["status"])
            repaired += 1
        if repaired:
            self._projection_wakeup.set()
        return repaired

    async def close_unadmitted_reservation(self, row: dict, now: float, target: str) -> int:
        """Record as terminal a child row a parent reserved (``record_delegation`` inserts it ``submitted``) that this
        service never admitted, so no execution of it exists here and no other sweep can end it.

        Only this service may write its own task rows, so the parent cannot end them itself. The row is closed once
        its parent closed the reservation (this service refused the delegation), or once its delegation deadline plus
        a grace period has passed with no admission; that also closes the parent's reservation (only while it is still
        pending), so a late resend is refused instead of running a child whose row already ended. Returns rows queued."""
        task_id = row["task_id"]
        dependency = await self.store.dependency_for_child(task_id)
        if dependency is None or dependency["target"] != self.name:
            return 0  # no reservation here to judge it by: never end a row on missing evidence
        if dependency["state"] not in CLOSED_DEPENDENCY_STATES | {"pending"}:
            return 0  # accepted or completed: the child was admitted, so its own execution owns the row
        # Under the admission lock: an admission of this child either finished before (it has an execution, so the
        # row is left to it) or will see the closed reservation and be refused.
        async with self.admission_lock:
            if await self.store.get(task_id) is not None:
                return 0
            state = dependency["state"]
            if state == "pending":
                deadline = TaskEnvelope.model_validate_json(dependency["envelope"]).deadline_s
                try:
                    reserved = datetime.fromisoformat(
                        str(row.get("started_at") or row.get("updated_at", ""))).timestamp()
                except ValueError:
                    return 0
                if now - reserved < deadline + RESERVATION_GRACE_S:
                    return 0
                state, error = "canceled", "delegation_expired"
                reason = f"{self.name} never admitted this delegation within its {deadline}s deadline"
                if not await self.store.close_dependency(dependency["id"], state,
                                                         [{"error": error, "detail": reason}]):
                    return 0  # it changed meanwhile; the next sweep reads it again
            else:
                error = "delegation_refused" if state == "rejected" else f"delegation_{state}"
                reason = failure_reason(json.loads(dependency["result"])) or f"the delegation was {state}"
        final = "canceled" if state == "canceled" else "failed"
        payload = {"task_id": task_id, "case_id": row.get("case_id", ""), "agent": self.name,
                   "skill": row.get("skill", ""), "parent_task_id": row.get("parent_task_id", ""),
                   "a2a_state": final, "business_status": "failed",
                   "artifact_json": failure_artifact(error, reason, admitted=False),
                   "write_id": stable_id("projection-reservation", task_id, final)}
        if await self.store.projection_exists(payload["write_id"], target):
            return 0
        await self.store.queue_projection(self.name, {**payload, "_target": target})
        log.warning("Reserved task %s was never admitted by %s (%s); recorded %s", task_id, self.name, reason, final)
        return 1

    async def queue_tool_audit(self, payload: dict) -> None:
        """Persist tool telemetry in the task's ordered outbox before best-effort delivery.

        A transient dashboard outage must not replace a completed business outcome or
        a parked dependency. Recovery flushes the same write IDs, including after restart.
        """
        from common.policy import check
        if payload.get("kind") != "tool_call" or not payload.get("task_id") or not payload.get("case_id"):
            raise ValueError("tool audit requires an owned task and case")
        await self._assert_execution(payload["task_id"])
        fields = {"task_id", "case_id", "kind", "reason_code", "summary", "evidence_refs", "policy_ids", "model_used"}
        if set(payload) - fields:
            raise ValueError("unsupported tool audit field")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        document = {**payload, "write_id": stable_id("tool-audit", self.name, canonical)}
        if check("append_audit", self.name, document).result != "allow":
            raise PermissionError("tool audit denied by policy")
        await self.store.queue_projection(self.name, {**document, "_operation": "append_audit",
                                                      "_target": projection_target()})
        await self.deliver_projections(payload["task_id"])

    async def cancel(self, ctx: RequestContext, q: EventQueue) -> None:
        await self.store.cancel(ctx.task_id)
        await TaskUpdater(q, ctx.task_id, ctx.context_id).cancel()

    async def delegate(self, skill: str, env: TaskEnvelope, meta: DelegationMeta, parent_task_id: str,
                       context_id: str | None = None, *, mode: str = "await",
                       invocation_key: str | None = None) -> list[dict]:
        """Send directly to a peer; parked children release the parent's worker."""
        from common.a2a_transport import get_task, send_task
        if mode not in {"await", "park", "fanout"}:
            raise ValueError("invalid delegation mode")
        target = SKILL_OWNER.get(skill)
        if target is None or target not in AGENTS[self.name].may_delegate_to:
            raise DelegationRefused(f"{self.name} may not delegate skill {skill}")
        visited = [*meta.visited, self.name]
        if target in visited:
            raise DelegationRefused("delegation cycle")
        if meta.delegation_depth + 1 > MAX_DELEGATION_DEPTH:
            raise DelegationRefused("max delegation depth")
        if env.task_type != skill or (context_id and context_id != env.case_id) or meta.case_id != env.case_id:
            raise DelegationRefused("delegation skill or case mismatch")
        await self._assert_execution(parent_task_id)
        # Reserve exactly what the child receives: the A2A data part turns every number into a double, and the
        # child checks its envelope against this reservation.
        env = TaskEnvelope.model_validate(wire_form(env.model_dump()))
        invocation = invocation_key or skill
        key = f"{parent_task_id}:{invocation}"
        child_meta = DelegationMeta(case_id=env.case_id, from_agent=self.name, parent_task_id=parent_task_id,
            delegation_depth=meta.delegation_depth + 1, visited=visited, idempotency_key=key,
            priority=meta.priority, correlation_id=meta.correlation_id or env.case_id,
            causation_id=parent_task_id)
        child_id = task_id_for(target, self.name, env.case_id, key)
        try:
            dependency = await self.store.add_dependency(parent_task_id, invocation, target, child_id,
                env.model_dump(), child_meta.model_dump(exclude_none=True), max_fanout=MAX_FANOUT)
        except ValueError as error:
            raise DelegationRefused(str(error)) from error
        if dependency["state"] == "completed":
            return json.loads(dependency["result"])
        if dependency["state"] in CLOSED_DEPENDENCY_STATES:
            raise ChildFailed(dependency["state"], failure_reason(json.loads(dependency["result"])))

        async def reserve():
            if os.getenv("SPACETIME_DISABLED", "1") != "1":
                from common.policy import check
                from common.spacetime_client import SpacetimeClient
                payload = {
                    "write_id": dependency["id"], "id": dependency["id"], "case_id": env.case_id,
                    "parent_task_id": parent_task_id, "task_id": child_id, "to_agent": target, "skill": skill,
                }
                if check("record_delegation", self.name, payload).result != "allow":
                    raise PermissionError("delegation reservation denied by policy")
                await SpacetimeClient(actor=self.name).mutate("record_delegation", payload)
            return {"reserved": True}
        await self.step(parent_task_id, f"reserve:{invocation}", reserve)
        try:
            child = await send_task(target, env, child_meta)
        except (httpx.HTTPError, A2AClientError):
            await self.park(parent_task_id, "awaiting_other_agent", "child", child_id,
                            {"target": target, "skill": skill, "reason": "peer_unavailable"})
        except A2AError as error:
            failure = await self.refuse_dependency(dependency, error) if final_refusal(error) else None
            if failure is not None:
                raise failure from None
            # Transient (InternalError, an identity hiccup) or settled elsewhere: the recovery loop resends or polls.
            await self.park(parent_task_id, "awaiting_other_agent", "child", child_id,
                            {"target": target, "skill": skill, "reason": "peer_unavailable"})
        if child.id != child_id or child.context_id != env.case_id:
            raise RuntimeError("child acceptance does not match the reserved task")
        await self.accept_dependency(dependency)
        wait_started = time.monotonic()
        while True:
            if child.status.state in TERMINAL_STATES:
                state = TaskState.Name(child.status.state).removeprefix("TASK_STATE_").lower()
                results = extract_artifact_data(child)
                await self.store.finish_dependency(dependency["id"], state, results)
                if state != "completed":
                    raise ChildFailed(state, failure_reason(results))
                return results
            if mode in {"park", "fanout"} or child.status.state in WAIT_STATES or time.monotonic() - wait_started > 5:
                await self.park(parent_task_id, "awaiting_other_agent", "child", child_id,
                                {"target": target, "skill": skill})
            await asyncio.sleep(0.2)
            await self._assert_execution(parent_task_id)
            try:
                child = await get_task(target, child_id, sender=self.name)
            except (httpx.HTTPError, A2AError):
                await self.park(parent_task_id, "awaiting_other_agent", "child", child_id,
                                {"target": target, "skill": skill, "reason": "peer_unavailable"})

    async def accept_dependency(self, dependency: dict) -> None:
        """Record the child's admission, unless its reservation was closed meanwhile (then report that instead)."""
        if await self.store.accept_dependency(dependency["id"]):
            return
        current = await self.store.dependency_for_child(dependency["child_id"]) or dependency
        raise ChildFailed(current["state"], failure_reason(json.loads(current["result"])))

    async def refuse_dependency(self, dependency: dict, error: Exception) -> ChildFailed | None:
        """The child's service refused this delegation, so it will never run: close the reservation (the child's
        service then records its reserved row as failed) and report the refusal to the parent's workflow.

        Returns None when the reservation is no longer pending (accepted or completed elsewhere): the refusal was of
        a duplicate, and the parent keeps following the admitted child."""
        reason = f"{dependency['target']} refused the delegation: {refusal_text(error)}"
        log.warning("%s delegation %s was refused by %s: %s", self.name, dependency["child_id"],
                    dependency["target"], refusal_text(error))
        if await self.store.close_dependency(dependency["id"], "rejected",
                                             [{"error": "delegation_refused", "detail": reason}]):
            return ChildFailed("rejected", reason)
        current = await self.store.dependency_for_child(dependency["child_id"])
        if current is not None and current["state"] in CLOSED_DEPENDENCY_STATES:
            return ChildFailed(current["state"], failure_reason(json.loads(current["result"])) or reason)
        return None

    async def fanout(self, envelopes: list[TaskEnvelope], meta: DelegationMeta,
                     parent_task_id: str) -> list[list[dict]]:
        if not 1 <= len(envelopes) <= MAX_FANOUT:
            raise DelegationRefused("max delegation fanout")
        results = await asyncio.gather(*[
            self.delegate(env.task_type, env, meta, parent_task_id, mode="fanout",
                          invocation_key=f"fanout:{index}:{env.task_type}")
            for index, env in enumerate(envelopes)
        ], return_exceptions=True)
        waits = [result for result in results if isinstance(result, TaskParked)]
        failures = [result for result in results if isinstance(result, BaseException) and not isinstance(result, TaskParked)]
        if failures:
            raise failures[0]
        if waits:
            wait = waits[0]
            await self.park(parent_task_id, wait.business_status, wait.wait_kind, wait.wait_key, wait.details)
        return results


def extract_artifact_data(event) -> list[dict]:
    """Extract data from snapshots or updates, including SDK internal Task values."""
    if isinstance(event, Task):
        return [data for artifact in event.artifacts for data in get_data_parts(artifact.parts)]
    if hasattr(event, "HasField"):
        if event.HasField("task"):
            return extract_artifact_data(event.task)
        if event.HasField("artifact_update"):
            return list(get_data_parts(event.artifact_update.artifact.parts))
    return []
