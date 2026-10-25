"""Delegated children that were reserved but never admitted (live, Oct 25 2026).

Twenty Voice Desk `place_call` rows stayed `submitted` for good: Verification reserved each child with
`record_delegation`, then Voice Desk refused the A2A send ("delegation does not match a durable reservation")
because the data part had turned the reserved envelope's integers into doubles. The refusal reached the model as
a tool error, the parent burned its turns and failed, and nothing ever closed the reserved row.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from a2a.helpers import new_data_message
from a2a.server.context import ServerCallContext
from a2a.types import Role, SendMessageConfiguration, SendMessageRequest, TaskState
from a2a.utils.errors import InternalError, InvalidParamsError

from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.runtime import ChildFailed, FreightAgent, minimal_state_projection
from common.runtime_store import RuntimeStore, task_id_for, wire_form
from common.server import build_app

CASE = "CASE-LD-1025-2E406317-BANK"
# build_voice_context facts carry integers (free minutes, quantities): the shape that was refused.
CALL_SUBJECT = {"purpose": "verification", "counterparty_id": "C-OFF-2", "questions": ["Did you ask?"],
                "facts": {"free_minutes": 120, "claimed_amount": 650.0, "lines": [{"qty": 1}]}}


@pytest.fixture(autouse=True)
def fixture_environment(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "1")


class Callee(FreightAgent):
    name = "voice_desk"
    calls = 0

    async def handle(self, env, meta, task_id, updater):
        self.calls += 1
        return ResultArtifact(outcome="unverified", findings={"facts": env.subject.get("facts")})


class Caller(FreightAgent):
    """A Verification parent that, like the real one, decides without a callback that cannot happen."""
    name = "verification"
    runs = 0

    async def handle(self, env, meta, task_id, updater):
        self.runs += 1
        call = TaskEnvelope(task_type="place_call", case_id=env.case_id, subject=CALL_SUBJECT)
        try:
            results = await self.delegate("place_call", call, meta, task_id, invocation_key="verified_callback")
        except ChildFailed as error:
            return ResultArtifact(outcome="unverified", findings={"state": error.state, "reason": error.reason})
        return ResultArtifact(outcome="verified", findings={"results": results})


@asynccontextmanager
async def serving(tmp_path, cls):
    agent = cls(store=RuntimeStore(f"sqlite+aiosqlite:///{tmp_path}/runtime.sqlite3"))
    app = build_app(agent, {})
    async with app.router.lifespan_context(app):
        yield app.state.handler, agent


def root_request(case=CASE, key="root"):
    message = new_data_message({"task_type": "verify_bank_change", "case_id": case}, context_id=case,
                               role=Role.ROLE_USER)
    message.metadata.update({"case_id": case, "from_agent": "router", "idempotency_key": key})
    return SendMessageRequest(message=message, configuration=SendMessageConfiguration(return_immediately=False))


async def reserve_like_before(store, parent_id, *, deadline_s=120):
    """A reservation as the old sender stored it: the envelope exactly as built, integers and all."""
    env = TaskEnvelope(task_type="place_call", case_id=CASE, subject=CALL_SUBJECT, deadline_s=deadline_s)
    key = f"{parent_id}:verified_callback"
    meta = DelegationMeta(case_id=CASE, from_agent="verification", parent_task_id=parent_id, delegation_depth=1,
                          visited=["verification"], idempotency_key=key, correlation_id=CASE,
                          causation_id=parent_id)
    child_id = task_id_for("voice_desk", "verification", CASE, key)
    dependency = await store.add_dependency(parent_id, "verified_callback", "voice_desk", child_id,
                                            env.model_dump(), meta.model_dump(exclude_none=True))
    return env, meta, child_id, dependency


async def admit_parent(store):
    env = TaskEnvelope(task_type="verify_bank_change", case_id=CASE)
    meta = DelegationMeta(case_id=CASE, from_agent="router", idempotency_key="root")
    parent, _ = await store.admit("verification", "router", "root", env.model_dump(),
                                  meta.model_dump(exclude_none=True), {})
    return parent


def child_request(env, meta):
    message = new_data_message(env.model_dump(), context_id=env.case_id, role=Role.ROLE_USER)
    message.metadata.update(meta.model_dump(exclude_none=True))
    return SendMessageRequest(message=message, configuration=SendMessageConfiguration(return_immediately=False))


def test_wire_form_is_what_a_peer_receives():
    assert wire_form({"n": 120, "x": 1.5, "s": "a", "z": None, "l": [1, True]}) == \
        {"n": 120.0, "x": 1.5, "s": "a", "z": None, "l": [1.0, True]}


async def test_a_reserved_envelope_with_integers_is_admitted(tmp_path, monkeypatch):
    """The live refusal: the child sees 120.0 where the reservation holds 120."""
    async with serving(tmp_path, Callee) as (handler, agent):
        parent = await admit_parent(agent.store)
        env, meta, child_id, _ = await reserve_like_before(agent.store, parent["id"])
        monkeypatch.setenv("FREIGHTOPS_MODE", "live")
        task = await handler.on_message_send(child_request(env, meta),
                                             ServerCallContext(state={"actor": "verification"}))
        assert task.id == child_id
        async with asyncio.timeout(5):
            while (await agent.store.get(child_id))["status"] != "completed":
                await asyncio.sleep(0.02)
        assert agent.calls == 1


async def test_delegation_reserves_the_envelope_as_the_child_receives_it(tmp_path, monkeypatch):
    sent = []

    async def send_task(agent, envelope, meta):
        sent.append(envelope)
        raise httpx.ConnectError("peer down")
    monkeypatch.setattr("common.a2a_transport.send_task", send_task)
    async with serving(tmp_path, Caller) as (handler, agent):
        task = await handler.on_message_send(root_request(), ServerCallContext())
        assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        [dependency] = await agent.store.get_dependencies(task.id)
        reserved = json.loads(dependency["envelope"])
        assert reserved["subject"]["facts"]["free_minutes"] == 120.0
        assert reserved == wire_form(sent[0].model_dump())


async def test_a_refused_send_closes_the_reservation_and_the_parent_decides(tmp_path, monkeypatch):
    async def send_task(agent, envelope, meta):
        raise InvalidParamsError(message="delegation does not match a durable reservation")
    monkeypatch.setattr("common.a2a_transport.send_task", send_task)
    async with serving(tmp_path, Caller) as (handler, agent):
        task = await handler.on_message_send(root_request(), ServerCallContext())
        assert task.status.state == TaskState.TASK_STATE_COMPLETED
        [dependency] = await agent.store.get_dependencies(task.id)
        assert dependency["state"] == "rejected"
        result = await agent.store.get_step(task.id, "__result")
        assert result["outcome"] == "unverified" and result["findings"]["state"] == "rejected"
        assert "durable reservation" in result["findings"]["reason"]
        assert agent.runs == 1


async def test_a_parent_waiting_on_an_unreachable_child_is_woken_by_a_refused_resend(tmp_path, monkeypatch):
    attempts = []

    async def send_task(agent, envelope, meta):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("peer restarting")
        raise InvalidParamsError(message="delegation does not match a durable reservation")
    monkeypatch.setattr("common.a2a_transport.send_task", send_task)
    async with serving(tmp_path, Caller) as (handler, agent):
        task = await handler.on_message_send(root_request(), ServerCallContext())
        assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        async with asyncio.timeout(5):
            while (await agent.store.get(task.id))["status"] != "completed":
                await handler.reconcile_once()
                await asyncio.sleep(0.02)
        assert (await agent.store.get_step(task.id, "__result"))["outcome"] == "unverified"
        assert len(attempts) == 2, "a refused delegation is not resent"


async def test_a_parent_waiting_on_a_closed_reservation_is_woken_without_contacting_the_peer(tmp_path, monkeypatch):
    async def send_task(agent, envelope, meta):
        raise httpx.ConnectError("peer down")
    monkeypatch.setattr("common.a2a_transport.send_task", send_task)
    async with serving(tmp_path, Caller) as (handler, agent):
        task = await handler.on_message_send(root_request(), ServerCallContext())
        [dependency] = await agent.store.get_dependencies(task.id)
        # The child's service expired the reservation (close_unadmitted_reservation).
        assert await agent.store.close_dependency(dependency["id"], "canceled",
                                                  [{"error": "delegation_expired", "detail": "never admitted"}])

        async def unexpected(*_args, **_kwargs):
            raise AssertionError("a closed reservation is never resent")
        monkeypatch.setattr("common.a2a_transport.send_task", unexpected)
        async with asyncio.timeout(5):
            while (await agent.store.get(task.id))["status"] != "completed":
                await handler.reconcile_once()
                await asyncio.sleep(0.02)
        findings = (await agent.store.get_step(task.id, "__result"))["findings"]
        assert findings == {"state": "canceled", "reason": "never admitted"}


async def test_a_closed_reservation_refuses_a_late_resend(tmp_path, monkeypatch):
    async with serving(tmp_path, Callee) as (handler, agent):
        parent = await admit_parent(agent.store)
        env, meta, _, dependency = await reserve_like_before(agent.store, parent["id"])
        await agent.store.close_dependency(dependency["id"], "canceled", [])
        monkeypatch.setenv("FREIGHTOPS_MODE", "live")
        with pytest.raises(ValueError, match="closed"):
            await handler._validate_parent(env, meta)


async def test_an_admitted_task_whose_snapshot_is_pending_is_a_retryable_error(tmp_path, monkeypatch):
    """A parent treats InvalidParamsError as a final refusal, so this transient case must not use it."""
    async with serving(tmp_path, Callee) as (handler, _agent):
        async def absent(*_args, **_kwargs):
            return None
        monkeypatch.setattr(handler.snapshots, "get", absent)
        with pytest.raises(InternalError):
            await handler._snapshot("task", ServerCallContext())


def submitted_row(child_id, parent_id, *, age_s):
    reserved = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    return {"task_id": child_id, "case_id": CASE, "skill": "place_call", "parent_task_id": parent_id,
            "a2a_state": "submitted", "started_at": reserved, "updated_at": reserved}


@pytest.fixture
def dashboard(monkeypatch):
    """SpacetimeDB task reads return `rows`; upsert_task writes are collected."""
    from common.spacetime_client import SpacetimeClient
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    monkeypatch.setenv("A2A_PROJECTION_DELIVERY", "inline")
    state = {"rows": [], "written": []}

    async def query_rows(_self, sql):
        wanted = sql.rsplit("'", 2)[-2]
        return [row for row in state["rows"] if row["a2a_state"] == wanted] if "FROM task" in sql else []

    async def mutate(_self, reducer, payload):
        if reducer == "upsert_task":
            state["written"].append(payload)
    monkeypatch.setattr(SpacetimeClient, "query_rows", query_rows)
    monkeypatch.setattr(SpacetimeClient, "mutate", mutate)
    return state


async def test_sweep_closes_a_reservation_never_admitted_within_its_deadline(tmp_path, dashboard):
    async with serving(tmp_path, Callee) as (_handler, agent):
        parent = await admit_parent(agent.store)
        _, _, child_id, _ = await reserve_like_before(agent.store, parent["id"], deadline_s=120)
        dashboard["rows"] = [submitted_row(child_id, parent["id"], age_s=150)]
        assert await agent.sweep_orphaned_tasks() == 0, "inside deadline + grace the parent may still send it"
        dashboard["rows"] = [submitted_row(child_id, parent["id"], age_s=200)]
        assert await agent.sweep_orphaned_tasks() == 1
        await agent.flush_projections()
        [row] = dashboard["written"]
        assert row["task_id"] == child_id and row["agent"] == "voice_desk" and row["a2a_state"] == "canceled"
        assert row["business_status"] == "failed" and row["parent_task_id"] == parent["id"]
        assert row["artifact_json"]["findings"]["error"] == "delegation_expired"
        assert "120s" in row["artifact_json"]["findings"]["detail"]
        assert (await agent.store.get_dependencies(parent["id"]))[0]["state"] == "canceled"
        assert await agent.sweep_orphaned_tasks() == 0, "closed once"


async def test_sweep_records_a_refused_reservation_as_failed_without_waiting(tmp_path, dashboard):
    async with serving(tmp_path, Callee) as (_handler, agent):
        parent = await admit_parent(agent.store)
        _, _, child_id, dependency = await reserve_like_before(agent.store, parent["id"], deadline_s=600)
        await agent.store.close_dependency(dependency["id"], "rejected", [
            {"error": "delegation_refused", "detail": "voice_desk refused the delegation: no reservation"}])
        dashboard["rows"] = [submitted_row(child_id, parent["id"], age_s=130)]
        assert await agent.sweep_orphaned_tasks() == 1
        await agent.flush_projections()
        [row] = dashboard["written"]
        assert row["a2a_state"] == "failed"
        assert row["artifact_json"]["findings"] == {"error": "delegation_refused", "admitted": False,
                                                    "detail": "voice_desk refused the delegation: no reservation"}


async def test_sweep_leaves_admitted_and_accepted_children_alone(tmp_path, dashboard):
    async with serving(tmp_path, Callee) as (_handler, agent):
        parent = await admit_parent(agent.store)
        env, meta, child_id, dependency = await reserve_like_before(agent.store, parent["id"])
        await agent.store.finish_dependency(dependency["id"], "accepted", [])
        dashboard["rows"] = [submitted_row(child_id, parent["id"], age_s=10_000)]
        assert await agent.sweep_orphaned_tasks() == 0
        # Pending again, but this service admitted it: its own execution owns the row.
        await agent.store.finish_dependency(dependency["id"], "pending", [])
        await agent.store.admit("voice_desk", "verification", meta.idempotency_key, wire_form(env.model_dump()),
                                meta.model_dump(exclude_none=True), {})
        assert await agent.sweep_orphaned_tasks() == 0
        assert (await agent.store.get_dependencies(parent["id"]))[0]["state"] == "pending"
        assert not dashboard["written"]


async def test_failed_task_rows_say_why(tmp_path, dashboard):
    class Slow(Callee):
        async def handle(self, env, meta, task_id, updater):
            raise TimeoutError
    async with serving(tmp_path, Slow) as (handler, _agent):
        message = new_data_message({"task_type": "place_call", "case_id": CASE, "deadline_s": 300},
                                   context_id=CASE, role=Role.ROLE_USER)
        message.metadata.update({"case_id": CASE, "from_agent": "router", "idempotency_key": "slow"})
        task = await handler.on_message_send(SendMessageRequest(message=message), ServerCallContext())
        assert task.status.state == TaskState.TASK_STATE_FAILED
        failed = [row for row in dashboard["written"] if row["a2a_state"] == "failed"][-1]
        assert failed["artifact_json"]["findings"] == {"error": "TimeoutError",
                                                       "detail": "task deadline of 300s exceeded"}
        # A refused full row falls back to the state alone, and keeps the reason.
        fallback = minimal_state_projection({**failed, "write_id": "w"}, "target")
        assert fallback["artifact_json"]["findings"] == {"details_omitted": True, "error": "TimeoutError",
                                                         "detail": "task deadline of 300s exceeded"}


def test_a_refusal_keeps_only_its_first_line_and_no_contact_details():
    from common.runtime import refusal_text
    error = InvalidParamsError(message="1 validation error for TaskEnvelope\nsubject\n  input_value={'phone': 'x'}")
    assert refusal_text(error) == "1 validation error for TaskEnvelope"
    assert refusal_text(ValueError("call +1 415 555 0123 or a@b.com")) == "call [redacted] or [redacted]"


@pytest.mark.parametrize("error", [InternalError(message="task admitted; retry to retrieve its pending snapshot"),
                                   InvalidParamsError(message="task admitted; retry to retrieve its pending snapshot"),
                                   InvalidParamsError(message="caller identity does not match authenticated service")])
async def test_a_transient_or_identity_refusal_parks_instead_of_closing(tmp_path, monkeypatch, error):
    """An older child answers "task admitted; retry" as InvalidParamsError; a credential mix-up can be repaired."""
    async def send_task(agent, envelope, meta):
        raise error
    monkeypatch.setattr("common.a2a_transport.send_task", send_task)
    async with serving(tmp_path, Caller) as (_handler, agent):
        task = await _handler.on_message_send(root_request(), ServerCallContext())
        assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        [dependency] = await agent.store.get_dependencies(task.id)
        assert dependency["state"] == "pending"
        assert (await agent.store.get(task.id))["wait_details"]["reason"] == "peer_unavailable"


async def test_a_protocol_error_while_polling_the_child_parks(tmp_path, monkeypatch):
    from a2a.types import Task, TaskStatus

    async def send_task(agent, envelope, meta):
        return Task(id=task_id_for("voice_desk", "verification", CASE, f"{meta.parent_task_id}:verified_callback"),
                    context_id=CASE, status=TaskStatus(state=TaskState.TASK_STATE_WORKING))

    async def get_task(agent, task_id, *, sender):
        raise InternalError(message="snapshot store unavailable")
    monkeypatch.setattr("common.a2a_transport.send_task", send_task)
    monkeypatch.setattr("common.a2a_transport.get_task", get_task)
    async with serving(tmp_path, Caller) as (handler, agent):
        task = await handler.on_message_send(root_request(), ServerCallContext())
        assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        [dependency] = await agent.store.get_dependencies(task.id)
        assert dependency["state"] == "accepted"


async def test_a_parent_replays_cleanly_against_an_integer_era_reservation(tmp_path, monkeypatch):
    """Reserved before this change (integers), replayed after it (doubles): the same work, not a conflict."""
    async def send_task(agent, envelope, meta):
        raise InvalidParamsError(message="delegation does not match a durable reservation")
    monkeypatch.setattr("common.a2a_transport.send_task", send_task)
    async with serving(tmp_path, Caller) as (handler, agent):
        parent_id = task_id_for("verification", "router", CASE, "root")
        await reserve_like_before(agent.store, parent_id)
        task = await handler.on_message_send(root_request(), ServerCallContext())
        assert task.id == parent_id and task.status.state == TaskState.TASK_STATE_COMPLETED
        assert (await agent.store.get_step(task.id, "__result"))["findings"]["state"] == "rejected"
        [dependency] = await agent.store.get_dependencies(parent_id)
        assert dependency["state"] == "rejected"


async def test_a_refusal_after_admission_elsewhere_does_not_close_the_reservation(tmp_path):
    async with serving(tmp_path, Caller) as (_handler, agent):
        parent = await admit_parent(agent.store)
        _, _, _, dependency = await reserve_like_before(agent.store, parent["id"])
        assert await agent.store.accept_dependency(dependency["id"])
        assert await agent.refuse_dependency(dependency, InvalidParamsError(message="duplicate")) is None
        assert (await agent.store.get_dependencies(parent["id"]))[0]["state"] == "accepted"
        await agent.store.finish_dependency(dependency["id"], "canceled", [{"error": "delegation_expired"}])
        failure = await agent.refuse_dependency(dependency, InvalidParamsError(message="closed"))
        assert failure.state == "canceled", "an expired reservation is reported as expired, not rejected"


async def test_an_admission_is_not_recorded_over_a_closed_reservation(tmp_path):
    async with serving(tmp_path, Caller) as (_handler, agent):
        parent = await admit_parent(agent.store)
        _, _, _, dependency = await reserve_like_before(agent.store, parent["id"])
        await agent.store.close_dependency(dependency["id"], "canceled",
                                           [{"error": "delegation_expired", "detail": "too late"}])
        with pytest.raises(ChildFailed) as failure:
            await agent.accept_dependency(dependency)
        assert failure.value.state == "canceled" and failure.value.reason == "too late"
        assert (await agent.store.get_dependencies(parent["id"]))[0]["state"] == "canceled"


async def test_the_sweep_waits_for_an_admission_in_progress(tmp_path, dashboard):
    async with serving(tmp_path, Callee) as (handler, agent):
        parent = await admit_parent(agent.store)
        env, meta, child_id, _ = await reserve_like_before(agent.store, parent["id"])
        dashboard["rows"] = [submitted_row(child_id, parent["id"], age_s=10_000)]
        async with handler._admission_lock:  # an admission of this very child is under way
            sweep = asyncio.create_task(agent.sweep_orphaned_tasks())
            await asyncio.sleep(0.1)
            assert not sweep.done()
            await agent.store.admit("voice_desk", "verification", meta.idempotency_key, wire_form(env.model_dump()),
                                    meta.model_dump(exclude_none=True), {})
        assert await sweep == 0
        assert (await agent.store.get_dependencies(parent["id"]))[0]["state"] == "pending"
        assert not dashboard["written"]


async def test_the_sweep_never_ends_a_row_without_its_reservation(tmp_path, dashboard):
    async with serving(tmp_path, Callee) as (_handler, agent):
        dashboard["rows"] = [submitted_row("11111111-1111-5111-8111-111111111111", "parent", age_s=100_000)]
        assert await agent.sweep_orphaned_tasks() == 0
        assert not dashboard["written"]
