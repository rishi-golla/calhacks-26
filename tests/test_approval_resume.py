"""A parked human approval resumes once W2 records a decision, and never otherwise."""
import asyncio
import re
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from a2a.helpers import new_data_message
from a2a.server.context import ServerCallContext
from a2a.types import GetTaskRequest, Role, SendMessageConfiguration, SendMessageRequest, TaskState

import agents.controller.main as controller
from agents.controller.main import ControllerAgent, HumanApproverNotConfigured
from common.agent_support import identifier
from common.envelope import TaskEnvelope
from common.runtime import extract_artifact_data
from common.runtime_store import RuntimeStore
from common.server import _readiness, build_app
from common.spacetime_client import ReducerReceipt, SpacetimeClient, SpacetimeTransportError

CASE = "CASE-APPROVAL"
TARGET = "PAY-APPROVAL"


@pytest.fixture
def w2(monkeypatch):
    """W2 enabled for the controller's approval reads; every other read is empty."""
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    monkeypatch.setenv("HUMAN_APPROVER_ID", "c0ffee")
    monkeypatch.delenv("SOURCE_DATA_PATH", raising=False)
    # The controller treats fixture mode as "no stored approvals"; read them as in live mode.
    monkeypatch.setattr(controller, "fixture_mode", lambda: False)
    state = SimpleNamespace(approvals=[], reads=0, outage=False, writes=[])

    async def query_rows(_self, sql):
        match = re.fullmatch(r"SELECT \* FROM approval WHERE id = '([^']*)'", sql)
        if not match:
            return []
        state.reads += 1
        if state.outage:
            raise SpacetimeTransportError(timeout=True, may_have_committed=False)
        return [dict(row) for row in state.approvals if row["id"] == match.group(1)]

    async def mutate(_self, reducer, payload):
        state.writes.append((reducer, payload))
        return ReducerReceipt(reducer, 200, payload.get("write_id"))

    monkeypatch.setattr(SpacetimeClient, "query_rows", query_rows)
    monkeypatch.setattr(SpacetimeClient, "mutate", mutate)
    return state


class CountingController(ControllerAgent):
    runs = 0

    async def run_workflow(self, env, meta, task_id, updater):
        self.runs += 1
        return await super().run_workflow(env, meta, task_id, updater)


@asynccontextmanager
async def running(tmp_path):
    agent = CountingController(store=RuntimeStore(f"sqlite+aiosqlite:///{tmp_path}/runtime.sqlite3"))
    app = build_app(agent, {})
    async with app.router.lifespan_context(app):
        yield app.state.handler, agent


def payment_review():
    subject = {"case_id": CASE, "invoice_id": "INV-APPROVAL", "invoice_amount": 9400, "contract_rate": 9400,
               "requested_action": "propose_payment", "target_id": TARGET, "evidence_refs": ["source:invoice"]}
    message = new_data_message({"task_type": "review_hold", "case_id": CASE, "subject": subject},
                               context_id=CASE, role=Role.ROLE_USER)
    message.metadata.update({"case_id": CASE, "from_agent": "carrier_ap", "idempotency_key": "approval-once",
                             "parent_task_id": "carrier-parent", "delegation_depth": 1, "visited": ["carrier_ap"]})
    return SendMessageRequest(message=message, configuration=SendMessageConfiguration(return_immediately=False))


async def released(agent, task_id):
    # The protocol snapshot precedes lease release; wait for the durable wait to settle.
    async with asyncio.timeout(3):
        while True:
            row = await agent.store.get(task_id)
            if row["status"] != "working" and not row["lease_owner"]:
                return row
            await asyncio.sleep(0.01)


async def park_for_approval(handler, agent, w2):
    task = await handler.on_message_send(payment_review(), ServerCallContext())
    assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    row = await released(agent, task.id)
    approval_id = identifier(task.id, f"approval:propose_payment:{TARGET}")
    assert (row["status"], row["wait_kind"], row["wait_key"]) == ("waiting", "approval", approval_id)
    requested = [payload for reducer, payload in w2.writes if reducer == "request_approval"]
    assert [(item["id"], item["approver"], item["task_id"]) for item in requested] == [(approval_id, "c0ffee", task.id)]
    return task, approval_id


def approval_row(approval_id, task_id, decision="pending", *, code_ok=False, case_id=CASE):
    return {"id": approval_id, "case_id": case_id, "task_id": task_id, "action": "propose_payment",
            "target_id": TARGET, "amount": 9400, "decision": decision, "code_ok": code_ok}


async def settled(handler, task_id):
    async with asyncio.timeout(3):
        while True:
            await handler.reconcile_once()
            task = await handler.on_get_task(GetTaskRequest(id=task_id), ServerCallContext())
            if task.status.state == TaskState.TASK_STATE_COMPLETED:
                return extract_artifact_data(task)[0]
            await asyncio.sleep(0.02)


@pytest.mark.parametrize(("decision", "outcome"), [("approved", "approve"), ("rejected", "reject")])
async def test_decided_approval_resumes_the_parked_controller_task(tmp_path, w2, decision, outcome):
    async with running(tmp_path) as (handler, agent):
        task, approval_id = await park_for_approval(handler, agent, w2)
        assert agent.runs == 1
        w2.approvals = [approval_row(approval_id, task.id, decision, code_ok=True)]
        result = await settled(handler, task.id)
        assert result["outcome"] == outcome
        assert result["findings"]["approval_id"] == approval_id
        assert result["findings"]["approved"] is (decision == "approved")
        assert agent.runs == 2
        # The decision is requested once; the resumed run re-reads it instead.
        assert [reducer for reducer, _ in w2.writes].count("request_approval") == 1
        for _ in range(3):
            await handler.reconcile_once()
        assert agent.runs == 2


@pytest.mark.parametrize("pending", ["undecided", "unverified_code", "other_case", "outage", "missing"])
async def test_undecided_or_unverifiable_approval_keeps_the_task_waiting(tmp_path, w2, pending):
    async with running(tmp_path) as (handler, agent):
        task, approval_id = await park_for_approval(handler, agent, w2)
        w2.approvals = {
            "undecided": [approval_row(approval_id, task.id)],
            "unverified_code": [approval_row(approval_id, task.id, "approved", code_ok=False)],
            "other_case": [approval_row(approval_id, task.id, "approved", code_ok=True, case_id="CASE-OTHER")],
            "outage": [approval_row(approval_id, task.id, "approved", code_ok=True)],
            "missing": [],
        }[pending]
        w2.outage = pending == "outage"
        reads = w2.reads
        for _ in range(3):
            await handler.reconcile_once()
        assert w2.reads > reads
        row = await released(agent, task.id)
        assert (row["status"], row["wait_kind"]) == ("waiting", "approval")
        assert not await agent.store.pending_inputs(task.id)
        assert agent.runs == 1
        state = (await handler.on_get_task(GetTaskRequest(id=task.id), ServerCallContext())).status.state
        assert state == TaskState.TASK_STATE_INPUT_REQUIRED


async def test_approval_waits_are_not_polled_without_spacetime(tmp_path, w2, monkeypatch):
    async with running(tmp_path) as (handler, agent):
        task, approval_id = await park_for_approval(handler, agent, w2)
        w2.approvals = [approval_row(approval_id, task.id, "approved", code_ok=True)]
        monkeypatch.setenv("SPACETIME_DISABLED", "1")
        reads = w2.reads
        await handler.reconcile_once()
        assert w2.reads == reads
        assert (await agent.store.get(task.id))["status"] == "waiting"


async def test_live_approval_without_designated_human_fails_instead_of_parking(w2, monkeypatch):
    monkeypatch.delenv("HUMAN_APPROVER_ID")
    agent = ControllerAgent(store=RuntimeStore("sqlite+aiosqlite:///:memory:"))
    env = TaskEnvelope(task_type="review_hold", case_id=CASE)
    try:
        with pytest.raises(HumanApproverNotConfigured, match="HUMAN_APPROVER_ID"):
            await agent.approval(env, "task", {"invoice_id": "INV"}, "propose_payment", TARGET, Decimal(9400))
    finally:
        await agent.store.close()
    assert not w2.writes


def test_live_controller_readiness_requires_human_approver(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("HUMAN_APPROVER_ID", " ")
    with pytest.raises(ValueError, match="live mode requires HUMAN_APPROVER_ID"):
        _readiness(SimpleNamespace(name="controller", live_required_env=ControllerAgent.live_required_env))
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    _readiness(SimpleNamespace(name="controller", live_required_env=ControllerAgent.live_required_env))
