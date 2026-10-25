"""Agents and the Validator show their work: every reported number carries a re-runnable formula."""
import json

import pytest

from agents.carrier_ap.main import CarrierAPAgent
from agents.cfo.main import CfoAgent
from agents.controller.main import ControllerAgent
from agents.shipper_ar.main import ShipperArAgent
from agents.validator.tools import deterministic_review
from common.workings import UNITS, document_ref, refs_for, working
from tests.test_brokerage_workflows import fixture_environment, invoice, run_handler  # noqa: F401

WORKING_KEYS = {"label", "formula", "inputs", "result", "unit", "evidence_refs", "basis"}


def assert_workings(items, source):
    assert items, "no workings reported"
    json.dumps(items)  # plain JSON only, no Decimals
    for item in items:
        assert set(item) == WORKING_KEYS and item["unit"] in UNITS
        assert item["label"] and item["formula"]
        assert set(item["evidence_refs"]) <= set(source.get("evidence_refs", [])) | {
            d["ref"] for d in source.get("documents", [])}


def test_working_helper_binds_inputs_to_source_documents():
    source = invoice()
    source["evidence_refs"].append("doc:INV-OTHER")
    assert document_ref(source, "rate_confirmation") == "source:rate"
    assert document_ref(source, "carrier_invoice") == "doc:INV-OTHER"
    assert refs_for(source, "invoice_amount", "contract_rate", "unknown_input") == ["doc:INV-OTHER", "source:rate"]
    with pytest.raises(ValueError):
        working("x", "y", {}, 1, unit="furlongs")


async def test_carrier_settlement_shows_match_arithmetic_and_open_balance():
    source = invoice(1300, 1275.50)
    source["line_items"] = [{"kind": "linehaul", "amount": 1275.50}, {"kind": "fuel_surcharge", "amount": 24.50}]
    result = await run_handler(CarrierAPAgent, "process_carrier_invoice", source)
    assert result.outcome == "approve"
    items = {w["label"]: w for w in result.findings["workings"]}
    assert_workings(list(items.values()), source)
    assert items["Invoice vs rate confirmation"]["result"] == 24.5
    assert items["Tolerance ceiling"]["result"] == 1301.01
    assert items["Invoice lines add up"]["inputs"] == {"linehaul": 1275.5, "fuel_surcharge": 24.5}
    assert items["Open balance to pay"]["result"] == 1300


async def test_held_settlement_shows_why_the_charge_is_unsupported():
    source = invoice(1450, 1200)
    source.update(gps_dwell_min=70, free_time_min=90, detention_hours_claimed=2,
                  line_items=[{"kind": "linehaul", "amount": 1200}, {"kind": "detention", "amount": 250}])

    async def delegate(skill, *args, **kwargs):
        return [{"outcome": "hold" if skill == "review_hold" else "suspicious", "findings": {}}]

    result = await run_handler(CarrierAPAgent, "process_carrier_invoice", source, delegate=delegate)
    items = {w["label"]: w for w in result.findings["workings"]}
    assert_workings(list(items.values()), source)
    assert items["Time on site beyond free time"]["result"] == -20
    assert items["Claimed hours within support"]["result"] is False
    assert items["Unsupported detention"]["result"] == 250
    assert items["Unsupported charges total"]["result"] == 250


async def test_controller_cfo_and_ar_show_their_formulas():
    source = invoice(1450, 1200)
    source.update(gps_dwell_min=70, free_time_min=90, detention_hours_claimed=2,
                  line_items=[{"kind": "linehaul", "amount": 1200}, {"kind": "detention", "amount": 250}])

    async def delegate(*args, **kwargs):
        return [{"outcome": "verified", "findings": {}}]

    hold = await run_handler(ControllerAgent, "review_hold", source, delegate=delegate)
    items = {w["label"]: w for w in hold.findings["workings"]}
    assert hold.outcome == "hold" and items["Amount to hold"]["result"] == 250
    assert items["Hold required"]["result"] is True

    cash = invoice()
    cash.update(shipper_invoice_amount=1600, shipper_paid_amount=100, paid_amount=75.5)
    forecast = await run_handler(CfoAgent, "cash_forecast", cash)
    items = {w["label"]: w for w in forecast.findings["workings"]}
    assert items["Net cash on this load"]["result"] == 300
    assert items["Margin as invoiced"]["inputs"] == {"revenue": 1600, "invoiced_carrier_cost": 1275.5}

    receivable = invoice()
    receivable.update(shipper_invoice_id="AR-1", deposits=[
        {"id": "D-1", "invoice_id": "AR-1", "shipper_id": "S-OTHER", "amount": 1000}])
    applied = await run_handler(ShipperArAgent, "reconcile_customer_payment", receivable, delegate=delegate)
    items = {w["label"]: w for w in applied.findings["workings"]}
    assert items["Cash applied to this invoice"]["inputs"] == {"D-1": 1000}
    assert items["Open balance"]["result"] == 600


async def test_validator_checks_carry_their_own_recomputation():
    source = invoice(1300, 1275.50)
    source["line_items"] = [{"kind": "linehaul", "amount": 1300}]
    result = await run_handler(CarrierAPAgent, "process_carrier_invoice", source)
    review = deterministic_review({
        "target": {"task_id": "task-test", "agent": "carrier_ap", "skill": "process_carrier_invoice",
                   "case_id": source["case_id"], "a2a_state": "completed"},
        "source": source, "artifact": result.model_dump(), "history": [], "flag": None, "simulated": True})
    assert review.verdict == "pass"
    json.dumps(review.model_dump())
    with_work = {c["name"]: c["work"] for c in review.checks if c.get("work")}
    assert {"delta", "contract_tolerance", "payment_proposal_amount", "approve_clear_of_above_tolerance"} <= set(with_work)
    assert with_work["delta"]["result"] == 24.5 and set(with_work["delta"]) == WORKING_KEYS
    assert with_work["payment_proposal_amount"]["formula"] == "invoice_amount - paid_amount"
