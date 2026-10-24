"""First-hop A2A dispatch (PRD H3, H4, 7.1 step 3-4). Returns on acceptance (task created), not completion."""
import json
import logging
import sqlite3
import threading
import time

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import new_data_message
from a2a.types import Role, SendMessageRequest
from google.protobuf.json_format import MessageToDict

from common.config import AGENTS, SKILL_OWNER
from common.envelope import DelegationMeta, TaskEnvelope
from harness.ingest.vault import DATA_DIR

log = logging.getLogger("harness.dispatch")
CARD_TTL_S = 60


class DispatchError(Exception):
    pass


class Dispatcher:
    def __init__(self, path=None) -> None:
        path = path or DATA_DIR / "ingest.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS dispatch(event_id TEXT PRIMARY KEY, case_id TEXT, skill TEXT, agent TEXT,"
            " task_id TEXT, status TEXT, attempts INTEGER, reason TEXT, envelope TEXT, updated_at REAL)")
        self._db.commit()
        self._cards: dict[str, tuple[float, set[str]]] = {}

    def get(self, event_id: str) -> dict | None:
        row = self._db.execute("SELECT event_id, case_id, skill, agent, task_id, status, attempts, reason"
                               " FROM dispatch WHERE event_id=?", (event_id,)).fetchone()
        keys = ("event_id", "case_id", "skill", "agent", "task_id", "status", "attempts", "reason")
        return dict(zip(keys, row, strict=True)) if row else None

    def record(self, event_id: str, case_id: str, skill: str | None, agent: str | None, task_id: str | None,
               status: str, reason: str, env: TaskEnvelope | None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO dispatch VALUES(?,?,?,?,?,?,1,?,?,?) ON CONFLICT(event_id) DO UPDATE SET"
                " skill=excluded.skill, agent=excluded.agent, task_id=excluded.task_id, status=excluded.status,"
                " attempts=dispatch.attempts+1, reason=excluded.reason, envelope=excluded.envelope,"
                " updated_at=excluded.updated_at",
                (event_id, case_id, skill, agent, task_id, status, reason,
                 env.model_dump_json() if env else None, time.time()))
            self._db.commit()

    async def _advertised(self, agent: str) -> set[str]:
        hit = self._cards.get(agent)
        if hit and time.time() - hit[0] < CARD_TTL_S:
            return hit[1]
        url = f"{AGENTS[agent].url}/.well-known/agent-card.json"
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(url)
            r.raise_for_status()
        skills = {s["id"] for s in r.json().get("skills", [])}
        self._cards[agent] = (time.time(), skills)
        return skills

    async def send(self, event_id: str, skill: str, env: TaskEnvelope) -> dict:
        """Idempotent on event_id. Raises DispatchError (caller leaves the Redis entry pending to retry)."""
        prior = self.get(event_id)
        if prior and prior["status"] == "accepted":
            return {**prior, "duplicate": True}
        agent = SKILL_OWNER.get(skill)
        if agent is None:
            raise DispatchError(f"no owner for skill {skill}")
        try:
            advertised = await self._advertised(agent)
        except httpx.HTTPError as e:
            self.record(event_id, env.case_id, skill, agent, None, "retry", f"card: {e}", env)
            raise DispatchError(f"{agent} card unreachable: {e}") from e
        if skill not in advertised:
            self.record(event_id, env.case_id, skill, agent, None, "retry", "skill not advertised", env)
            raise DispatchError(f"{agent} card does not advertise {skill}")

        msg = new_data_message(env.model_dump(), context_id=env.case_id, role=Role.ROLE_USER)
        meta = DelegationMeta(case_id=env.case_id, from_agent="router", delegation_depth=0, visited=[],
                              idempotency_key=event_id, correlation_id=event_id, causation_id=event_id,
                              priority=env.priority)
        msg.metadata.update(meta.model_dump(exclude_none=True))
        task_id = None
        try:
            # polling=True sets return_immediately: the agent answers once the task exists.
            client = await create_client(AGENTS[agent].url, ClientConfig(streaming=False, polling=True))
            try:
                async for ev in client.send_message(SendMessageRequest(message=msg)):
                    d = MessageToDict(ev) if hasattr(ev, "DESCRIPTOR") else {}
                    task_id = (d.get("task") or {}).get("id") or (d.get("statusUpdate") or {}).get("taskId")
                    if task_id:
                        break
            finally:
                await client.close()
        except Exception as e:
            self.record(event_id, env.case_id, skill, agent, None, "retry", f"send: {e}", env)
            raise DispatchError(f"send to {agent} failed: {e}") from e
        if not task_id:
            self.record(event_id, env.case_id, skill, agent, None, "retry", "no task in response", env)
            raise DispatchError(f"{agent} returned no task")
        self.record(event_id, env.case_id, skill, agent, task_id, "accepted", "ok", env)
        log.info(json.dumps({"event_id": event_id, "skill": skill, "agent": agent, "task_id": task_id}))
        return {"event_id": event_id, "skill": skill, "agent": agent, "task_id": task_id, "status": "accepted",
                "duplicate": False}
