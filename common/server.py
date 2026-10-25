"""Durable A2A 1.0 admission and recovery around the SDK's persisted task stream."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from a2a.helpers import get_data_parts
from a2a.server.agent_execution import RequestContext, RequestContextBuilder
from a2a.server.cluster.database_event_stream import DatabaseTaskEventStream
from a2a.server.cluster.database_task_store import VersionedDatabaseTaskStore
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.types import Message, SendMessageConfiguration, SendMessageRequest, TaskState
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import ValidationError
from sqlalchemy import select, text
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from common.a2a_util import build_card
from common.config import AGENTS, MAX_DELEGATION_DEPTH
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import TERMINAL_STATES, FreightAgent, _meta_dict, extract_artifact_data
from common.runtime_store import IdempotencyConflict, LeaseLost, digest, stable_id, task_id_for, tasks

log = logging.getLogger(__name__)
SERVICE_ACTORS = {*AGENTS, "router", "gateway"}


class DurableContextBuilder(RequestContextBuilder):
    async def build(self, context, params=None, task_id=None, context_id=None, task=None):
        return RequestContext(call_context=context, request=params,
            task_id=context.state.get("admitted_id", task_id), context_id=context_id, task=task)


class AuthContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request):
        context = super().build(request)
        context.state["actor"] = getattr(request.state, "a2a_actor", None)
        return context


class DurableRequestHandler(DefaultRequestHandler):
    """Admit before execute. Retries return the canonical task; resumes enter an inbox."""
    def __init__(self, executor: FreightAgent, card, snapshots, event_stream):
        super().__init__(agent_executor=executor, task_store=snapshots, agent_card=card,
                         request_context_builder=DurableContextBuilder(), event_stream=event_stream)
        self.executor = executor
        self.store = executor.store
        self.name = executor.name
        self.snapshots = snapshots
        self._admission_lock = asyncio.Lock()
        self._recovery_wakeup = asyncio.Event()
        executor.wake_recovery = self._recovery_wakeup.set
        executor.confirm_state = self._confirm_state

    async def _confirm_state(self, task_id, state):
        # enqueue_event acknowledges queueing, not the SDK transaction. Keep the
        # execution recoverable until the corresponding snapshot is persisted.
        try:
            async with asyncio.timeout(5):
                while True:
                    stored = await self.snapshots.get(task_id, ServerCallContext())
                    if stored and stored.task.status.state == state:
                        return
                    if stored and stored.task.status.state in TERMINAL_STATES:
                        raise LeaseLost("task reached another terminal state")
                    await asyncio.sleep(0.01)
        except TimeoutError:
            raise LeaseLost("task snapshot persistence remains unconfirmed") from None

    def _actor(self, context: ServerCallContext, claimed: str | None = None) -> str:
        actor = context.state.get("actor")
        if context.state.get("internal"):
            return self.name
        if actor is None and os.getenv("FREIGHTOPS_MODE", "fixture") == "fixture":
            actor = claimed or "router"
        if actor not in SERVICE_ACTORS or (claimed and claimed != actor):
            raise InvalidParamsError(message="caller identity does not match authenticated service")
        return actor

    async def _authorize_task(self, task_id, context, *, resume=False):
        row = await self.store.get(task_id)
        if not row or row["agent"] != self.name:
            raise TaskNotFoundError
        actor = self._actor(context)
        permitted = {self.name, row["sender"], "router"}
        if self.name == "voice_desk":
            permitted.add("gateway")
        if actor not in permitted:
            raise TaskNotFoundError
        return row

    def _validate_initial(self, params, actor, env, meta):
        if (params.message.context_id and params.message.context_id != env.case_id) or meta.case_id != env.case_id:
            raise InvalidParamsError(message="case and context IDs must match")
        if not env.case_id or len(env.case_id) > 36 or not 1 <= env.deadline_s <= 600:
            raise InvalidParamsError(message="case ID must be 1..36 characters; deadline must be 1..600 seconds")
        if env.task_type not in AGENTS[self.name].skills:
            raise InvalidParamsError(message="skill is not owned by this service")
        if actor == "router":
            if meta.delegation_depth or meta.visited or meta.parent_task_id:
                raise InvalidParamsError(message="root admission cannot claim delegation ancestry")
            return
        if actor not in AGENTS or self.name not in AGENTS[actor].may_delegate_to:
            raise InvalidParamsError(message="delegation is not allowed")
        if (not meta.parent_task_id or not 1 <= meta.delegation_depth <= MAX_DELEGATION_DEPTH
                or meta.delegation_depth != len(meta.visited) or meta.visited[-1] != actor
                or len(set(meta.visited)) != len(meta.visited) or self.name in meta.visited):
            raise InvalidParamsError(message="invalid delegation depth or ancestry")

    async def _validate_parent(self, env, meta):
        if meta.from_agent == "router" or os.getenv("FREIGHTOPS_MODE", "fixture") != "live":
            return
        parent = await self.store.get(meta.parent_task_id)
        if (parent is None or parent["agent"] != meta.from_agent
                or parent["envelope"]["case_id"] != env.case_id
                or parent["status"] in {"canceled", "failed", "rejected"}):
            raise ValueError("delegation parent is absent, inactive or belongs to another case/service")
        ancestry = parent["delegation"]
        if (meta.visited != [*ancestry.get("visited", []), meta.from_agent]
                or meta.delegation_depth != ancestry.get("delegation_depth", 0) + 1
                or meta.causation_id != meta.parent_task_id
                or meta.correlation_id != (ancestry.get("correlation_id") or env.case_id)):
            raise ValueError("delegation ancestry does not match its durable parent")
        child_id = task_id_for(self.name, meta.from_agent, env.case_id, meta.idempotency_key)
        reservation = next((row for row in await self.store.get_dependencies(meta.parent_task_id)
                            if row["child_id"] == child_id and row["target"] == self.name), None)
        if (reservation is None or digest(env.model_dump()) != digest(json.loads(reservation["envelope"]))
                or digest(meta.model_dump(exclude_none=True)) != digest(json.loads(reservation["delegation"]))):
            raise ValueError("delegation does not match a durable reservation")

    async def _prepare(self, params, context):
        if context.state.get("internal"):
            return None
        try:
            data = list(get_data_parts(params.message.parts))
            if len(data) != 1 or not isinstance(data[0], dict):
                raise ValueError("exactly one JSON data part is required")
            raw_meta = _meta_dict(params.message)
            actor = self._actor(context, raw_meta.get("from_agent"))
            # Fixture callers still get a stable principal for the task's subsequent ACL.
            context.state["actor"] = actor
            if params.message.task_id:
                row = await self._authorize_task(params.message.task_id, context, resume=True)
                if (params.message.context_id != row["envelope"]["case_id"]
                        or raw_meta.get("case_id") != row["envelope"]["case_id"]):
                    raise ValueError("resume must preserve the original case and context")
                wait_key, value = data[0].get("wait_key"), data[0].get("data")
                key = raw_meta.get("idempotency_key")
                if not isinstance(wait_key, str) or not wait_key or not isinstance(value, dict) or not key:
                    raise ValueError("resume requires wait_key, JSON data and idempotency_key")
                if actor == "gateway" and not wait_key.startswith("call:"):
                    raise ValueError("gateway may only resume call waits")
                if row["status"] in {"completed", "failed", "canceled", "rejected"}:
                    return row["id"]
                if row["wait_key"] and row["wait_key"] != wait_key:
                    raise ValueError("resume does not match the task's pending wait")
                fresh = await self.store.put_input(row["id"], wait_key, str(key), value)
                self._recovery_wakeup.set()
                context.state["admitted_id"] = row["id"]
                # An early callback is durable even while the task is entering its wait.
                if not fresh or row["lease_until"] > time.time() or row["status"] != "waiting":
                    return row["id"]
                return None if await self.store.queue_recoverable(row) else row["id"]
            env = TaskEnvelope.model_validate(data[0])
            meta = DelegationMeta.model_validate(raw_meta)
            self._validate_initial(params, actor, env, meta)
            key = meta.idempotency_key or params.message.message_id
            if not key:
                raise ValueError("idempotency_key or message_id is required")
            # Resolve defaults before hashing so a retransmission has semantic identity.
            meta.idempotency_key = key
            await self._validate_parent(env, meta)
            params.message.context_id = env.case_id
            row, fresh = await self.store.admit(self.name, actor, key, env.model_dump(),
                meta.model_dump(exclude_none=True), MessageToDict(params.message))
            context.state["admitted_id"] = row["id"]
            return None if fresh else row["id"]
        except (ValueError, ValidationError, IdempotencyConflict) as error:
            raise InvalidParamsError(message=str(error)) from None

    async def _snapshot(self, task_id, context):
        for _ in range(100):
            stored = await self.snapshots.get(task_id, context)
            if stored:
                return stored.task
            await asyncio.sleep(0.02)
        raise InvalidParamsError(message="task admitted; retry to retrieve its pending snapshot")

    async def on_message_send(self, params, context):
        params = SendMessageRequest.FromString(params.SerializeToString())
        async with self._admission_lock:
            duplicate = await self._prepare(params, context)
        if duplicate:
            return await self._snapshot(duplicate, context)
        return await super().on_message_send(params, context)

    async def on_message_send_stream(self, params, context):
        params = SendMessageRequest.FromString(params.SerializeToString())
        async with self._admission_lock:
            duplicate = await self._prepare(params, context)
        if duplicate:
            yield await self._snapshot(duplicate, context)
            return
        async for event in super().on_message_send_stream(params, context):
            yield event

    async def on_get_task(self, params, context):
        await self._authorize_task(params.id, context)
        return await super().on_get_task(params, context)

    async def on_list_tasks(self, params, context):
        if self._actor(context) not in {"router", self.name}:
            raise InvalidParamsError(message="caller may retrieve known tasks only")
        return await super().on_list_tasks(params, context)

    async def on_cancel_task(self, params, context):
        await self._authorize_task(params.id, context, resume=True)
        await self.store.cancel(params.id)
        return await super().on_cancel_task(params, context)

    async def on_subscribe_to_task(self, params, context):
        await self._authorize_task(params.id, context)
        async for event in super().on_subscribe_to_task(params, context):
            yield event

    async def reconcile_once(self):
        from common.a2a_transport import get_task, send_task
        if os.getenv("SPACETIME_DISABLED", "1") != "1":
            await self.executor.flush_projections()
        for row in await self.store.recoverable(self.name):
            context = ServerCallContext(state={"internal": True, "admitted_id": row["id"]})
            stored = await self.snapshots.get(row["id"], context)
            if stored and stored.task.status.state in TERMINAL_STATES:
                state = TaskState.Name(stored.task.status.state).removeprefix("TASK_STATE_").lower()
                await self.store.repair_terminal(row, state)
                continue
            if row["status"] == "waiting":
                if row["wait_kind"] == "child":
                    dependency = next((d for d in await self.store.get_dependencies(row["id"])
                                       if d["child_id"] == row["wait_key"]), None)
                    if dependency:
                        try:
                            if dependency["state"] == "pending":
                                child = await send_task(dependency["target"],
                                    TaskEnvelope.model_validate_json(dependency["envelope"]),
                                    DelegationMeta.model_validate_json(dependency["delegation"]))
                            else:
                                child = await get_task(dependency["target"], dependency["child_id"], sender=self.name)
                            if child.id != dependency["child_id"] or child.context_id != row["envelope"]["case_id"]:
                                raise ValueError("child identity mismatch")
                            if dependency["state"] == "pending":
                                await self.store.finish_dependency(dependency["id"], "accepted", [])
                            if child.status.state in TERMINAL_STATES:
                                state = TaskState.Name(child.status.state).removeprefix("TASK_STATE_").lower()
                                await self.store.finish_dependency(dependency["id"], state, extract_artifact_data(child))
                                await self.store.put_input(row["id"], row["wait_key"], f"child:{child.id}:{state}",
                                                           {"state": state, "child_id": child.id})
                        except Exception as error:  # noqa: BLE001 -- retain dependency on peer failure
                            log.warning("Child reconciliation deferred: %s", type(error).__name__)
                            continue
                if not await self.store.pending_inputs(row["id"], row["wait_key"]):
                    continue
            # Backpressure: only queue a task once per process, even before it claims a lease.
            if await self._active_task_registry.get(row["id"]) is not None and row["status"] != "waiting":
                continue
            message = ParseDict(row["message"], Message())
            if stored:
                message.task_id = row["id"]
            else:
                message.ClearField("task_id")
            if not await self.store.queue_recoverable(row):
                continue
            await self.on_message_send(SendMessageRequest(message=message,
                configuration=SendMessageConfiguration(return_immediately=True)), context)

    async def recovery_loop(self):
        while True:
            self._recovery_wakeup.clear()
            try:
                _readiness(self.executor)
                await self.reconcile_once()
            except Exception as error:  # noqa: BLE001 -- keep recovering after a broken task
                log.warning("Recovery pass deferred: %s", type(error).__name__)
            try:
                await asyncio.wait_for(self._recovery_wakeup.wait(), timeout=5)
            except TimeoutError:
                pass


def _readiness(executor):
    from common.providers import configuration, mode
    provider = configuration(executor.name)
    if mode() == "live":
        if provider.provider != "openai":
            raise ValueError("live model-selected tools require OpenAI")
        if os.getenv("SPACETIME_DISABLED", "1") != "0":
            raise ValueError("live mode requires SpacetimeDB")
        if executor.store.engine.dialect.name != "postgresql":
            raise ValueError("live mode requires PostgreSQL task storage")
        if not os.getenv(f"SPACETIME_TOKEN_{executor.name.upper()}"):
            raise ValueError("live mode requires the service actor SpacetimeDB credential")
        if not os.getenv("SPACETIME_URL") or not os.getenv("SPACETIME_DB"):
            raise ValueError("live mode requires explicit SpacetimeDB URL and database")
        from common.source_repository import source_backend
        # SpacetimeDB is required above, so the spacetime backend needs no local file.
        if source_backend() == "file" and (not os.getenv("SOURCE_DATA_PATH")
                                           or not Path(os.environ["SOURCE_DATA_PATH"]).is_file()):
            raise ValueError("live mode requires an accessible source dataset")
        credentials = [os.getenv(f"A2A_TOKEN_{actor.upper()}", "") for actor in sorted(SERVICE_ACTORS)]
        if any(not value for value in credentials) or len(set(credentials)) != len(credentials):
            raise ValueError("live mode requires distinct per-service A2A tokens")


async def publish_agent_presence(executor: FreightAgent, emission_id: str, *, db=None) -> bool:
    """Publish service liveness separately from task snapshots and lease renewal."""
    if os.getenv("SPACETIME_DISABLED", "1") == "1":
        return False
    from common.spacetime_client import SpacetimeClient
    client = db or SpacetimeClient(actor=executor.name)
    config = await client.get_agent_config(executor.name)
    # Disabled agents are also rejected by W2's principal guard. Do not overwrite
    # an operator's disabled state with an optimistic online heartbeat.
    if config is None or not config.enabled:
        return False
    async with executor.store.engine.connect() as conn:
        active = (await conn.execute(select(tasks.c.id).where(
            tasks.c.agent == executor.name, tasks.c.status == "working", tasks.c.lease_until > time.time(),
        ).limit(1))).scalar_one_or_none()
    await client.mutate("agent_heartbeat", {
        "write_id": emission_id, "name": executor.name, "status": "busy" if active else "idle",
        "card_url": f"{AGENTS[executor.name].url}/.well-known/agent-card.json",
    })
    return True


async def agent_presence_loop(executor: FreightAgent, *, interval: float = 15) -> None:
    instance_id = str(uuid.uuid4())
    sequence = 0
    while True:
        emission_id = stable_id("agent-presence", executor.name, instance_id, str(sequence))
        try:
            await publish_agent_presence(executor, emission_id)
        except Exception as error:  # noqa: BLE001 -- a presence outage cannot stop task execution
            log.warning("Agent presence deferred for %s: %s", executor.name, type(error).__name__)
        sequence += 1
        await asyncio.sleep(interval)


def build_app(executor: FreightAgent, descriptions: dict[str, str]) -> Starlette:
    card = build_card(AGENTS[executor.name], descriptions)
    snapshots = VersionedDatabaseTaskStore(executor.store.engine, owner_resolver=lambda _: executor.name)
    events = DatabaseTaskEventStream(executor.store.engine, poll_interval_s=0.1)
    handler = DurableRequestHandler(executor, card, snapshots, events)

    @asynccontextmanager
    async def lifespan(app):
        await executor.store.initialize()
        async with executor.store.schema_lock():
            await snapshots.initialize()
            await events.initialize()
        recovery = asyncio.create_task(handler.recovery_loop())
        presence = asyncio.create_task(agent_presence_loop(executor)) \
            if os.getenv("SPACETIME_DISABLED", "1") != "1" else None
        try:
            yield
        finally:
            recovery.cancel()
            if presence:
                presence.cancel()
            await asyncio.gather(recovery, *([presence] if presence else []), return_exceptions=True)
            await handler.aclose()
            await executor.store.close()

    async def health(_):
        return JSONResponse({"ok": True, "agent": executor.name})

    async def ready(_):
        try:
            _readiness(executor)
            async with executor.store.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            if os.getenv("FREIGHTOPS_MODE", "fixture") == "live":
                from common.spacetime_client import SpacetimeClient
                config = await SpacetimeClient(actor=executor.name).get_agent_config(executor.name)
                if config is None or not config.enabled:
                    raise ValueError("service is absent or disabled in SpacetimeDB")
                from common.providers import configuration
                if configuration(executor.name, config.model).provider != "openai":
                    raise ValueError("live model-selected tools require OpenAI")
        except Exception as error:  # noqa: BLE001 -- readiness must report all dependency failures
            return JSONResponse({"ready": False, "reason": type(error).__name__}, status_code=503)
        return JSONResponse({"ready": True, "agent": executor.name,
                             "mode": os.getenv("FREIGHTOPS_MODE", "fixture")})

    app = Starlette(lifespan=lifespan, routes=[Route("/healthz", health), Route("/readyz", ready),
        *create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/", context_builder=AuthContextBuilder())])
    app.state.handler = handler
    app.state.executor = executor

    async def authenticate(request, call_next):
        if request.url.path in {"/healthz", "/readyz", "/.well-known/agent-card.json"}:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        actor = next((name for name in SERVICE_ACTORS if (token := os.getenv(f"A2A_TOKEN_{name.upper()}", ""))
                      and hmac.compare_digest(header, f"Bearer {token}")), None)
        if os.getenv("FREIGHTOPS_MODE", "fixture") == "live":
            if not actor:
                return JSONResponse({"error": "service authentication required"}, status_code=401)
            try:
                _readiness(executor)
            except ValueError:
                return JSONResponse({"error": "service configuration is not ready"}, status_code=503)
        elif header and actor is None:
            return JSONResponse({"error": "invalid service credential"}, status_code=401)
        request.state.a2a_actor = actor
        return await call_next(request)
    app.add_middleware(BaseHTTPMiddleware, dispatch=authenticate)
    return app


def serve(executor: FreightAgent, descriptions: dict[str, str]) -> None:
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s {executor.name} %(levelname)s %(message)s")
    host = os.getenv("A2A_BIND_HOST", "127.0.0.1")
    if os.getenv("FREIGHTOPS_MODE", "fixture") == "fixture" and host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("unauthenticated fixture mode must bind to loopback")
    uvicorn.run(build_app(executor, descriptions), host=host, port=AGENTS[executor.name].port, log_level="warning")
