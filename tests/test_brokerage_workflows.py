"""Financial/domain checks use varied cases, never fixed hero-case branches."""
import json
from pathlib import Path

import pytest

from agents.carrier_ap.main import CarrierAPAgent
from agents.carrier_ap.tools import duplicate_invoices, three_way_match
from agents.cfo.main import CfoAgent
from agents.controller.main import ControllerAgent
from agents.documents.main import DocumentsAgent
from agents.documents.tools import check_packet
from agents.risk.tools import audit_charges
from agents.shipper_ar.main import ShipperArAgent
from common.agent_support import identifier, money
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import TaskParked
from common.runtime_store import RuntimeStore
from common.source_repository import SourceRepository, SourceUnavailable


@pytest.fixture(autouse=True)
def fixture_environment(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    monkeypatch.delenv("SOURCE_DATA_PATH", raising=False)


def invoice(amount=1275.50, contract=1275.50):
    return {"case_id": "CASE-OTHER", "invoice_id": "INV-OTHER", "load_id": "LOAD-OTHER",
            "carrier_id": "C-OTHER", "shipper_id": "S-OTHER", "period": "2026-10",
            "invoice_amount": amount, "contract_rate": contract, "shipper_invoice_amount": 1600,
            "line_items": [{"kind": "linehaul", "amount": amount}],
            "evidence_refs": ["source:invoice"],
            "documents": [{"kind": "rate_confirmation", "ref": "source:rate", "load_id": "LOAD-OTHER"},
                          {"kind": "pod", "ref": "source:pod", "load_id": "LOAD-OTHER", "signed": True}]}


@pytest.mark.parametrize(("amount", "contract", "exceeds"), [
    (100, 100, False), (102, 100, False), (102.01, 100, True), (4800, 5000, False), (9999.99, 8000, True),
])
def test_invoice_tolerance_uses_this_load(amount, contract, exceeds):
    result = three_way_match(invoice(amount, contract))
    assert result["above_tolerance"] is exceeds
    assert result["matched"] is not exceeds


def test_discount_lines_count_toward_the_invoice_total():
    source = invoice(1869.44, 1907.59)
    source["line_items"] = [{"kind": "linehaul", "amount": 1850}, {"kind": "fuel_surcharge", "amount": 57.59},
                            {"kind": "quickpay_discount", "amount": -38.15}]
    result = three_way_match(source)
    assert result["totals_match"] is True and result["matched"] is True
    source["invoice_amount"] = -1
    with pytest.raises(SourceUnavailable):
        three_way_match(source)


@pytest.mark.parametrize("value", [True, None, "NaN", "Infinity", -1, 1.001])
def test_money_rejects_invalid_or_fractional_cents(value):
    with pytest.raises(SourceUnavailable):
        money(value)


def test_duplicate_window_and_same_delivery_not_a_new_invoice():
    source = invoice()
    source.update(issued_at="2026-10-24T00:00:00+00:00", previous_invoices=[
        {"id": "older-copy", "carrier_id": "C-OTHER", "load_id": "LOAD-OTHER",
         "amount": 1275.50, "issued_at": "2026-10-11T00:00:00+00:00"},
        {"id": "outside-window", "carrier_id": "C-OTHER", "load_id": "LOAD-OTHER",
         "amount": 1275.50, "issued_at": "2026-10-01T00:00:00+00:00"},
        {"id": "INV-OTHER", "carrier_id": "C-OTHER", "load_id": "LOAD-OTHER",
         "amount": 1275.50, "issued_at": "2026-10-24T00:00:00+00:00"},
    ])
    assert duplicate_invoices(source) == ["older-copy"]


def test_document_packet_rejects_unsigned_and_wrong_load_pods():
    source = invoice()
    source["documents"][1]["signed"] = False
    assert check_packet(source)["missing"] == ["pod"]
    assert three_way_match(source)["missing_documents"] == ["pod"]
    source["documents"][1].update(signed=True, load_id="ANOTHER-LOAD")
    assert check_packet(source)["missing"] == ["pod"]
    assert three_way_match(source)["missing_documents"] == ["pod"]


def test_accessorial_uses_recorded_terms_and_receipts():
    source = invoice(1450, 1200)
    source.update(gps_dwell_min=70, free_time_min=90, detention_hours_claimed=2,
                  line_items=[{"kind": "linehaul", "amount": 1200},
                              {"kind": "detention", "amount": 250}])
    result = audit_charges(source)
    assert result["needs_verification"] is True
    assert result["unsupported_amount"] == 250
    source["free_time_min"] = 30
    assert audit_charges(source)["needs_verification"] is False


async def test_live_source_ignores_forged_financial_payload(tmp_path, monkeypatch):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"cases": {"CASE-OTHER": invoice()}}))
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    env = TaskEnvelope(task_type="process_carrier_invoice", case_id="CASE-OTHER",
                       subject={"invoice_amount": 1, "contract_rate": 1})
    result = await SourceRepository().load(env)
    assert result["invoice_amount"] == 1275.50
    assert result["simulated"] is False
    env.case_id = "CASE-MISSING"
    with pytest.raises(SourceUnavailable):
        await SourceRepository().load(env)


async def test_live_source_requires_explicit_dataset(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    with pytest.raises(SourceUnavailable):
        await SourceRepository().load(TaskEnvelope(task_type="x", case_id="x", subject=invoice()))


async def test_source_directory_rejects_unverified_contact():
    repo = SourceRepository({"companies": {"C": {"phone": "+15555550100", "source": "fixture"}},
                             "contacts": {"C": [{"phone": "+15555550101", "verified": False}]}})
    with pytest.raises(SourceUnavailable):
        await repo.get_contact("C")


async def run_handler(agent_class, skill, source, *, delegate=None):
    store = RuntimeStore("sqlite+aiosqlite:///:memory:")
    agent = agent_class(store=store)
    if delegate:
        agent.delegate = delegate
    env = TaskEnvelope(task_type=skill, case_id=source["case_id"], subject=source)
    try:
        return await agent.handle(env, DelegationMeta(case_id=env.case_id, from_agent="router"), "task-test", None)
    finally:
        await store.close()


async def test_clean_invoice_prepares_explicit_fixture_proposal():
    result = await run_handler(CarrierAPAgent, "process_carrier_invoice", invoice())
    assert result.outcome == "approve"
    proposal = result.findings["payment_proposal"]
    assert proposal["executed"] is False and proposal["simulated"] is True
    assert proposal["proposed_payload"]["amount"] == 1275.50
    assert result.model_used == "fixture/deterministic"


async def test_held_invoice_never_proposes_partial_payment():
    source = invoice()
    source["holds"] = [{"id": "HOLD", "status": "active", "amount": 5}]
    result = await run_handler(CarrierAPAgent, "propose_payment", source)
    assert result.outcome == "hold"
    assert result.findings["payment_blocked"] is True
    assert "payment_proposal" not in result.findings


async def test_bank_change_blocks_otherwise_clean_settlement():
    source = invoice()
    source["bank_change_requested"] = True
    calls = []
    async def delegate(skill, *args, **kwargs):
        calls.append(skill)
        return [{"outcome": "hold" if skill == "review_hold" else "suspicious", "findings": {}}]
    result = await run_handler(CarrierAPAgent, "process_carrier_invoice", source, delegate=delegate)
    assert result.outcome == "hold"
    assert calls == ["screen_bank_change", "review_hold"]
    assert "P-004" in result.policy_ids
    assert "payment_proposal" not in result.findings


async def test_financial_forecast_keeps_held_obligations():
    source = invoice(1400, 1200)
    source.update(holds=[{"amount": 200, "status": "active"}], shipper_invoice_amount=1800,
                  shipper_paid_amount=300, paid_amount=100)
    result = await run_handler(CfoAgent, "cash_forecast", source)
    assert result.findings["invoiced_margin"] == 400
    assert result.findings["net_cash"] == 200
    assert result.findings["outstanding_carrier_cost"] == 1300
    forecasts = [entry["proposed_payload"] for entry in result.findings["forecasts"]]
    assert sum(entry["amount"] for entry in forecasts) == result.findings["net_cash"]


async def test_forecast_includes_matching_deposits_when_paid_summary_absent():
    source = invoice()
    source.update(shipper_invoice_id="AR-1", deposits=[{
        "id": "D-1", "invoice_id": "AR-1", "shipper_id": "S-OTHER", "amount": 600}])
    result = await run_handler(CfoAgent, "cash_forecast", source)
    assert result.findings["expected_collections"] == 1000


async def test_controller_recomputes_hold_and_calls_profitability():
    source = invoice(2400, 2000)
    calls = []
    async def delegate(skill, *args, **kwargs):
        calls.append(skill)
        return [{"outcome": "verified", "findings": {"net_cash": 100}}]
    result = await run_handler(ControllerAgent, "review_hold", source, delegate=delegate)
    assert result.findings["hold_amount"] == 400
    assert result.findings["hold"]["executed"] is False
    assert calls == ["assess_financial_impact"]


async def test_documents_without_contact_park_instead_of_inventing_pod():
    source = invoice()
    source["documents"] = source["documents"][:1]
    with pytest.raises(TaskParked) as parked:
        await run_handler(DocumentsAgent, "collect_documents", source)
    assert parked.value.business_status == "awaiting_external_verification"


async def test_collections_counts_only_matching_unique_deposits():
    source = invoice()
    source.update(shipper_invoice_id="AR-1", deposits=[
        {"id": "D-1", "invoice_id": "AR-1", "shipper_id": "S-OTHER", "amount": 600},
        {"id": "D-2", "invoice_id": "AR-OTHER", "shipper_id": "S-OTHER", "amount": 999},
    ])
    async def delegate(*args, **kwargs):
        return [{"outcome": "verified", "findings": {}}]
    result = await run_handler(ShipperArAgent, "reconcile_customer_payment", source, delegate=delegate)
    assert result.outcome == "partial"
    assert result.findings["matched_amount"] == 600
    assert result.findings["balance"] == 1000
    assert result.findings["unmatched_deposit_ids"] == ["D-2"]


async def test_repeated_collection_does_not_request_a_second_posted_deposit():
    source = invoice()
    source.update(shipper_invoice_id="AR-1", deposits=[{
        "id": "D-1", "invoice_id": "AR-1", "shipper_id": "S-OTHER", "amount": 600}],
        journal_requests=[{"id": identifier("S-OTHER:D-1", "receivable_journal_request"),
                           "case_id": source["case_id"], "posted_entry_id": "ENTRY-1"}])
    calls = []
    async def delegate(skill, *args, **kwargs):
        calls.append(skill)
        return [{"outcome": "verified", "findings": {}}]
    result = await run_handler(ShipperArAgent, "reconcile_customer_payment", source, delegate=delegate)
    assert result.findings["journal_requests"] == []
    assert result.findings["controller"] == ["ENTRY-1"]
    assert calls == ["assess_financial_impact"]



def journal_request(request_id, requested_by="shipper_ar", **extra):
    return {"id": request_id, "case_id": "CASE-OTHER", "requested_by": requested_by, "period": "2026-10",
            "lines": [{"account": "cash", "debit": 300, "credit": 0},
                      {"account": "accounts_receivable", "debit": 0, "credit": 300}], **extra}


async def test_controller_posts_a_batch_and_keeps_separation_of_duties_per_request():
    source = invoice()
    source.update(request_ids=["JR-1", "JR-2", "JR-3"], journal_requests=[
        journal_request("JR-1"), journal_request("JR-2", "controller"),
        journal_request("JR-3", posted_entry_id="ENTRY-3")])
    result = await run_handler(ControllerAgent, "approve_entry", source)
    assert [(entry["request_id"], entry["outcome"]) for entry in result.findings["entries"]] == [
        ("JR-1", "approve"), ("JR-2", "reject"), ("JR-3", "verified")]
    assert result.outcome == "partial"
    assert result.policy_ids == ["P-005", "P-006"]


async def test_controller_accepts_a_single_request_reference():
    source = invoice()
    source.update(request_id="JR-1", journal_request=journal_request("JR-1"))
    result = await run_handler(ControllerAgent, "approve_entry", source)
    assert result.outcome == "approve"
    assert [entry["request_id"] for entry in result.findings["entries"]] == ["JR-1"]


@pytest.mark.parametrize("request_ids", [["JR-1", "JR-MISSING"], ["JR-1", "JR-1"], [], "JR-1"])
async def test_controller_rejects_an_unresolvable_batch_before_posting(request_ids, monkeypatch):
    import agents.controller.main as controller
    posted = []
    async def mutate(*args):
        posted.append(args)
        return {"decision": "allow"}
    monkeypatch.setattr(controller, "mutate", mutate)
    source = invoice()
    source.update(request_ids=request_ids, journal_requests=[journal_request("JR-1")])
    with pytest.raises(SourceUnavailable):
        await run_handler(ControllerAgent, "approve_entry", source)
    assert posted == []


async def test_reconciliation_sends_all_deposit_requests_to_one_approval():
    source = invoice()
    deposits = [{"id": f"D-{i}", "invoice_id": "AR-1", "shipper_id": "S-OTHER", "amount": 300} for i in range(4)]
    source.update(shipper_invoice_id="AR-1", deposits=deposits, journal_requests=[
        journal_request(identifier(f"S-OTHER:D-{i}", "receivable_journal_request")) for i in range(4)])
    calls = []
    async def delegate(skill, env, *args, **kwargs):
        calls.append((skill, env.subject.get("request_ids")))
        return [{"outcome": "approve", "findings": {}}]
    result = await run_handler(ShipperArAgent, "reconcile_customer_payment", source, delegate=delegate)
    assert result.findings["matched_amount"] == 1200
    assert calls == [("approve_entry", [request["id"] for request in source["journal_requests"]]),
                     ("assess_financial_impact", None)]

def test_fixture_source_contains_multiple_financial_scenarios():
    data = json.loads(Path("sim/fixtures/brokerage_source.json").read_text())
    assert len(data["cases"]) >= 3
    assert len({case["invoice_amount"] for case in data["cases"].values()}) > 1


async def test_invoice_from_a_different_carrier_is_held_not_paid():
    source = invoice()
    source.update(carrier_identity_mismatch=True, carrier_identity={
        "tendered_usdot": "1111111", "invoice_usdot": "2222222", "flags": ["usdot_mismatch"]})
    result = await run_handler(CarrierAPAgent, "process_carrier_invoice", source)
    assert result.outcome == "hold" and result.findings["payment_blocked"] is True
    assert "payment_proposal" not in result.findings and "P-002" in result.policy_ids
