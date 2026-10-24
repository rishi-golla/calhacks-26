"""FreightAgent: shared A2A executor + delegation with guards (PRD W3 A2/A3)."""
import logging
import uuid

from a2a.client import ClientConfig, create_client
from a2a.helpers import get_data_parts, new_data_message, new_data_part, new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Role, SendMessageRequest, TaskState
from google.protobuf.json_format import MessageToDict

from common.config import AGENTS, MAX_DELEGATION_DEPTH, SKILL_OWNER
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope

log = logging.getLogger(__name__)


class DelegationRefused(Exception):
    pass


def _meta_dict(msg) -> dict:
    md = getattr(msg, "metadata", None)
    if md is None:
        return {}
    return MessageToDict(md) if hasattr(md, "DESCRIPTOR") else dict(md)


class FreightAgent(AgentExecutor):
    name: str = ""

    async def handle(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str,
                     updater: TaskUpdater) -> ResultArtifact:
        return ResultArtifact(outcome="not_implemented", findings={"agent": self.name})

    async def execute(self, ctx: RequestContext, q: EventQueue) -> None:
        task = ctx.current_task or new_task_from_user_message(ctx.message)
        if not ctx.current_task:
            await q.enqueue_event(task)
        u = TaskUpdater(q, task.id, task.context_id)
        data = get_data_parts(ctx.message.parts)[0]
        env = TaskEnvelope.model_validate(data)
        raw = _meta_dict(ctx.message)
        meta = DelegationMeta(case_id=raw.get("case_id", env.case_id), from_agent=raw.get("from_agent", "router"),
                              parent_task_id=raw.get("parent_task_id"),
                              delegation_depth=int(raw.get("delegation_depth", 0)),
                              visited=list(raw.get("visited", [])), priority=raw.get("priority", env.priority))
        await u.update_status(TaskState.TASK_STATE_WORKING, metadata={"business_status": "investigating",
                                                                      "agent": self.name})
        try:
            result = await self.handle(env, meta, task.id, u)
        except Exception as e:
            log.exception("%s failed", self.name)
            await u.add_artifact(parts=[new_data_part({"outcome": "failed", "error": str(e)})], name="error")
            await u.failed()
            return
        await u.add_artifact(parts=[new_data_part(result.model_dump())], name=f"{self.name}_result")
        await u.complete()

    async def cancel(self, ctx: RequestContext, q: EventQueue) -> None:
        u = TaskUpdater(q, ctx.task_id, ctx.context_id)
        await u.cancel()

    async def delegate(self, skill: str, env: TaskEnvelope, meta: DelegationMeta, parent_task_id: str,
                       context_id: str | None = None) -> list[dict]:
        """Await-mode delegation. Returns the data parts of all artifacts from the child task."""
        target = SKILL_OWNER[skill]
        me = AGENTS[self.name]
        if target not in me.may_delegate_to:
            raise DelegationRefused(f"{self.name} may not delegate to {target}")
        visited = [*meta.visited, self.name]
        if target in visited:
            raise DelegationRefused(f"cycle: {' -> '.join(visited + [target])}")
        if meta.delegation_depth + 1 > MAX_DELEGATION_DEPTH:
            raise DelegationRefused("max delegation depth")
        msg = new_data_message(env.model_dump(), context_id=context_id, role=Role.ROLE_USER)
        child_meta = DelegationMeta(case_id=env.case_id, from_agent=self.name, parent_task_id=parent_task_id,
                                    delegation_depth=meta.delegation_depth + 1, visited=visited,
                                    idempotency_key=f"{parent_task_id}:{skill}", priority=meta.priority,
                                    correlation_id=meta.correlation_id or str(uuid.uuid4()))
        msg.metadata.update(child_meta.model_dump(exclude_none=True))
        client = await create_client(AGENTS[target].url, ClientConfig(streaming=True))
        artifacts: list[dict] = []
        try:
            async for ev in client.send_message(SendMessageRequest(message=msg)):
                artifacts.extend(extract_artifact_data(ev))
        finally:
            await client.close()
        return artifacts


def extract_artifact_data(ev) -> list[dict]:
    """Pull data parts out of any streamed event (task snapshot or artifact update)."""
    out: list[dict] = []
    d = MessageToDict(ev) if hasattr(ev, "DESCRIPTOR") else {}
    arts = []
    if "artifactUpdate" in d:
        arts.append(d["artifactUpdate"].get("artifact", {}))
    if "task" in d:
        arts.extend(d["task"].get("artifacts", []))
    for a in arts:
        for p in a.get("parts", []):
            if "data" in p:
                out.append(p["data"])
    return out
