"""Durable W3 admission, checkpoints, dependencies and callback inbox.

The SDK owns protocol task snapshots. These records own execution, which the
SDK deliberately does not schedule after a process restart. All IDs are stable
across retries. A cached step is not a substitute for an external API's own
idempotency key: business reducers and outreach adapters must supply one.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

NAMESPACE = uuid.UUID("e6b9c053-7578-4e06-b5e5-5639c4b59e55")
metadata = MetaData()
tasks = Table(
    "freight_execution", metadata,
    Column("id", String(36), primary_key=True), Column("agent", String(80), nullable=False, index=True),
    Column("sender", String(80), nullable=False), Column("request_key", Text, nullable=False),
    Column("payload_hash", String(64), nullable=False), Column("envelope", Text, nullable=False),
    Column("delegation", Text, nullable=False), Column("message", Text, nullable=False),
    Column("status", String(30), nullable=False), Column("lease_owner", String(36), nullable=False, default=""),
    Column("lease_until", Float, nullable=False, default=0), Column("generation", Integer, nullable=False, default=0),
    Column("wait_kind", String(80), nullable=False, default=""),
    Column("wait_key", Text, nullable=False, default=""), Column("wait_details", Text, nullable=False, default="{}"),
    Column("updated_at", Float, nullable=False),
)
steps = Table(
    "freight_steps", metadata, Column("id", String(36), primary_key=True),
    Column("task_id", String(36), nullable=False, index=True), Column("key", Text, nullable=False),
    Column("value", Text, nullable=False), Column("updated_at", Float, nullable=False),
)
inbox = Table(
    "freight_inbox", metadata, Column("id", String(36), primary_key=True),
    Column("task_id", String(36), nullable=False, index=True), Column("wait_key", Text, nullable=False),
    Column("payload_hash", String(64), nullable=False), Column("value", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
consumed_inputs = Table(
    "freight_consumed_inputs", metadata, Column("id", String(36), primary_key=True),
    Column("task_id", String(36), nullable=False, index=True), Column("created_at", Float, nullable=False),
)
dependencies = Table(
    "freight_dependencies", metadata, Column("id", String(36), primary_key=True),
    Column("parent_id", String(36), nullable=False, index=True), Column("invocation", Text, nullable=False),
    Column("target", String(80), nullable=False), Column("child_id", String(36), nullable=False),
    Column("payload_hash", String(64), nullable=False), Column("envelope", Text, nullable=False),
    Column("delegation", Text, nullable=False), Column("state", String(30), nullable=False),
    Column("result", Text, nullable=False, default="[]"),
)

projections = Table(
    "freight_projections", metadata, Column("id", String(36), primary_key=True),
    Column("task_id", String(36), nullable=False, index=True), Column("agent", String(80), nullable=False),
    Column("payload", Text, nullable=False), Column("delivered", Integer, nullable=False, default=0),
    Column("created_at", Float, nullable=False),
    # The SpacetimeDB database (URL/name) a row is for; '' for rows queued before targets were recorded, which
    # any database may receive. Added in place to existing tables by RuntimeStore.initialize.
    Column("target", Text, nullable=False, default="", server_default=""),
)


class IdempotencyConflict(ValueError):
    """An operation key was reused for different semantic input."""


class LeaseLost(RuntimeError):
    """Execution was canceled or another worker owns its current generation."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, canonical(list(parts))))


def projection_target() -> str:
    """The SpacetimeDB database activity is written to, as SpacetimeClient resolves it from the environment."""
    url = os.getenv("SPACETIME_URL", "http://localhost:3000").rstrip("/")
    return f"{url}/{os.getenv('SPACETIME_DB', 'freightops')}"


def _add_missing_columns(sync_conn) -> None:
    """Additive, in-place schema upgrades for tables create_all already made (it never alters them).

    Adding `freight_projections.target` backfills every pending row in the same transaction: from the `_target`
    an earlier build kept inside the payload, else the database this service is configured for now, which is where
    the row would have been delivered without targets. Row IDs and write IDs are unchanged, so a write the reducer
    already received stays idempotent."""
    from sqlalchemy import inspect
    columns = {column["name"] for column in inspect(sync_conn).get_columns(projections.name)}
    if "target" in columns:
        return
    sync_conn.execute(text(f"ALTER TABLE {projections.name} ADD COLUMN target TEXT NOT NULL DEFAULT ''"))
    current = projection_target()
    rows = sync_conn.execute(select(projections.c.id, projections.c.payload).where(
        projections.c.delivered == 0)).all()
    for row_id, raw in rows:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("_target"), str) and payload["_target"]:
            target = payload.pop("_target")
            sync_conn.execute(update(projections).where(projections.c.id == row_id).values(
                target=target, payload=canonical(payload)))
        else:
            sync_conn.execute(update(projections).where(projections.c.id == row_id).values(target=current))


def projection_row_id(write_id: str, target: str) -> str:
    """One outbox row per (target, write_id): the same event may be owed to two databases."""
    return stable_id("outbox", target, write_id) if target else write_id


def task_id_for(agent: str, sender: str, case_id: str, key: str) -> str:
    return stable_id("task", agent, sender, case_id, key)


def runtime_url() -> str:
    explicit = os.getenv("A2A_DATABASE_URL")
    if explicit:
        return explicit
    if os.getenv("FREIGHTOPS_MODE", "fixture") == "live":
        url = os.getenv("POSTGRES_URL")
        if not url:
            raise ValueError("live A2A requires POSTGRES_URL or A2A_DATABASE_URL")
        return url
    path = Path(os.getenv("A2A_RUNTIME_DIR", "data/a2a"))
    path.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{(path / 'runtime.sqlite3').resolve()}"


def _has_pending_wait_input():
    return select(inbox.c.id).outerjoin(consumed_inputs, inbox.c.id == consumed_inputs.c.id).where(
        inbox.c.task_id == tasks.c.id, inbox.c.wait_key == tasks.c.wait_key,
        consumed_inputs.c.id.is_(None),
    ).correlate(tasks).exists()


class RuntimeStore:
    def __init__(self, url: str | None = None, *, engine: AsyncEngine | None = None):
        self.engine = engine or create_async_engine(url or runtime_url())
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._dependency_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if not self._initialized:
                async with self.schema_lock(), self.engine.begin() as conn:
                    await conn.run_sync(metadata.create_all)
                    await conn.run_sync(_add_missing_columns)
                self._initialized = True

    @asynccontextmanager
    async def schema_lock(self):
        """Serialize first boot across all nine processes, including SDK table creation."""
        if self.engine.dialect.name == "postgresql":
            async with self.engine.begin() as conn:
                await conn.execute(text("SELECT pg_advisory_xact_lock(726192831)"))
                yield
        elif self.engine.dialect.name == "sqlite" and self.engine.url.database != ":memory:":
            lock_path = Path(str(self.engine.url.database) + ".schema.lock")
            with lock_path.open("a+") as lock_file:  # noqa: ASYNC230 -- tiny local lock; lock acquisition is nonblocking
                while True:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        else:
            yield

    async def close(self) -> None:
        await self.engine.dispose()

    async def get(self, task_id: str) -> dict | None:
        await self.initialize()
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
        if row is None:
            return None
        result = dict(row)
        for key in ("envelope", "delegation", "message", "wait_details"):
            result[key] = json.loads(result[key])
        return result

    async def admit(self, agent: str, sender: str, key: str, envelope: dict,
                    delegation: dict, message: dict) -> tuple[dict, bool]:
        await self.initialize()
        task_id = task_id_for(agent, sender, envelope["case_id"], key)
        fingerprint = digest({"envelope": envelope, "delegation": delegation})
        try:
            async with self.engine.begin() as conn:
                await conn.execute(insert(tasks).values(
                    id=task_id, agent=agent, sender=sender, request_key=key, payload_hash=fingerprint,
                    envelope=canonical(envelope), delegation=canonical(delegation), message=canonical(message),
                    status="queued", lease_owner="", lease_until=0, generation=0,
                    wait_kind="", wait_key="", wait_details="{}", updated_at=time.time(),
                ))
            fresh = True
        except IntegrityError:
            fresh = False
        row = await self.get(task_id)
        if row is None or row["payload_hash"] != fingerprint:
            raise IdempotencyConflict("idempotency key already belongs to a different request")
        return row, fresh

    async def update(self, task_id: str, **values) -> None:
        await self.initialize()
        for key in ("envelope", "delegation", "message", "wait_details"):
            if key in values:
                values[key] = canonical(values[key])
        async with self.engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values, updated_at=time.time()))

    async def claim(self, task_id: str, owner: str, *, seconds: float = 30) -> int | None:
        await self.initialize()
        now = time.time()
        async with self.engine.begin() as conn:
            result = await conn.execute(update(tasks).where(
                tasks.c.id == task_id, tasks.c.status.not_in(("completed", "failed", "canceled", "rejected")),
                tasks.c.lease_until <= now,
                # A delayed SDK request may outlive the callback that scheduled it.
                or_(tasks.c.status != "waiting", _has_pending_wait_input()),
            ).values(status="working", lease_owner=owner, lease_until=now + seconds,
                     generation=tasks.c.generation + 1, updated_at=now))
            if not result.rowcount:
                return None
            return (await conn.execute(select(tasks.c.generation).where(tasks.c.id == task_id))).scalar_one()

    async def queue_recoverable(self, row: dict) -> bool:
        """Fence stale callback/recovery observations across workers in the database."""
        await self.initialize()
        now = time.time()
        async with self.engine.begin() as conn:
            result = await conn.execute(update(tasks).where(
                tasks.c.id == row["id"], tasks.c.generation == row["generation"],
                tasks.c.status == row["status"], tasks.c.status.in_(("queued", "working", "waiting")),
                tasks.c.wait_key == row["wait_key"],
                tasks.c.lease_until <= now,
                or_(tasks.c.status != "waiting", _has_pending_wait_input()),
            ).values(status="queued", updated_at=now))
            return bool(result.rowcount)

    async def repair_terminal(self, row: dict, status: str) -> bool:
        """Apply a persisted terminal snapshot only to the observed inactive execution."""
        if status not in {"completed", "failed", "canceled", "rejected"}:
            raise ValueError("terminal snapshot state required")
        now = time.time()
        async with self.engine.begin() as conn:
            result = await conn.execute(update(tasks).where(
                tasks.c.id == row["id"], tasks.c.generation == row["generation"],
                tasks.c.status == row["status"], tasks.c.status.in_(("queued", "working", "waiting")),
                tasks.c.lease_until <= now,
            ).values(status=status, updated_at=now))
            return bool(result.rowcount)

    async def renew(self, task_id: str, owner: str, generation: int) -> None:
        async with self.engine.begin() as conn:
            result = await conn.execute(update(tasks).where(
                tasks.c.id == task_id, tasks.c.lease_owner == owner, tasks.c.generation == generation,
                tasks.c.status == "working", tasks.c.lease_until > time.time(),
            ).values(lease_until=time.time() + 30, updated_at=time.time()))
            if not result.rowcount:
                raise LeaseLost("execution lease is no longer owned")

    async def assert_lease(self, task_id: str, owner: str, generation: int) -> None:
        row = await self.get(task_id)
        if (row is None or row["status"] != "working" or row["lease_owner"] != owner
                or row["generation"] != generation or row["lease_until"] <= time.time()):
            raise LeaseLost("execution lease is no longer owned")

    async def release(self, task_id: str, owner: str, generation: int, *, status: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(update(tasks).where(
                tasks.c.id == task_id, tasks.c.lease_owner == owner, tasks.c.generation == generation,
            ).values(status=status, lease_until=0, lease_owner="", updated_at=time.time()))

    async def get_step(self, task_id: str, key: str) -> Any:
        await self.initialize()
        async with self.engine.connect() as conn:
            value = (await conn.execute(select(steps.c.value).where(
                steps.c.id == stable_id("step", task_id, key)))).scalar_one_or_none()
        return json.loads(value) if value is not None else None

    async def set_step(self, task_id: str, key: str, value: Any) -> None:
        await self.initialize()
        sid = stable_id("step", task_id, key)
        encoded = canonical(value)
        try:
            async with self.engine.begin() as conn:
                await conn.execute(insert(steps).values(id=sid, task_id=task_id, key=key,
                                                       value=encoded, updated_at=time.time()))
        except IntegrityError:
            old = await self.get_step(task_id, key)
            if canonical(old) != encoded:
                raise IdempotencyConflict("completed step cannot be overwritten") from None

    async def put_input(self, task_id: str, wait_key: str, key: str, value: dict) -> bool:
        await self.initialize()
        iid = stable_id("input", task_id, key)
        fingerprint = digest({"wait_key": wait_key, "value": value})
        try:
            async with self.engine.begin() as conn:
                await conn.execute(insert(inbox).values(id=iid, task_id=task_id, wait_key=wait_key,
                    payload_hash=fingerprint, value=canonical(value), created_at=time.time()))
            return True
        except IntegrityError:
            async with self.engine.connect() as conn:
                old = (await conn.execute(select(inbox.c.payload_hash).where(inbox.c.id == iid))).scalar_one()
            if old != fingerprint:
                raise IdempotencyConflict("callback key already belongs to different input") from None
            return False

    async def get_input(self, task_id: str, wait_key: str) -> dict | None:
        await self.initialize()
        async with self.engine.connect() as conn:
            value = (await conn.execute(select(inbox.c.value).where(
                inbox.c.task_id == task_id, inbox.c.wait_key == wait_key,
            ).order_by(inbox.c.created_at.desc()).limit(1))).scalar_one_or_none()
        return json.loads(value) if value is not None else None

    async def pending_inputs(self, task_id: str, wait_key: str | None = None) -> list[str]:
        await self.initialize()
        query = select(inbox.c.id).outerjoin(consumed_inputs, inbox.c.id == consumed_inputs.c.id).where(
            inbox.c.task_id == task_id, consumed_inputs.c.id.is_(None))
        if wait_key is not None:
            query = query.where(inbox.c.wait_key == wait_key)
        async with self.engine.connect() as conn:
            return list((await conn.execute(query)).scalars())

    async def consume_inputs(self, task_id: str, input_ids: list[str]) -> None:
        for input_id in input_ids:
            try:
                async with self.engine.begin() as conn:
                    await conn.execute(insert(consumed_inputs).values(
                        id=input_id, task_id=task_id, created_at=time.time()))
            except IntegrityError:
                pass  # Replayed completion acknowledges the same immutable delivery.

    async def add_dependency(self, parent_id: str, invocation: str, target: str, child_id: str,
                             envelope: dict, delegation: dict, *, max_fanout: int = 3) -> dict:
        await self.initialize()
        did = stable_id("dependency", parent_id, invocation)
        fingerprint = digest({"target": target, "envelope": envelope, "delegation": delegation})
        # A parent-row write serializes cross-worker reservations; the local lock also
        # serializes concurrent fanout coroutines before SQLite upgrades its transaction.
        async with self._dependency_lock, self.engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == parent_id).values(updated_at=tasks.c.updated_at))
            rows = (await conn.execute(select(dependencies).where(
                dependencies.c.parent_id == parent_id))).mappings().all()
            found = next((dict(row) for row in rows if row["id"] == did), None)
            if found:
                if found["payload_hash"] != fingerprint:
                    raise IdempotencyConflict("delegation invocation was reused for different work")
                return found
            if len(rows) >= max_fanout:
                raise ValueError("max delegation fanout")
            row = {"id": did, "parent_id": parent_id, "invocation": invocation, "target": target, "child_id": child_id,
                "payload_hash": fingerprint, "envelope": canonical(envelope), "delegation": canonical(delegation),
                "state": "pending", "result": "[]"}
            await conn.execute(insert(dependencies).values(**row))
        return row

    async def get_dependencies(self, parent_id: str) -> list[dict]:
        await self.initialize()
        async with self.engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(select(dependencies).where(
                dependencies.c.parent_id == parent_id))).mappings()]

    async def finish_dependency(self, dependency_id: str, state: str, result: list[dict]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(update(dependencies).where(dependencies.c.id == dependency_id).values(
                state=state, result=canonical(result)))

    async def recoverable(self, agent: str) -> list[dict]:
        await self.initialize()
        async with self.engine.connect() as conn:
            ids = (await conn.execute(select(tasks.c.id).where(
                tasks.c.agent == agent,
                tasks.c.status.in_(("queued", "working", "waiting")),
                tasks.c.lease_until <= time.time(),
            ).order_by(tasks.c.updated_at).limit(100))).scalars().all()
        return [row for task_id in ids if (row := await self.get(task_id)) is not None]

    async def waiting_on(self, agent: str, wait_key: str) -> list[dict]:
        """Open executions of one agent parked on a business wait key, e.g. documents:<case>."""
        await self.initialize()
        async with self.engine.connect() as conn:
            ids = (await conn.execute(select(tasks.c.id).where(
                tasks.c.agent == agent, tasks.c.wait_key == wait_key,
                tasks.c.status.in_(("queued", "working", "waiting")),
            ).order_by(tasks.c.updated_at))).scalars().all()
        return [row for task_id in ids if (row := await self.get(task_id)) is not None]

    async def queue_projection(self, agent: str, payload: dict) -> None:
        """Queue one activity row. A payload's `_target` names the database it is for (stored as a column, not
        content); the row's identity is (target, write_id), so each database keeps its own ordered copy."""
        await self.initialize()
        payload = dict(payload)
        target = payload.pop("_target", "") or ""
        row_id = projection_row_id(payload["write_id"], target)
        try:
            async with self.engine.begin() as conn:
                await conn.execute(insert(projections).values(id=row_id, task_id=payload["task_id"],
                    agent=agent, payload=canonical(payload), delivered=0, created_at=time.time(), target=target))
        except IntegrityError:
            async with self.engine.connect() as conn:
                previous = (await conn.execute(select(projections.c.payload).where(
                    projections.c.id == row_id))).scalar_one()
            if previous != canonical(payload):
                raise IdempotencyConflict("projection key cannot be reused for a different event") from None

    async def projection_exists(self, write_id: str, target: str = "") -> bool:
        await self.initialize()
        async with self.engine.connect() as conn:
            return (await conn.execute(select(projections.c.id).where(
                projections.c.id == projection_row_id(write_id, target)))).scalar_one_or_none() is not None

    async def pending_projections(self, agent: str, task_id: str | None = None,
                                  target: str | None = None) -> list[dict]:
        """Undelivered rows in queue order; with `target`, only rows for it (and untargeted legacy rows)."""
        await self.initialize()
        query = select(projections).where(projections.c.agent == agent, projections.c.delivered == 0)
        if task_id:
            query = query.where(projections.c.task_id == task_id)
        if target is not None:
            query = query.where(projections.c.target.in_((target, "")))
        async with self.engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(query.order_by(projections.c.created_at))).mappings()]

    async def held_projections(self, agent: str, target: str) -> int:
        """Undelivered rows owed to some other database, which this target never delivers."""
        await self.initialize()
        from sqlalchemy import func
        async with self.engine.connect() as conn:
            return (await conn.execute(select(func.count()).select_from(projections).where(
                projections.c.agent == agent, projections.c.delivered == 0,
                projections.c.target.not_in((target, ""))))).scalar_one()

    async def acknowledge_projection(self, projection_id: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(update(projections).where(projections.c.id == projection_id).values(delivered=1))

    async def reject_projection(self, projection_id: str) -> None:
        """A reducer that threw is deterministic: retrying the same write never helps, so the row leaves the
        pending set for good (delivered=2) instead of being replayed on every pass."""
        async with self.engine.begin() as conn:
            await conn.execute(update(projections).where(projections.c.id == projection_id).values(delivered=2))

    async def cancel(self, task_id: str) -> None:
        await self.initialize()
        async with self.engine.begin() as conn:
            await conn.execute(update(tasks).where(and_(tasks.c.id == task_id,
                tasks.c.status.not_in(("completed", "failed", "canceled", "rejected")))).values(
                    status="canceled", lease_owner="", lease_until=0,
                    generation=tasks.c.generation + 1, updated_at=time.time()))
