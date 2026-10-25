"""Billing and carrier-cost journals: the shipper invoice books revenue and AR when Shipper AR reconciles it,
and a proposed carrier bill books expense and AP. Each is requested once and posted by Settlement Approvals."""
from decimal import Decimal

import pytest

from agents.carrier_ap.main import CarrierAPAgent
from agents.controller.main import ControllerAgent
from agents.shipper_ar.main import ShipperArAgent
from agents.validator.tools import ISSUE, deterministic_review
from common import agent_support
from common.agent_support import (
    billing_journal_id,
    billing_lines,
    identifier,
    journal_lines,
    money,
    payable_journal_id,
    payable_lines,
)
from common.config import MAX_FANOUT
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.runtime import DelegationRefused
from common.runtime_store import RuntimeStore
from common.source_repository import SourceUnavailable

CASE = "CASE-BOOKS"
REFS = ["source:invoice"]


@pytest.fixture(autouse=True)
def fixture_environment(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    monkeypatch.delenv("SOURCE_DATA_PATH", raising=False)


def case(**extra):
    base = {"case_id": CASE, "invoice_id": "INV-B", "load_id": "LOAD-B", "carrier_id": "C-B", "shipper_id": "S-B",
            "period": "2026-10", "invoice_amount": 1275.50, "contract_rate": 1275.50, "paid_amount": 0,
            "shipper_invoice_id": "AR-B", "shipper_invoice_amount": 1600,
            "line_items": [{"kind": "linehaul", "amount": 1275.50}], "evidence_refs": REFS,
            "documents": [{"kind": "rate_confirmation", "ref": "source:rate", "load_id": "LOAD-B"},
                          {"kind": "pod", "ref": "source:pod", "load_id": "LOAD-B", "signed": True}]}
    base.update(extra)
    return base


def deposit(index, amount):
    return {"id": f"D-{index}", "invoice_id": "AR-B", "shipper_id": "S-B", "amount": amount}


def deposit_id(index):
    return identifier(f"S-B:D-{index}", "receivable_journal_request")


def stored(request_id, lines, requested_by="shipper_ar", **extra):
    return {"id": request_id, "case_id": CASE, "requested_by": requested_by, "period": "2026-10",
            "lines": lines, **extra}


def cash_lines(amount):
    return [{"account": "cash", "debit": amount, "credit": 0},
            {"account": "accounts_receivable", "debit": 0, "credit": amount}]


class Delegations:
    """Records every delegation the way the runtime would see it, and answers like Settlement Approvals."""

    def __init__(self, refuse: set[str] = frozenset()):
        self.calls, self.refuse = [], refuse

    async def __call__(self, skill, env, meta, task_id, *args, invocation_key=None, **kwargs):
        self.calls.append((skill, env.subject.get("request_ids"), invocation_key))
        if skill in self.refuse:
            raise DelegationRefused("max delegation fanout")
        if skill == "approve_entry":
            return [{"outcome": "approve", "findings": {"entries": [
                {"request_id": request_id, "outcome": "approve"} for request_id in env.subject["request_ids"]]}}]
        if skill == "review_hold":
            return [{"outcome": "approve", "findings": {"approval_id": "APR-1"}}]
        return [{"outcome": "verified", "findings": {}}]


@pytest.fixture
def recorded_writes(monkeypatch):
    """A live-like write path: every mutation executes, as with SpacetimeDB enabled."""
    import agents.carrier_ap.main as carrier
    writes = []

    async def mutate(agent, env, task_id, operation, payload):
        writes.append((operation, payload))
        return {"executed": True, "simulated": False, "action": operation, "write_id": payload["id"]}
    monkeypatch.setattr(agent_support, "mutate", mutate)
    monkeypatch.setattr(carrier, "mutate", mutate)
    return writes


async def run(agent_class, skill, source, delegate=None):
    store = RuntimeStore("sqlite+aiosqlite:///:memory:")
    agent = agent_class(store=store)
    if delegate is not None:
        agent.delegate = delegate
    env = TaskEnvelope(task_type=skill, case_id=source["case_id"], subject=source)
    try:
        return await agent.handle(env, DelegationMeta(case_id=env.case_id, from_agent="router"), "task-books", None)
    finally:
        await store.close()


def review(source, artifact, agent, skill):
    return deterministic_review({"target": {"task_id": "task-books", "agent": agent, "skill": skill,
                                            "case_id": CASE, "a2a_state": "completed"},
                                 "source": source, "artifact": artifact, "history": [], "flag": None,
                                 "simulated": True})


def failed(result, severity=ISSUE):
    return {x["name"] for x in result.checks if not x["ok"] and x["severity"] == severity}


def account_balance(payloads, account):
    """Debit-normal balance of one account across journal payloads."""
    return sum((money(line["debit"]) - money(line["credit"]) for payload in payloads
                for line in journal_lines(payload["lines"]) if line["account"] == account), Decimal(0))


# Shipper AR: customer billing ------------------------------------------------------------------------------


async def test_reconcile_requests_billing_then_deposits_and_posts_them_in_one_batch(recorded_writes):
    delegations = Delegations()
    source = case(deposits=[deposit(1, 600), deposit(2, 400)])
    result = await run(ShipperArAgent, "reconcile_customer_payment", source, delegations)
    requested = [payload for operation, payload in recorded_writes if operation == "request_journal_entry"]
    assert [payload["id"] for payload in requested] == [billing_journal_id("AR-B"), deposit_id(1), deposit_id(2)]
    assert requested[0] == {"id": billing_journal_id("AR-B"), "period": "2026-10", "memo": "Customer billing: AR-B",
                            "lines": [{"account": "accounts_receivable", "debit": 1600, "credit": 0},
                                      {"account": "freight_revenue", "debit": 0, "credit": 1600}]}
    # One Settlement Approvals batch, billing first, beside the cash impact: two of the three fan-out slots.
    assert [(skill, ids) for skill, ids, _ in delegations.calls] == [
        ("approve_entry", [billing_journal_id("AR-B"), deposit_id(1), deposit_id(2)]),
        ("assess_financial_impact", None)]
    assert len(delegations.calls) <= MAX_FANOUT
    # A short pay leaves the rest of the billing receivable, and the revenue is the whole invoice.
    assert result.outcome == "partial"
    assert account_balance(requested, "accounts_receivable") == Decimal(600) > 0
    assert account_balance(requested, "freight_revenue") == Decimal(-1600)
    assert account_balance(requested, "accounts_receivable") == money(result.findings["balance"])


async def test_reconcile_without_cash_still_bills_the_invoice_once(recorded_writes):
    delegations = Delegations()
    result = await run(ShipperArAgent, "reconcile_customer_payment", case(deposits=[]), delegations)
    assert [payload["id"] for _, payload in recorded_writes] == [billing_journal_id("AR-B")]
    assert [(skill, ids) for skill, ids, _ in delegations.calls] == [
        ("approve_entry", [billing_journal_id("AR-B")])]
    assert result.outcome == "partial" and result.findings["balance"] == 1600


async def test_repeat_reconcile_never_requests_the_billing_twice(recorded_writes):
    # Recorded but not yet posted: send it for posting again, but never re-request it.
    delegations = Delegations()
    source = case(deposits=[deposit(1, 600)], journal_requests=[
        stored(billing_journal_id("AR-B"), billing_lines(money(1600)))])
    result = await run(ShipperArAgent, "reconcile_customer_payment", source, delegations)
    assert [payload["id"] for _, payload in recorded_writes] == [deposit_id(1)]
    assert delegations.calls[0][:2] == ("approve_entry", [billing_journal_id("AR-B"), deposit_id(1)])
    assert result.findings["journal_requests"][0] == {"executed": False, "existing": True, "simulated": True,
                                                      "request_id": billing_journal_id("AR-B")}
    # Already posted: count it as posted, request nothing, and post only the new cash.
    recorded_writes.clear()
    delegations = Delegations()
    source["journal_requests"] = [stored(billing_journal_id("AR-B"), billing_lines(money(1600)),
                                         posted_entry_id="ENTRY-BILL")]
    source["deposits"].append(deposit(2, 400))
    result = await run(ShipperArAgent, "reconcile_customer_payment", source, delegations)
    assert [payload["id"] for _, payload in recorded_writes] == [deposit_id(1), deposit_id(2)]
    assert delegations.calls[0][:2] == ("approve_entry", [deposit_id(1), deposit_id(2)])
    assert result.findings["controller"][0] == "ENTRY-BILL"


async def test_a_billing_held_by_another_case_is_refused(recorded_writes):
    source = case(deposits=[], journal_requests=[{**stored(billing_journal_id("AR-B"), billing_lines(money(1600))),
                                                  "case_id": "CASE-ELSEWHERE"}])
    with pytest.raises(SourceUnavailable, match="billing journal belongs to a different case"):
        await run(ShipperArAgent, "reconcile_customer_payment", source, Delegations())
    assert recorded_writes == []


async def test_live_lookup_finds_a_billing_by_id_as_well_as_by_case(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    queries = []

    async def rows(agent, table, field, value):
        queries.append((table, field, value))
        return [] if field == "case_id" else [{"id": value, "case_id": CASE, "posted_entry_id": "ENTRY-BILL"}]
    monkeypatch.setattr(agent_support, "rows", rows)
    env = TaskEnvelope(task_type="reconcile_customer_payment", case_id=CASE)
    found = await agent_support.journal_requests("shipper_ar", env, {}, billing_journal_id("AR-B"))
    assert queries == [("journal_request", "case_id", CASE), ("journal_request", "id", billing_journal_id("AR-B"))]
    assert found[0]["posted_entry_id"] == "ENTRY-BILL"


async def test_fixture_reconcile_proposes_the_billing_and_the_validator_accepts_it():
    source = case(deposits=[deposit(1, 600)])
    result = await run(ShipperArAgent, "reconcile_customer_payment", source, Delegations())
    receipt = result.findings["journal_requests"][0]
    assert receipt["executed"] is False and receipt["proposed_payload"]["id"] == billing_journal_id("AR-B")
    checked = review(source, result.model_dump(mode="json"), "shipper_ar", "reconcile_customer_payment")
    assert checked.verdict == "pass", failed(checked)
    assert {"billing_journal_requested", "billing_journal_balanced", "billing_journal_accounts",
            "receivable_journal_requested"} <= {x["name"] for x in checked.checks if x["ok"]}


def test_validator_requires_the_billing_once_at_the_invoice_amount():
    source = case(deposits=[])
    artifact = ResultArtifact(outcome="partial", evidence_refs=REFS, findings={
        "matched_amount": 0, "balance": 1600, "matched_deposit_ids": [], "journal_requests": []}).model_dump()
    assert "billing_journal_requested" in failed(review(source, artifact, "shipper_ar", "reconcile_customer_payment"))
    artifact["findings"]["journal_requests"] = [{"executed": False, "simulated": True, "proposed_payload": {
        "id": billing_journal_id("AR-B"), "lines": billing_lines(money(1500))}}]
    assert "billing_journal_balanced" in failed(review(source, artifact, "shipper_ar", "reconcile_customer_payment"))
    artifact["findings"]["journal_requests"][0]["proposed_payload"]["lines"] = cash_lines(1600)
    assert "billing_journal_accounts" in failed(review(source, artifact, "shipper_ar", "reconcile_customer_payment"))
    # Posted by an earlier reconciliation: the stored row satisfies the check.
    artifact["findings"]["journal_requests"] = []
    source["journal_requests"] = [stored(billing_journal_id("AR-B"), billing_lines(money(1600)),
                                         posted_entry_id="ENTRY-BILL")]
    assert review(source, artifact, "shipper_ar", "reconcile_customer_payment").verdict == "pass"


# Carrier AP: carrier settlement accrual --------------------------------------------------------------------


async def test_proposed_payment_books_the_payable_for_the_proposed_amount(recorded_writes):
    delegations = Delegations()
    source = case(paid_amount=275.50)
    result = await run(CarrierAPAgent, "process_carrier_invoice", source, delegations)
    assert result.outcome == "approve"
    assert [operation for operation, _ in recorded_writes] == ["propose_payment", "request_journal_entry"]
    proposal, journal = (payload for _, payload in recorded_writes)
    assert journal == {"id": payable_journal_id("INV-B"), "period": "2026-10", "memo": "Carrier settlement: INV-B",
                       "lines": [{"account": "freight_expense", "debit": 1000, "credit": 0},
                                 {"account": "accounts_payable", "debit": 0, "credit": 1000}]}
    assert proposal["amount"] == 1000
    assert delegations.calls == [("approve_entry", [payable_journal_id("INV-B")], "post_payable_journal")]
    assert result.findings["payable_journal"]["request_id"] == payable_journal_id("INV-B")
    # No payment executes, so the payable stays open.
    assert account_balance([journal], "accounts_payable") == Decimal(-1000)
    assert all(line["account"] != "cash" for line in journal["lines"])


async def test_payable_period_falls_back_to_the_invoice_date(recorded_writes):
    source = case(period=None, issued_at="2026-09-30T23:00:00+00:00")
    await run(CarrierAPAgent, "process_carrier_invoice", source, Delegations())
    assert recorded_writes[-1][1]["period"] == "2026-09"


async def test_payable_is_requested_once_and_posted_once(recorded_writes):
    lines = payable_lines(money(1275.50))
    delegations = Delegations()
    source = case(journal_requests=[stored(payable_journal_id("INV-B"), lines, "carrier_ap")])
    result = await run(CarrierAPAgent, "process_carrier_invoice", source, delegations)
    assert [operation for operation, _ in recorded_writes] == ["propose_payment"]
    assert delegations.calls == [("approve_entry", [payable_journal_id("INV-B")], "post_payable_journal")]
    recorded_writes.clear()
    delegations = Delegations()
    source["journal_requests"][0]["posted_entry_id"] = "ENTRY-AP"
    result = await run(CarrierAPAgent, "process_carrier_invoice", source, delegations)
    assert [operation for operation, _ in recorded_writes] == ["propose_payment"]
    assert delegations.calls == []
    assert result.findings["journal_posting"] == ["ENTRY-AP"] and result.findings["journal_requests"] == []


async def test_payable_posting_is_the_third_delegation_at_most(recorded_writes):
    # The longest settlement path: chase the packet, get a human approval above the threshold, then post.
    delegations = Delegations()
    source = case(invoice_amount=6400, contract_rate=6400, line_items=[{"kind": "linehaul", "amount": 6400}])
    source["documents"] = source["documents"][:1]
    result = await run(CarrierAPAgent, "process_carrier_invoice", source, delegations)
    assert result.outcome == "approve"
    assert [skill for skill, _, _ in delegations.calls] == ["collect_documents", "review_hold", "approve_entry"]
    assert len(delegations.calls) == MAX_FANOUT


async def test_a_refused_posting_leaves_the_payable_pending_without_failing_settlement(recorded_writes):
    result = await run(CarrierAPAgent, "process_carrier_invoice", case(), Delegations(refuse={"approve_entry"}))
    assert result.outcome == "approve"
    assert result.findings["payable_journal"]["posting_deferred"] == "max delegation fanout"
    assert [operation for operation, _ in recorded_writes] == ["propose_payment", "request_journal_entry"]


async def test_a_refused_payable_journal_does_not_fail_settlement(monkeypatch):
    # The proposal is stored first; a journal the reducer refuses (no open period for the invoice month) must
    # leave settlement complete with the bill reported unbooked, not strand the proposal behind a failed task.
    import agents.carrier_ap.main as carrier
    from common.spacetime_client import REDUCER_REFUSED_STATUS, SpacetimeHTTPError
    writes = []

    async def mutate(agent, env, task_id, operation, payload):
        writes.append(operation)
        if operation == "request_journal_entry":
            error = SpacetimeHTTPError(REDUCER_REFUSED_STATUS, mutation=True)
            error.reason = "P-006: accounting period is missing or locked"
            raise error
        return {"executed": True, "simulated": False, "action": operation, "write_id": payload["id"]}
    monkeypatch.setattr(agent_support, "mutate", mutate)
    monkeypatch.setattr(carrier, "mutate", mutate)
    delegations = Delegations()
    result = await run(CarrierAPAgent, "process_carrier_invoice", case(period="2026-09"), delegations)
    assert result.outcome == "approve"
    assert writes == ["propose_payment", "request_journal_entry"]
    assert result.findings["payable_journal"] == {
        "request_id": payable_journal_id("INV-B"), "requested": False, "amount": 1275.5, "period": "2026-09",
        "reason": "P-006: accounting period is missing or locked"}
    assert delegations.calls == []
    # Any other failure still fails the task.
    writes.clear()

    async def broken(agent, env, task_id, operation, payload):
        if operation == "request_journal_entry":
            raise SpacetimeHTTPError(500, mutation=True)
        return {"executed": True, "simulated": False, "action": operation, "write_id": payload["id"]}
    monkeypatch.setattr(agent_support, "mutate", broken)
    monkeypatch.setattr(carrier, "mutate", broken)
    with pytest.raises(SpacetimeHTTPError):
        await run(CarrierAPAgent, "process_carrier_invoice", case(), Delegations())


async def test_no_payable_without_an_allowed_proposal():
    # Fixture policy returns needs_approval above the threshold: nothing was proposed, so nothing is booked.
    source = case(invoice_amount=6400, contract_rate=6400, line_items=[{"kind": "linehaul", "amount": 6400}])
    result = await run(CarrierAPAgent, "process_carrier_invoice", source, Delegations())
    assert result.outcome == "unverified"
    assert "payable_journal" not in result.findings


async def test_fixture_settlement_proposes_the_payable_and_the_validator_accepts_it():
    source = case()
    result = await run(CarrierAPAgent, "process_carrier_invoice", source)
    receipt = result.findings["journal_requests"][0]
    assert receipt["executed"] is False and receipt["proposed_payload"]["id"] == payable_journal_id("INV-B")
    checked = review(source, result.model_dump(mode="json"), "carrier_ap", "process_carrier_invoice")
    assert checked.verdict == "pass", failed(checked)
    assert {"payable_journal_requested", "payable_journal_balanced",
            "payable_journal_accounts"} <= {x["name"] for x in checked.checks if x["ok"]}
    artifact = result.model_dump(mode="json")
    artifact["findings"]["journal_requests"] = []
    assert "payable_journal_requested" in failed(review(source, artifact, "carrier_ap", "process_carrier_invoice"))
    artifact["findings"]["journal_requests"] = [receipt]
    receipt["proposed_payload"]["lines"] = payable_lines(money(1000))
    assert "payable_journal_balanced" in failed(review(source, artifact, "carrier_ap", "process_carrier_invoice"))


# Settlement Approvals posts every kind of request in its own case ----------------------------------------------


async def test_controller_posts_billing_cash_and_payable_requests_and_the_validator_accepts_them():
    source = case(request_ids=[billing_journal_id("AR-B"), deposit_id(1), payable_journal_id("INV-B")],
                  journal_requests=[stored(billing_journal_id("AR-B"), billing_lines(money(1600))),
                                    stored(deposit_id(1), cash_lines(600)),
                                    stored(payable_journal_id("INV-B"), payable_lines(money(1275.50)), "carrier_ap")])
    result = await run(ControllerAgent, "approve_entry", source)
    assert [(entry["request_id"], entry["outcome"]) for entry in result.findings["entries"]] == [
        (billing_journal_id("AR-B"), "approve"), (deposit_id(1), "approve"), (payable_journal_id("INV-B"), "approve")]
    checked = review(source, result.model_dump(mode="json"), "controller", "approve_entry")
    assert checked.verdict == "pass", failed(checked)
    # A request from another case is never posted here.
    source["journal_requests"][0]["case_id"] = "CASE-ELSEWHERE"
    with pytest.raises(SourceUnavailable):
        await run(ControllerAgent, "approve_entry", source)


# Live mode: the tool loop completes through execute_current_skill, which runs the same workflow ------------


@pytest.mark.parametrize(("agent_class", "skill", "expected"), [
    (ShipperArAgent, "reconcile_customer_payment", [billing_journal_id("AR-B"), deposit_id(1)]),
    (CarrierAPAgent, "process_carrier_invoice", [payable_journal_id("INV-B")]),
])
async def test_the_live_tool_path_books_the_same_journals(recorded_writes, agent_class, skill, expected):
    from common.tool_registry import ToolContext, registry_for
    store = RuntimeStore("sqlite+aiosqlite:///:memory:")
    agent = agent_class(store=store)
    delegations = Delegations()
    agent.delegate = delegations
    env = TaskEnvelope(task_type=skill, case_id=CASE, subject=case(deposits=[deposit(1, 600)]))
    ctx = ToolContext(agent, env, DelegationMeta(case_id=CASE, from_agent="router"), "task-books")
    try:
        artifact = (await registry_for(ctx)["execute_current_skill"].handler(ctx, {}))["artifact"]
    finally:
        await store.close()
    requested = [payload["id"] for operation, payload in recorded_writes if operation == "request_journal_entry"]
    assert requested == expected
    assert delegations.calls[0][:2] == ("approve_entry", expected)
    assert artifact["outcome"] in {"partial", "approve"}
