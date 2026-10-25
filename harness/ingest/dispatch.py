"""First-hop dispatch (PRD H3, H4, 7.1 step 3-4). Returns on durable acceptance, not completion.

FIRST_HOP_TRANSPORT selects how a routed envelope leaves the router:
- `a2a` (default): push the task to the owning agent over W3's common.a2a_transport; accepted = task created.
- `spacetime`: commit it to the SpacetimeDB outbox with `enqueue_first_hop` (router role); queued = the outbox
  row and its `dispatch` row committed together. The dispatch worker delivers it to the agent over A2A."""
import json
import logging
import os
import sqlite3
import threading
import time

from common.a2a_transport import send_task
from common.config import SKILL_OWNER
from common.envelope import TaskEnvelope
from common.first_hop import first_hop_meta
from common.spacetime_client import SpacetimeClient, SpacetimeError
from harness.ingest.telemetry import POLICY_VERSION, QUEUE_MS
from harness.ingest.vault import DATA_DIR

log = logging.getLogger("harness.dispatch")
TRANSPORTS = ("a2a", "spacetime")
DELIVERED = ("queued", "accepted")   # a re-delivered event in either state is a duplicate
REDUCER_REJECTED = 530               # SpacetimeDB's status for a reducer that threw (deterministic refusal)


class DispatchError(Exception):
    pass


class Dispatcher:
    def __init__(self, path=None, transport: str | None = None) -> None:
        self.transport = (transport or os.getenv("FIRST_HOP_TRANSPORT", "a2a")).strip().lower()
        if self.transport not in TRANSPORTS:
            raise ValueError(f"FIRST_HOP_TRANSPORT must be one of {', '.join(TRANSPORTS)}")
        path = path or DATA_DIR / "ingest.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        # `enqueue` is the exact enqueue_first_hop payload, stored before the first attempt and resent verbatim:
        # the reducer rejects a reused write id whose contents changed, and a reclaim re-runs the models.
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS dispatch(event_id TEXT PRIMARY KEY, case_id TEXT, skill TEXT, agent TEXT,"
            " task_id TEXT, status TEXT, attempts INTEGER, reason TEXT, envelope TEXT, updated_at REAL, enqueue TEXT)")
        if "enqueue" not in {row[1] for row in self._db.execute("PRAGMA table_info(dispatch)")}:
            self._db.execute("ALTER TABLE dispatch ADD COLUMN enqueue TEXT")
        self._db.commit()

    def get(self, event_id: str) -> dict | None:
        row = self._db.execute("SELECT event_id, case_id, skill, agent, task_id, status, attempts, reason"
                               " FROM dispatch WHERE event_id=?", (event_id,)).fetchone()
        keys = ("event_id", "case_id", "skill", "agent", "task_id", "status", "attempts", "reason")
        return dict(zip(keys, row, strict=True)) if row else None

    def record(self, event_id: str, case_id: str, skill: str | None, agent: str | None, task_id: str | None,
               status: str, reason: str, env: TaskEnvelope | None) -> None:
        # A late failure (a second Redis entry for the same event) never downgrades a delivered row.
        with self._lock:
            self._db.execute(
                "INSERT INTO dispatch(event_id, case_id, skill, agent, task_id, status, attempts, reason, envelope,"
                " updated_at) VALUES(?,?,?,?,?,?,1,?,?,?) ON CONFLICT(event_id) DO UPDATE SET"
                " skill=excluded.skill, agent=excluded.agent, task_id=excluded.task_id, status=excluded.status,"
                " attempts=dispatch.attempts+1, reason=excluded.reason, envelope=excluded.envelope,"
                " updated_at=excluded.updated_at"
                " WHERE NOT (dispatch.status IN ('queued', 'accepted') AND excluded.status = 'retry')",
                (event_id, case_id, skill, agent, task_id, status, reason,
                 env.model_dump_json() if env else None, time.time()))
            self._db.commit()

    def pinned(self, event_id: str) -> dict | None:
        """The enqueue_first_hop payload already attempted for this event, which every retry must resend."""
        row = self._db.execute("SELECT enqueue FROM dispatch WHERE event_id=?", (event_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def _pin(self, event_id: str, payload: dict) -> dict:
        """The payload pinned for this event, pinning `payload` first if there is none yet."""
        with self._lock:
            self._db.execute(
                "INSERT INTO dispatch(event_id, case_id, skill, agent, status, attempts, reason, updated_at,"
                " enqueue) VALUES(?,?,?,?,'retry',0,'enqueue in flight',?,?)"
                " ON CONFLICT(event_id) DO UPDATE SET enqueue=COALESCE(dispatch.enqueue, excluded.enqueue)",
                (event_id, payload["case_id"], payload["skill"], payload["agent"], time.time(), json.dumps(payload)))
            self._db.commit()
        return self.pinned(event_id)

    async def send(self, event_id: str, skill: str, env: TaskEnvelope, reason: str = "") -> dict:
        """Idempotent on event_id. Raises DispatchError (caller leaves the Redis entry pending to retry)."""
        prior = self.get(event_id)
        if prior and prior["status"] in DELIVERED:
            return {**prior, "duplicate": True}
        agent = SKILL_OWNER.get(skill)
        if agent is None:
            raise DispatchError(f"no owner for skill {skill}")
        if self.transport == "spacetime":
            return await self._enqueue(event_id, skill, agent, env, reason)
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

    async def _enqueue(self, event_id: str, skill: str, agent: str, env: TaskEnvelope, reason: str) -> dict:
        try:
            db = SpacetimeClient(actor="router")
        except SpacetimeError as e:
            self.record(event_id, env.case_id, skill, agent, None, "retry", f"enqueue: {type(e).__name__}: {e}", env)
            raise DispatchError(f"first hop for {event_id}: {e}") from e
        if db.disabled:
            why = "first hop needs SpacetimeDB (SPACETIME_DISABLED=0) or FIRST_HOP_TRANSPORT=a2a"
            self.record(event_id, env.case_id, skill, agent, None, "retry", why, env)
            raise DispatchError(why)
        # A retry sends the first attempt's payload, even if this attempt computed a different envelope or route.
        payload = self._pin(event_id, {
            "write_id": f"first_hop:{event_id}", "event_id": event_id, "case_id": env.case_id, "agent": agent,
            "skill": skill, "envelope": env.model_dump(mode="json"), "queue_ms": QUEUE_MS.get(),
            "policy_version": POLICY_VERSION, "reason": (reason or "routed")[:500]})
        case_id, skill, agent = payload["case_id"], payload["skill"], payload["agent"]
        env = TaskEnvelope.model_validate(payload["envelope"])
        try:
            await db.mutate("enqueue_first_hop", payload)
        except SpacetimeError as e:
            # 530: the reducer refused (missing or human_owned case, route/envelope mismatch, changed contents).
            # Transport errors, 429 and other 5xx may succeed later. Either way the entry stays pending and the
            # reclaim resends this payload under the same write id.
            kind = "rejected" if getattr(e, "status_code", None) == REDUCER_REJECTED else "unavailable"
            self.record(event_id, case_id, skill, agent, None, "retry", f"enqueue {kind}: {type(e).__name__}: {e}",
                        env)
            raise DispatchError(f"first hop for {event_id} {kind}: {type(e).__name__}: {e}") from e
        self.record(event_id, case_id, skill, agent, None, "queued", "ok", env)
        log.info(json.dumps({"event_id": event_id, "skill": skill, "agent": agent, "status": "queued"}))
        return {"event_id": event_id, "skill": skill, "agent": agent, "status": "queued", "duplicate": False}
