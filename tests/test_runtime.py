"""Durability and isolation tests against the installed A2A 1.0 SDK, with real SQLite."""
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from a2a.helpers import new_data_message
from a2a.server.context import ServerCallContext
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError

from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.runtime import DelegationRefused, FreightAgent, extract_artifact_data
from common.runtime_store import IdempotencyConflict, LeaseLost, RuntimeStore, task_id_for
from common.server import build_app


@pytest.fixture(autouse=True)
def fixture_environment(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "1")


def request(key="once", *, subject=None, context="case-one"):
    message = new_data_message({"task_type": "investigate_charge", "case_id": context,
        "subject": subject or {"amount": 271.25}}, context_id=context, role=Role.ROLE_USER)
    message.metadata.update({"case_id": context, "from_agent": "router", "idempotency_key": key,
                             "correlation_id": "correlation-one"})
    return SendMessageRequest(message=message, configuration=SendMessageConfiguration(return_immediately=False))


def resume(task_id, *, wait_key="evidence:packet", data=None, key="callback-once", context="case-one"):
    message = new_data_message({"wait_key": wait_key, "data": data or {"arrived": True}},
                               context_id=context, role=Role.ROLE_USER)
    message.task_id = task_id
    message.metadata.update({"case_id": context, "from_agent": "router", "idempotency_key": key})
    return SendMessageRequest(message=message, configuration=SendMessageConfiguration(return_immediately=False))


class CountingAgent(FreightAgent):
    name = "risk"
    calls = 0

    async def handle(self, env, meta, task_id, updater):
        self.calls += 1
        await asyncio.sleep(0.02)
        return ResultArtifact(outcome="clear", findings={"subject": env.subject, "sender": meta.from_agent})


class WaitingAgent(CountingAgent):
    stage_calls = 0

    async def handle(self, env, meta, task_id, updater):
        self.calls += 1

        async def first_stage():
            self.stage_calls += 1
            return {"amount": env.subject["amount"]}
        first = await self.step(task_id, "read_source", first_stage)
        callback = await self.resume_data(task_id, "evidence:packet")
        if callback is None:
            await self.park(task_id, "awaiting_external_verification", "evidence", "evidence:packet")
        return ResultArtifact(outcome="verified", findings={"first": first, "callback": callback,
            "sender": meta.from_agent, "correlation": meta.correlation_id, "case_id": env.case_id})


@asynccontextmanager
async def running(tmp_path, cls=CountingAgent):
    agent = cls(store=RuntimeStore(f"sqlite+aiosqlite:///{tmp_path}/runtime.sqlite3"))
    app = build_app(agent, {})
    async with app.router.lifespan_context(app):
        yield app.state.handler, agent, app


async def completed(handler, task_id):
    async with asyncio.timeout(3):
        while True:
            task = await handler.on_get_task(GetTaskRequest(id=task_id), ServerCallContext())
            if task.status.state == TaskState.TASK_STATE_COMPLETED:
                return task
            await asyncio.sleep(0.02)


async def test_semantic_dedup_precedes_execution_and_rejects_changed_payload(tmp_path):
    async with running(tmp_path) as (handler, agent, _):
        results = await asyncio.gather(*[
            handler.on_message_send(request(), ServerCallContext()) for _ in range(8)
        ])
        assert len({task.id for task in results}) == 1
        assert results[0].id == task_id_for("risk", "router", "case-one", "once")
        assert agent.calls == 1
        with pytest.raises(InvalidParamsError, match="different request"):
            await handler.on_message_send(request(subject={"amount": 18.5}), ServerCallContext())
        other = await handler.on_message_send(request(context="case-two"), ServerCallContext())
        assert other.id != results[0].id
        assert agent.calls == 2


async def test_wait_resume_keeps_original_envelope_and_checkpoints(tmp_path):
    async with running(tmp_path, WaitingAgent) as (handler, agent, _):
        waiting = await handler.on_message_send(request(), ServerCallContext())
        assert waiting.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert not waiting.artifacts
        result = await handler.on_message_send(resume(waiting.id), ServerCallContext())
        result = await completed(handler, result.id)
        findings = extract_artifact_data(result)[0]["findings"]
        assert findings == {"first": {"amount": 271.25}, "callback": {"arrived": True},
                            "sender": "router", "correlation": "correlation-one", "case_id": "case-one"}
        assert agent.stage_calls == 1
        assert agent.calls == 2
        duplicate = await handler.on_message_send(resume(waiting.id), ServerCallContext())
        assert duplicate.id == waiting.id
        assert agent.calls == 2


async def test_wait_survives_server_restart_and_inbox_replays(tmp_path):
    async with running(tmp_path, WaitingAgent) as (handler, agent, _):
        waiting = await handler.on_message_send(request(), ServerCallContext())
        assert agent.stage_calls == 1
    async with running(tmp_path, WaitingAgent) as (handler, agent, _):
        assert (await handler.on_get_task(GetTaskRequest(id=waiting.id), ServerCallContext())).status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        # Persisted inbox models the crash between receiving a callback and scheduling execution.
        await agent.store.put_input(waiting.id, "evidence:packet", "inbox-event", {"arrived": True})
        await handler.reconcile_once()
        result = await completed(handler, waiting.id)
        assert extract_artifact_data(result)[0]["findings"]["first"]["amount"] == 271.25
        assert agent.stage_calls == 0


async def test_early_callback_is_durable_until_task_enters_wait(tmp_path):
    class EarlyAgent(WaitingAgent):
        entered = asyncio.Event()
        proceed = asyncio.Event()

        async def handle(self, env, meta, task_id, updater):
            self.entered.set()
            await self.proceed.wait()
            return await super().handle(env, meta, task_id, updater)

    async with running(tmp_path, EarlyAgent) as (handler, agent, _):
        submission = request()
        submission.configuration.return_immediately = True
        initial = await handler.on_message_send(submission, ServerCallContext())
        await agent.entered.wait()
        await handler.on_message_send(resume(initial.id), ServerCallContext())
        agent.proceed.set()
        result = await completed(handler, initial.id)
        assert extract_artifact_data(result)[0]["findings"]["callback"]["arrived"] is True
        assert agent.calls == 1


async def test_cancel_waiting_task_fences_future_execution(tmp_path):
    async with running(tmp_path, WaitingAgent) as (handler, agent, _):
        waiting = await handler.on_message_send(request(), ServerCallContext())
        canceled = await handler.on_cancel_task(CancelTaskRequest(id=waiting.id), ServerCallContext())
        assert canceled.status.state == TaskState.TASK_STATE_CANCELED
        await handler.on_message_send(resume(waiting.id), ServerCallContext())
        await handler.reconcile_once()
        assert agent.calls == 1
        assert (await agent.store.get(waiting.id))["status"] == "canceled"


async def test_service_identity_and_task_read_isolation(tmp_path):
    async with running(tmp_path) as (handler, _, app):
        with pytest.raises(InvalidParamsError, match="identity"):
            await handler.on_message_send(request(), ServerCallContext(state={"actor": "shipper_ar"}))
        result = await handler.on_message_send(request(), ServerCallContext(state={"actor": "router"}))
        with pytest.raises(TaskNotFoundError):
            await handler.on_get_task(GetTaskRequest(id=result.id), ServerCallContext(state={"actor": "shipper_ar"}))
        bad = resume(result.id, context="case-other")
        with pytest.raises(InvalidParamsError, match="case and context"):
            await handler.on_message_send(bad, ServerCallContext())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            assert (await http.get("/healthz")).status_code == 200
            assert (await http.get("/readyz")).json()["mode"] == "fixture"
            assert (await http.post("/", headers={"Authorization": "Bearer invalid"})).status_code == 401


async def test_store_leases_and_callback_conflicts(tmp_path):
    store = RuntimeStore(f"sqlite+aiosqlite:///{tmp_path}/runtime.sqlite3")
    try:
        row, _ = await store.admit("risk", "router", "key", {"case_id": "case"}, {}, {})
        generation = await store.claim(row["id"], "owner")
        assert generation == 1
        assert await store.claim(row["id"], "other") is None
        await store.put_input(row["id"], "wait", "key", {"ok": True})
        assert not await store.put_input(row["id"], "wait", "key", {"ok": True})
        with pytest.raises(IdempotencyConflict):
            await store.put_input(row["id"], "wait", "key", {"ok": False})
        await store.cancel(row["id"])
        with pytest.raises(LeaseLost):
            await store.assert_lease(row["id"], "owner", generation)
        await store.release(row["id"], "owner", generation, status="completed")
        assert (await store.get(row["id"]))["status"] == "canceled"
    finally:
        await store.close()


async def test_delegation_guards_and_fanout_reservations(tmp_path):
    agent = CountingAgent(store=RuntimeStore(f"sqlite+aiosqlite:///{tmp_path}/runtime.sqlite3"))
    try:
        meta = DelegationMeta(case_id="case", from_agent="router")
        env = TaskEnvelope(task_type="process_carrier_invoice", case_id="case")
        with pytest.raises(DelegationRefused):
            await agent.delegate(env.task_type, env, meta, "parent")
        env.task_type = "verify_claim"
        with pytest.raises(DelegationRefused, match="depth"):
            await agent.delegate(env.task_type, env, meta.model_copy(update={"delegation_depth": 3}), "parent")
        with pytest.raises(DelegationRefused, match="cycle"):
            await agent.delegate(env.task_type, env, meta.model_copy(update={"visited": ["verification"]}), "parent")
        admitted, _ = await agent.store.admit("risk", "router", "parent", {"case_id": "case"}, {}, {})
        results = await asyncio.gather(*[
            agent.store.add_dependency(admitted["id"], f"operation-{i}", "verification", f"child-{i}", {}, {})
            for i in range(4)
        ], return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 3
        assert sum(isinstance(result, ValueError) for result in results) == 1
    finally:
        await agent.store.close()


async def test_stale_callback_does_not_repeatedly_wake_same_wait(tmp_path):
    class StillWaitingAgent(CountingAgent):
        async def handle(self, env, meta, task_id, updater):
            self.calls += 1
            await self.park(task_id, "awaiting_external_verification", "evidence", "evidence:packet")

    async with running(tmp_path, StillWaitingAgent) as (handler, agent, _):
        task = await handler.on_message_send(request(), ServerCallContext())
        await handler.on_message_send(resume(task.id), ServerCallContext())
        # The callback can arrive during the first execution's cleanup.
        await asyncio.sleep(0.02)
        await handler.reconcile_once()
        await asyncio.sleep(0.02)
        assert agent.calls == 2
        for _ in range(3):
            await handler.reconcile_once()
        assert agent.calls == 2
        assert not await agent.store.pending_inputs(task.id, "evidence:packet")
        await handler.on_message_send(resume(task.id, key="new-event"), ServerCallContext())
        await asyncio.sleep(0.02)
        await handler.reconcile_once()
        assert agent.calls == 3


async def test_shared_schema_can_boot_nine_services_concurrently(tmp_path):
    from common.config import AGENTS

    async def boot(name):
        agent = FreightAgent(store=RuntimeStore(f"sqlite+aiosqlite:///{tmp_path}/shared.sqlite3"))
        agent.name = name
        app = build_app(agent, {})
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.02)
    await asyncio.gather(*[boot(name) for name in AGENTS])


async def test_live_delegation_must_match_reserved_parent(tmp_path, monkeypatch):
    # Parent admission checks do not depend on a network database; the live readiness
    # endpoint separately rejects SQLite and absent credentials.
    async with running(tmp_path) as (handler, agent, _):
        parent_env = TaskEnvelope(task_type="process_carrier_invoice", case_id="case")
        parent_meta = DelegationMeta(case_id="case", from_agent="router", idempotency_key="parent")
        parent, _ = await agent.store.admit("carrier_ap", "router", "parent", parent_env.model_dump(),
                                          parent_meta.model_dump(exclude_none=True), {})
        env = TaskEnvelope(task_type="investigate_charge", case_id="case")
        meta = DelegationMeta(case_id="case", from_agent="carrier_ap", parent_task_id=parent["id"],
            causation_id=parent["id"], correlation_id="case", delegation_depth=1, visited=["carrier_ap"],
            idempotency_key="delegation-one")
        monkeypatch.setenv("FREIGHTOPS_MODE", "live")
        with pytest.raises(ValueError, match="reservation"):
            await handler._validate_parent(env, meta)
        await agent.store.add_dependency(parent["id"], "review", "risk",
            task_id_for("risk", "carrier_ap", "case", "delegation-one"), env.model_dump(),
            meta.model_dump(exclude_none=True))
        await handler._validate_parent(env, meta)
        with pytest.raises(ValueError, match="ancestry"):
            await handler._validate_parent(env, meta.model_copy(update={"visited": ["shipper_ar"]}))


async def test_lease_release_waits_for_protocol_snapshot_acknowledgement(tmp_path):
    async with running(tmp_path) as (handler, agent, _):
        entered, allow = asyncio.Event(), asyncio.Event()
        confirm = agent.confirm_state

        async def delayed_confirmation(task_id, state):
            entered.set()
            await allow.wait()
            await confirm(task_id, state)
        agent.confirm_state = delayed_confirmation
        submission = request()
        submission.configuration.return_immediately = True
        task = await handler.on_message_send(submission, ServerCallContext())
        await entered.wait()
        row = await agent.store.get(task.id)
        assert row["status"] == "working"
        assert row["lease_owner"]
        allow.set()
        async with asyncio.timeout(2):
            while (await agent.store.get(task.id))["status"] != "completed":
                await asyncio.sleep(0.01)


async def test_completed_result_checkpoint_recovers_after_expired_execution_lease(tmp_path):
    from google.protobuf.json_format import MessageToDict
    store = RuntimeStore(f"sqlite+aiosqlite:///{tmp_path}/runtime.sqlite3")
    submission = request()
    env = TaskEnvelope(task_type="investigate_charge", case_id="case-one", subject={"amount": 271.25})
    meta = DelegationMeta(case_id="case-one", from_agent="router", idempotency_key="once")
    row, _ = await store.admit("risk", "router", "once", env.model_dump(), meta.model_dump(exclude_none=True),
                               MessageToDict(submission.message))
    await store.claim(row["id"], "crashed-worker", seconds=-1)
    await store.set_step(row["id"], "__result", ResultArtifact(outcome="clear", findings={"saved": True}).model_dump())
    await store.close()
    async with running(tmp_path) as (handler, agent, _):
        async with asyncio.timeout(3):
            while True:
                try:
                    result = await completed(handler, row["id"])
                    break
                except TaskNotFoundError:
                    await asyncio.sleep(0.01)
        assert extract_artifact_data(result)[0]["findings"] == {"saved": True}
        assert agent.calls == 0


async def test_projection_outbox_retries_in_order_with_original_write_ids(tmp_path, monkeypatch):
    from common.spacetime_client import SpacetimeClient, SpacetimeTransportError
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    outage = True
    delivered = []

    async def mutate(_self, reducer, payload):
        assert reducer == "upsert_task"
        if outage:
            raise SpacetimeTransportError(timeout=True, may_have_committed=True)
        delivered.append(payload)
    monkeypatch.setattr(SpacetimeClient, "mutate", mutate)
    async with running(tmp_path) as (handler, agent, _):
        result = await handler.on_message_send(request(), ServerCallContext())
        assert result.status.state == TaskState.TASK_STATE_COMPLETED
        pending = await agent.store.pending_projections("risk")
        assert len(pending) == 2
        original_ids = [event["id"] for event in pending]
        outage = False
        await agent.flush_projections()
        assert [event["a2a_state"] for event in delivered] == ["working", "completed"]
        assert [event["write_id"] for event in delivered] == original_ids
        assert not await agent.store.pending_projections("risk")
