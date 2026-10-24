"""First-hop A2A dispatch (PRD H3, H4, 7.1 step 3-4). Returns on acceptance (task created), not completion.
Transport, card/skill checks and the router's A2A credential come from W3's common.a2a_transport."""
import json
import logging
import sqlite3
import threading
import time

from common.a2a_transport import send_task
from common.config import SKILL_OWNER
from common.envelope import TaskEnvelope
from harness.ingest.firsthop import first_hop_meta
from harness.ingest.vault import DATA_DIR

log = logging.getLogger("harness.dispatch")


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

    async def send(self, event_id: str, skill: str, env: TaskEnvelope) -> dict:
        """Idempotent on event_id. Raises DispatchError (caller leaves the Redis entry pending to retry)."""
        prior = self.get(event_id)
        if prior and prior["status"] == "accepted":
            return {**prior, "duplicate": True}
        agent = SKILL_OWNER.get(skill)
        if agent is None:
            raise DispatchError(f"no owner for skill {skill}")
        meta = first_hop_meta(event_id, env)
        try:
            task = await send_task(agent, env, meta)
        except Exception as e:  # card/skill mismatch, missing credential, transport: all retried via reclaim
            self.record(event_id, env.case_id, skill, agent, None, "retry", f"send: {type(e).__name__}: {e}", env)
            raise DispatchError(f"send to {agent} failed: {type(e).__name__}: {e}") from e
        if not task.id or task.context_id != env.case_id:
            self.record(event_id, env.case_id, skill, agent, None, "retry", "acceptance did not preserve the case",
                        env)
            raise DispatchError(f"{agent} acceptance did not preserve case {env.case_id}")
        self.record(event_id, env.case_id, skill, agent, task.id, "accepted", "ok", env)
        log.info(json.dumps({"event_id": event_id, "skill": skill, "agent": agent, "task_id": task.id}))
        return {"event_id": event_id, "skill": skill, "agent": agent, "task_id": task.id, "status": "accepted",
                "duplicate": False}
