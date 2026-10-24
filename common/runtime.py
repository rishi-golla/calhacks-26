"""Shared executor: durable stages, guarded delegation, and recoverable waits."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

import httpx
from a2a.client.errors import A2AClientError
from a2a.helpers import get_data_parts, new_data_part, new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Task, TaskState
from google.protobuf.json_format import MessageToDict

from common.config import AGENTS, MAX_DELEGATION_DEPTH, MAX_FANOUT, SKILL_OWNER
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.runtime_store import LeaseLost, RuntimeStore, stable_id, task_id_for

log = logging.getLogger(__name__)
TERMINAL_STATES = {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED,
                   TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_REJECTED}
WAIT_STATES = {TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED}
_execution: ContextVar[tuple[str, str, int] | None] = ContextVar("freight_execution", default=None)


class DelegationRefused(ValueError):
    pass


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

    def __init__(self, *, store: RuntimeStore | None = None):
        self.store = store or RuntimeStore()
        self._capacity = asyncio.Semaphore(int(os.getenv("A2A_CONCURRENCY", "4")))
        self.wake_recovery: Callable[[], None] = lambda: None
        self._projection_lock = asyncio.Lock()

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
                await updater.update_status(TaskState.TASK_STATE_WORKING,
                    metadata={"business_status": "investigating", "agent": self.name})
                await self._project(task_id, env, meta, "working", "investigating")
                cached = await self.store.get_step(task_id, "__result")
                if cached is None:
                    async with asyncio.timeout(env.deadline_s):
                        result = await self.handle(env, meta, task_id, updater)
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
                log.warning("%s task %s failed: %s", self.name, task_id, type(error).__name__)
                await updater.add_artifact(parts=[new_data_part({"error": type(error).__name__})],
                    artifact_id=stable_id("artifact", task_id, "error"), name="error")
                await updater.failed()
                try:
                    await self.confirm_state(task_id, TaskState.TASK_STATE_FAILED)
                    final_status = "failed"
                except LeaseLost:
                    final_status = "queued"
                try:
                    await self._project(task_id, env, meta, "failed", "failed")
                except Exception:  # noqa: BLE001 -- retain primary failure if projection is unavailable
                    log.warning("Failed task projection remains unavailable for %s", task_id)
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
                       state: str, business_status: str, result: ResultArtifact | None = None) -> None:
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
        cached = await self.store.get_step(task_id, "final_assessment")
        if cached and isinstance(cached.get("value"), dict):
            inference = cached["value"]
            payload.update(instr_version=inference.get("instr_version", 0),
                           memory_ids=inference.get("memory_ids", []))
        from common.policy import check
        if check("upsert_task", self.name, payload).result != "allow":
            raise PermissionError("task projection denied by policy")
        await self.store.queue_projection(self.name, payload)
        await self.flush_projections(task_id)

    async def flush_projections(self, task_id: str | None = None) -> None:
        """Deliver persisted activity in task order; ambiguous retries keep the same write ID."""
        from common.policy import check
        from common.spacetime_client import SpacetimeClient, SpacetimeHTTPError, SpacetimeTransportError
        blocked = set()
        async with self._projection_lock:
            for event in await self.store.pending_projections(self.name, task_id):
                if event["task_id"] in blocked:
                    continue
                payload = json.loads(event["payload"])
                operation = payload.pop("_operation", "upsert_task")
                if operation not in {"upsert_task", "append_audit"}:
                    raise ValueError("unsupported activity outbox operation")
                if check(operation, self.name, payload).result != "allow":
                    raise PermissionError("task projection denied by policy")
                try:
                    await SpacetimeClient(actor=self.name).mutate(operation, payload)
                    await self.store.acknowledge_projection(event["id"])
                except (SpacetimeTransportError, SpacetimeHTTPError) as error:
                    if isinstance(error, SpacetimeHTTPError) and error.status_code not in {408, 429} and error.status_code < 500:
                        raise
                    blocked.add(event["task_id"])
                    log.warning("Task projection queued for retry: %s", type(error).__name__)

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
        await self.store.queue_projection(self.name, {**document, "_operation": "append_audit"})
        await self.flush_projections(payload["task_id"])

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
        if dependency["state"] in {"failed", "canceled", "rejected"}:
            raise RuntimeError(f"child task {dependency['state']}")

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
        if child.id != child_id or child.context_id != env.case_id:
            raise RuntimeError("child acceptance does not match the reserved task")
        await self.store.finish_dependency(dependency["id"], "accepted", [])
        wait_started = time.monotonic()
        while True:
            if child.status.state in TERMINAL_STATES:
                state = TaskState.Name(child.status.state).removeprefix("TASK_STATE_").lower()
                results = extract_artifact_data(child)
                await self.store.finish_dependency(dependency["id"], state, results)
                if state != "completed":
                    raise RuntimeError(f"child task {state}")
                return results
            if mode in {"park", "fanout"} or child.status.state in WAIT_STATES or time.monotonic() - wait_started > 5:
                await self.park(parent_task_id, "awaiting_other_agent", "child", child_id,
                                {"target": target, "skill": skill})
            await asyncio.sleep(0.2)
            await self._assert_execution(parent_task_id)
            try:
                child = await get_task(target, child_id, sender=self.name)
            except (httpx.HTTPError, A2AClientError):
                await self.park(parent_task_id, "awaiting_other_agent", "child", child_id,
                                {"target": target, "skill": skill, "reason": "peer_unavailable"})

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
