"""FreightAgent runtime: A2A executor, guarded delegation, park/resume, parent callbacks, Validator hook.

Handlers return either a ResultArtifact (task completes) or a Park (task goes INPUT_REQUIRED and frees
the worker). A parked task is woken by a follow-up message on the same task id with metadata
kind=resume (from a child agent, a call webhook or a human); `on_resume` then continues from `case.state`,
which is persisted in the ops store so it survives a restart.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from a2a.helpers import get_data_parts, new_data_message, new_data_part, new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Role, TaskState
from google.protobuf.json_format import MessageToDict

from common import a2a_client, mirror, store
from common.config import AGENTS, MAX_DELEGATION_DEPTH, REVIEWED_AGENTS, SKILL_OWNER
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope

log = logging.getLogger(__name__)
_background: set[asyncio.Task] = set()


class DelegationRefused(Exception):
    pass


@dataclass
class Park:
    reason: str
    waiting_on: str = ""
    state: dict = field(default_factory=dict)


@dataclass
class Delegation:
    skill: str
    agent: str
    state: str  # completed | parked | failed
    child_task_id: str | None = None
    artifacts: list[dict] = field(default_factory=list)

    @property
    def parked(self) -> bool:
        return self.state == "parked"

    @property
    def result(self) -> dict:
        return self.artifacts[-1] if self.artifacts else {}


@dataclass
class Case:
    agent: "FreightAgent"
    env: TaskEnvelope
    meta: DelegationMeta
    task_id: str
    context_id: str
    updater: TaskUpdater
    state: dict = field(default_factory=dict)
    resume: dict | None = None

    @property
    def subject(self) -> dict:
        return self.env.subject

    async def status(self, business_status: str, **md) -> None:
        await self.updater.update_status(TaskState.TASK_STATE_WORKING,
                                         metadata={"business_status": business_status, "agent": self.agent.name, **md})
        mirror.emit("upsert_task", self.task_id, self.env.case_id, self.agent.name, business_status, md)

    def log(self, kind: str, **detail) -> None:
        store.log_event(self.env.case_id, self.agent.name, self.task_id, kind, **detail)

    async def delegate(self, skill: str, subject: dict | None = None, evidence_refs: list[str] | None = None) -> Delegation:
        env = TaskEnvelope(task_type=skill, case_id=self.env.case_id, display_id=self.env.display_id,
                           subject=self.env.subject if subject is None else subject,
                           evidence_refs=self.env.evidence_refs if evidence_refs is None else evidence_refs,
                           priority=self.env.priority)
        return await self.agent.delegate(skill, env, self.meta, self.task_id, self.context_id)

    def commit(self, action: str, cells: list[dict], policy_ids: list[str] | None = None,
               sources: list[str] | None = None) -> dict:
        """Write a Git-style audit change set for a state change (PRD audit model)."""
        cs = store.commit_change_set(case_id=self.env.case_id, agent=self.agent.name, task_id=self.task_id,
                                     action=action, cells=cells, policy_ids=policy_ids,
                                     sources=sources if sources is not None else self.env.evidence_refs,
                                     instruction_version=self.agent.instruction_version(),
                                     memory_ids=[m["id"] for m in store.memories(self.agent.name)])
        mirror.emit("record_change_set", cs)
        return cs


def _meta_dict(msg) -> dict:
    md = getattr(msg, "metadata", None)
    if md is None:
        return {}
    return MessageToDict(md) if hasattr(md, "DESCRIPTOR") else dict(md)


def _meta_from(raw: dict, env: TaskEnvelope, context_id: str) -> DelegationMeta:
    return DelegationMeta(case_id=raw.get("case_id", env.case_id), from_agent=raw.get("from_agent", "router"),
                          parent_task_id=raw.get("parent_task_id"), correlation_id=raw.get("correlation_id"),
                          delegation_depth=int(raw.get("delegation_depth", 0)), visited=list(raw.get("visited", [])),
                          idempotency_key=raw.get("idempotency_key"), priority=raw.get("priority", env.priority),
                          context_id=raw.get("context_id", context_id))


def spawn(coro) -> None:
    t = asyncio.get_running_loop().create_task(coro)
    _background.add(t)
    t.add_done_callback(_background.discard)


class FreightAgent(AgentExecutor):
    name: str = ""

    async def handle(self, case: Case) -> ResultArtifact | Park:
        return ResultArtifact(outcome="not_implemented", findings={"agent": self.name})

    async def on_resume(self, case: Case) -> ResultArtifact | Park:
        return ResultArtifact(outcome="info", findings={"resumed_with": case.resume})

    # ---- instructions + memory (Validator-managed, human-revertable) ----
    def instructions(self) -> str:
        p = Path(__file__).parent.parent / "agents" / self.name / "instructions.md"
        text = p.read_text() if p.exists() else ""
        patches = store.instruction_patches(self.name)
        if patches:
            text += "\n\n## Learned rules (Validator)\n" + "\n".join(f"- (v{x['version']}) {x['patch']}" for x in patches)
        mems = store.memories(self.name)
        if mems:
            text += "\n\n## Memory\n" + "\n".join(f"- {m['text']}" for m in mems[-20:])
        return text

    def instruction_version(self) -> int:
        patches = store.instruction_patches(self.name)
        return patches[-1]["version"] if patches else 1

    # ---- A2A executor ----
    async def execute(self, ctx: RequestContext, q: EventQueue) -> None:
        is_new = ctx.current_task is None
        task = ctx.current_task or new_task_from_user_message(ctx.message)
        if is_new:
            await q.enqueue_event(task)
        u = TaskUpdater(q, task.id, task.context_id)
        parts = get_data_parts(ctx.message.parts)
        saved = None if is_new else store.load_task_state(task.id)
        if saved:
            env = TaskEnvelope.model_validate(saved["env"])
            meta = DelegationMeta.model_validate(saved["meta"])
            case = Case(self, env, meta, task.id, task.context_id, u, state=saved["state"] or {},
                        resume=parts[0] if parts else {})
        else:
            env = TaskEnvelope.model_validate(parts[0])
            meta = _meta_from(_meta_dict(ctx.message), env, task.context_id)
            case = Case(self, env, meta, task.id, task.context_id, u)
        resuming = saved is not None
        case.log("resumed" if resuming else "started", task_type=env.task_type, from_agent=meta.from_agent,
                 parent_task_id=meta.parent_task_id, depth=meta.delegation_depth)
        await case.status("investigating")
        try:
            out = await (self.on_resume(case) if resuming else self.handle(case))
        except Exception as e:
            log.exception("%s failed", self.name)
            case.log("failed", error=str(e))
            err = ResultArtifact(outcome="failed", findings={"error": str(e), "agent": self.name})
            await u.add_artifact(parts=[new_data_part(err.model_dump())], name="error")
            await u.failed()
            if resuming:
                spawn(self._notify_parent(case, err, "failed"))
            return
        if isinstance(out, Park):
            store.save_task_state(task.id, self.name, env.model_dump(), meta.model_dump(), out.state)
            case.log("parked", reason=out.reason, waiting_on=out.waiting_on)
            mirror.emit("upsert_task", task.id, env.case_id, self.name, out.reason, {"waiting_on": out.waiting_on})
            await u.update_status(
                TaskState.TASK_STATE_INPUT_REQUIRED,
                message=new_data_message({"parked": out.reason, "waiting_on": out.waiting_on}, role=Role.ROLE_AGENT,
                                         task_id=task.id, context_id=task.context_id),
                metadata={"business_status": out.reason, "waiting_on": out.waiting_on, "agent": self.name})
            return
        await u.add_artifact(parts=[new_data_part(out.model_dump())], name=f"{self.name}_result")
        await u.complete()
        case.log("completed", outcome=out.outcome, policy_ids=out.policy_ids)
        mirror.emit("upsert_task", task.id, env.case_id, self.name, "resolved", {"outcome": out.outcome})
        if resuming:
            spawn(self._notify_parent(case, out, "completed"))
        if self.name in REVIEWED_AGENTS:
            spawn(self._request_review(case, out))

    async def cancel(self, ctx: RequestContext, q: EventQueue) -> None:
        await TaskUpdater(q, ctx.task_id, ctx.context_id).cancel()

    async def _notify_parent(self, case: Case, out: ResultArtifact, state: str) -> None:
        """A child that completes after parking calls back into its parked parent task."""
        parent, ptask = case.meta.from_agent, case.meta.parent_task_id
        if parent not in AGENTS or not ptask:
            return
        ok = await a2a_client.resume_task(parent, ptask, case.meta.context_id or case.context_id, {
            "kind": "child_result", "from_agent": self.name, "skill": case.env.task_type,
            "child_task_id": case.task_id, "state": state, "result": out.model_dump()})
        case.log("callback_sent" if ok else "callback_failed", to=parent, parent_task_id=ptask)

    async def _request_review(self, case: Case, out: ResultArtifact) -> None:
        env = TaskEnvelope(task_type="review_output", case_id=case.env.case_id, display_id=case.env.display_id,
                           subject={"agent": self.name, "task_id": case.task_id, "task_type": case.env.task_type,
                                    "input": case.env.subject, "result": out.model_dump()},
                           priority="P3")
        try:
            await a2a_client.start_task("validator", env.model_dump(),
                                        {"case_id": env.case_id, "from_agent": self.name,
                                         "parent_task_id": case.task_id, "context_id": case.context_id},
                                        context_id=case.context_id)
            case.log("review_requested")
        except Exception as e:  # noqa: BLE001 - Validator down must not block finance work
            case.log("review_unavailable", error=str(e))

    async def delegate(self, skill: str, env: TaskEnvelope, meta: DelegationMeta, parent_task_id: str,
                       context_id: str | None = None) -> Delegation:
        """Blocking delegation: returns when the child completes, fails or parks (then the caller parks)."""
        target = SKILL_OWNER.get(skill)
        visited = [*meta.visited, self.name]
        reason = None
        if target is None:
            reason = f"unknown skill {skill}"
        elif target not in AGENTS[self.name].may_delegate_to:
            reason = f"{self.name} may not delegate to {target}"
        elif target in visited:
            reason = f"cycle: {' -> '.join([*visited, target])}"
        elif meta.delegation_depth + 1 > MAX_DELEGATION_DEPTH:
            reason = "max delegation depth"
        if reason:
            store.log_event(env.case_id, self.name, parent_task_id, "delegation_refused", skill=skill, reason=reason)
            raise DelegationRefused(reason)
        child_meta = DelegationMeta(case_id=env.case_id, from_agent=self.name, parent_task_id=parent_task_id,
                                    delegation_depth=meta.delegation_depth + 1, visited=visited,
                                    idempotency_key=f"{parent_task_id}:{skill}", priority=meta.priority,
                                    correlation_id=meta.correlation_id or env.case_id, context_id=context_id)
        store.log_event(env.case_id, self.name, parent_task_id, "delegated", skill=skill, to=target)
        task = await a2a_client.start_task(target, env.model_dump(), child_meta.model_dump(exclude_none=True),
                                           context_id=context_id, wait=True)
        d = Delegation(skill, target, a2a_client.state_name(task), task.id if task else None, a2a_client.artifacts(task))
        store.log_event(env.case_id, self.name, parent_task_id, "delegation_returned", skill=skill, to=target,
                        state=d.state, child_task_id=d.child_task_id)
        return d


def child_result(case: Case) -> dict:
    """The child's ResultArtifact carried by a resume message (or {} if resumed by something else)."""
    r = case.resume or {}
    return r.get("result", {}) if r.get("kind") == "child_result" else {}
