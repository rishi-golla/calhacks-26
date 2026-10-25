"""The review is two-sided: wrong approvals and wrong holds are both issues, decoys pass.

Each scenario here mirrors a planted simulator pattern (sim/generator.py SCENARIOS) using the
same source field names the W4 export produces (ingest/source_export.py).
"""
import pytest

from agents.validator.tools import ADVISORY, ISSUE, deterministic_review, grounded_issues, lesson_text
from common.agent_support import identifier
from common.envelope import ResultArtifact

TASK = "task-ap"
REFS = ["doc:RC-1", "doc:POD-1", "doc:INV-1"]


def source(**extra):
    base = {"case_id": "CASE-1", "invoice_id": "INV-1", "load_id": "LD-1", "carrier_id": "C-1", "shipper_id": "S-1",
            "invoice_amount": 2000, "contract_rate": 2000, "period": "2026-10", "evidence_refs": REFS,
            "issued_at": "2026-10-24T00:00:00+00:00", "free_time_min": 120, "gps_dwell_min": 40,
            "line_items": [{"kind": "linehaul", "amount": 1500}, {"kind": "fuel_surcharge", "amount": 500}],
            "documents": [{"kind": "rate_confirmation", "ref": "doc:RC-1", "load_id": "LD-1"},
                          {"kind": "pod", "ref": "doc:POD-1", "load_id": "LD-1", "signed": True}]}
    base.update(extra)
    return base


def match(src, above=None):
    invoice, contract = src["invoice_amount"], src["contract_rate"]
    return {"invoice_amount": invoice, "contract_rate": contract, "delta": round(invoice - contract, 2),
            "above_tolerance": invoice > contract * 1.02 if above is None else above,
            "totals_match": round(sum(x["amount"] for x in src["line_items"]), 2) == invoice}


def approve(src, **findings):
    proposal = {"executed": False, "simulated": True, "proposed_payload": {
        "id": identifier(TASK, "payment_proposal"), "invoice_id": src["invoice_id"],
        "amount": round(src["invoice_amount"] - src.get("paid_amount", 0), 2)}}
    return ResultArtifact(outcome="approve", evidence_refs=REFS,
                          findings={"match": match(src), "payment_proposal": proposal, **findings}).model_dump()


def hold(src, **findings):
    return ResultArtifact(outcome="hold", evidence_refs=REFS, policy_ids=["P-001", "P-002"],
                          findings={"match": match(src), "payment_blocked": True, **findings}).model_dump()


def bundle(src, artifact, agent="carrier_ap", skill="process_carrier_invoice"):
    return {"target": {"task_id": TASK, "agent": agent, "skill": skill, "case_id": "CASE-1",
                       "a2a_state": "completed"},
            "source": src, "artifact": artifact, "history": [], "flag": None, "simulated": True}


def failed(review, severity=ISSUE):
    return {x["name"] for x in review.checks if not x["ok"] and x["severity"] == severity}


def test_clean_and_decoy_approvals_pass():
    assert deterministic_review(bundle(source(), approve(source()))).verdict == "pass"
    quickpay = source(invoice_amount=1960, line_items=[{"kind": "linehaul", "amount": 1500},
                                                      {"kind": "fuel_surcharge", "amount": 500},
                                                      {"kind": "quickpay_discount", "amount": -40}])
    assert deterministic_review(bundle(quickpay, approve(quickpay))).verdict == "pass"


def test_approving_over_tolerance_with_honest_numbers_is_an_issue():
    src = source(invoice_amount=2150, line_items=[{"kind": "linehaul", "amount": 1650},
                                                  {"kind": "fuel_surcharge", "amount": 500}])
    review = deterministic_review(bundle(src, approve(src)))
    assert review.verdict == "issue"
    assert "approve_clear_of_above_tolerance" in failed(review)
    assert "decision_justified" not in failed(review)


def test_unsigned_pod_approval_is_an_issue():
    src = source()
    src["documents"][1]["signed"] = False
    review = deterministic_review(bundle(src, approve(src)))
    assert "approve_clear_of_missing_documents" in failed(review)


def test_renumbered_duplicate_approval_uses_previous_invoices():
    src = source(previous_invoices=[{"id": "INV-0", "carrier_id": "C-1", "load_id": "LD-1", "amount": 2000,
                                     "issued_at": "2026-10-20T00:00:00+00:00"}])
    review = deterministic_review(bundle(src, approve(src)))
    assert "approve_clear_of_duplicate_invoice" in failed(review)
    rejected = ResultArtifact(outcome="reject", evidence_refs=REFS, policy_ids=["P-007"], findings={
        "match": match(src), "duplicate_invoice_ids": ["INV-0"]}).model_dump()
    assert deterministic_review(bundle(src, rejected)).verdict == "pass"


def test_unsupported_detention_approval_is_an_issue_and_hold_is_justified():
    src = source(invoice_amount=2150, detention_hours_claimed=2, gps_dwell_min=47, free_time_min=120,
                 line_items=[{"kind": "linehaul", "amount": 1500}, {"kind": "fuel_surcharge", "amount": 500},
                             {"kind": "detention", "amount": 150, "receipt_required": True, "receipt_ref": None}])
    assert "approve_clear_of_unsupported_charges" in failed(deterministic_review(bundle(src, approve(src))))
    assert deterministic_review(bundle(src, hold(src))).verdict == "pass"


def test_holding_a_clean_invoice_is_an_issue():
    src = source()
    review = deterministic_review(bundle(src, hold(src)))
    assert review.verdict == "issue"
    assert failed(review) == {"decision_justified"}
    assert "approve_clear_of_above_tolerance" not in {x["name"] for x in review.checks}


def test_proposal_amount_and_invoice_are_checked():
    src = source(paid_amount=500)
    artifact = approve(src)
    artifact["findings"]["payment_proposal"]["proposed_payload"]["amount"] = 2000
    review = deterministic_review(bundle(src, artifact))
    assert "payment_proposal_amount" in failed(review)
    artifact["findings"].pop("payment_proposal")
    assert "payment_proposal_recorded" in failed(deterministic_review(bundle(src, artifact)))


def test_live_rows_take_precedence_over_receipts_and_large_payments_need_a_human():
    src = source(invoice_amount=9400, contract_rate=9400,
                 line_items=[{"kind": "linehaul", "amount": 9400}])
    artifact = approve(src)
    proposal_id = identifier(TASK, "payment_proposal")
    src["payment_proposals"] = [{"id": proposal_id, "invoice_id": "INV-1", "amount": 9400, "status": "proposed",
                                 "approval_id": ""}]
    review = deterministic_review(bundle(src, artifact))
    assert "approval_present" in failed(review)
    src["payment_proposals"][0]["approval_id"] = "APR-1"
    src["approvals"] = [{"id": "APR-1", "decision": "pending", "code_ok": False, "approver": "carrier_ap",
                         "requested_by": "carrier_ap"}]
    review = deterministic_review(bundle(src, artifact))
    assert {"approval_decided", "separation_of_duties"} <= failed(review)
    src["approvals"][0].update(decision="approved", code_ok=True, approver="alice@broker.example")
    assert deterministic_review(bundle(src, artifact)).verdict == "pass"
    src["payment_proposals"][0]["amount"] = 9401
    assert "payment_proposal_amount" in failed(deterministic_review(bundle(src, artifact)))


def test_books_level_invariants_apply_to_every_review():
    src = source()
    src["holds"] = [{"id": "H-1", "invoice_id": "INV-9", "status": "active", "amount": 100}]
    src["payment_proposals"] = [{"id": "P-9", "invoice_id": "INV-9", "amount": 100, "status": "proposed"}]
    src["ledger_entries"] = [{"id": "LE-1", "lines": [{"account": "cash", "debit": 10, "credit": 0},
                                                       {"account": "ar", "debit": 0, "credit": 9}]}]
    review = deterministic_review(bundle(src, approve(src)))
    assert review.verdict == "pass"
    assert {"no_payment_under_hold", "ledger_entries_balanced"} <= failed(review, ADVISORY)


def test_controller_hold_amount_and_unjustified_clear():
    src = source(invoice_amount=2150, detention_hours_claimed=2, gps_dwell_min=47,
                 line_items=[{"kind": "linehaul", "amount": 1500}, {"kind": "fuel_surcharge", "amount": 500},
                             {"kind": "detention", "amount": 150, "receipt_required": True, "receipt_ref": None}])
    held = ResultArtifact(outcome="hold", evidence_refs=REFS, findings={
        "hold_amount": 150, "payment_blocked": True,
        "hold": {"proposed_payload": {"id": identifier(TASK, "hold"), "amount": 150}}}).model_dump()
    assert deterministic_review(bundle(src, held, "controller", "review_hold")).verdict == "pass"
    held["findings"]["hold"]["proposed_payload"]["amount"] = 2150
    assert "hold_recorded_amount" in failed(deterministic_review(bundle(src, held, "controller", "review_hold")))
    cleared = ResultArtifact(outcome="clear", evidence_refs=REFS, findings={"hold_required": False}).model_dump()
    assert "hold_not_required" in failed(deterministic_review(bundle(src, cleared, "controller", "review_hold")))
    clean = source()
    assert deterministic_review(bundle(clean, cleared, "controller", "review_hold")).verdict == "pass"
    over_held = ResultArtifact(outcome="hold", evidence_refs=REFS, findings={
        "hold_amount": 2000, "payment_blocked": True,
        "hold": {"proposed_payload": {"id": identifier(TASK, "hold"), "amount": 2000}}}).model_dump()
    assert {"hold_required", "hold_amount"} <= failed(deterministic_review(
        bundle(clean, over_held, "controller", "review_hold")))


def test_controller_entries_are_checked_per_request():
    src = source(journal_requests=[
        {"id": "JR-1", "case_id": "CASE-1", "requested_by": "shipper_ar", "period": "2026-10",
         "lines": [{"account": "cash", "debit": 100, "credit": 0}, {"account": "ar", "debit": 0, "credit": 100}]},
        {"id": "JR-2", "case_id": "CASE-1", "requested_by": "controller", "period": "2026-10",
         "lines": [{"account": "cash", "debit": 5, "credit": 0}, {"account": "ar", "debit": 0, "credit": 5}]}])
    posted = ResultArtifact(outcome="partial", evidence_refs=REFS, findings={"entries": [
        {"request_id": "JR-1", "outcome": "approve"}, {"request_id": "JR-2", "outcome": "reject",
                                                       "reason": "requester is approver"}]}).model_dump()
    assert deterministic_review(bundle(src, posted, "controller", "approve_entry")).verdict == "pass"
    posted["findings"]["entries"][1] = {"request_id": "JR-2", "outcome": "approve"}
    review = deterministic_review(bundle(src, posted, "controller", "approve_entry"))
    assert {"separation_of_duties", "entries_outcome"} <= failed(review)
    posted["outcome"] = "approve"
    assert "entries_outcome" not in failed(deterministic_review(bundle(src, posted, "controller", "approve_entry")))


def test_shipper_ar_outcome_follows_balance_and_short_pay_is_advisory():
    src = source(shipper_invoice_id="SINV-1", shipper_invoice_amount=2600, deposits=[
        {"id": "D-1", "invoice_id": "SINV-1", "shipper_id": "S-1", "amount": 2400},
        {"id": "D-2", "invoice_id": "SINV-1", "shipper_id": "S-OTHER", "amount": 200}])
    request_id = identifier("S-1:D-1", "receivable_journal_request")
    artifact = ResultArtifact(outcome="partial", evidence_refs=REFS, findings={
        "matched_amount": 2400, "balance": 200, "matched_deposit_ids": ["D-1"],
        "journal_requests": [{"executed": False, "simulated": True, "proposed_payload": {
            "id": request_id, "lines": [{"account": "cash", "debit": 2400, "credit": 0},
                                        {"account": "accounts_receivable", "debit": 0, "credit": 2400}]}}]}).model_dump()
    review = deterministic_review(bundle(src, artifact, "shipper_ar", "reconcile_customer_payment"))
    assert review.verdict == "pass"
    assert failed(review, ADVISORY) == {"short_pay_explained"}
    misapplied = dict(artifact, findings={**artifact["findings"], "matched_amount": 2600, "balance": 0,
                                          "matched_deposit_ids": ["D-1", "D-2"]}, outcome="verified")
    review = deterministic_review(bundle(src, misapplied, "shipper_ar", "reconcile_customer_payment"))
    assert {"matched_amount", "balance", "matched_deposit_ids", "outcome_matches_balance"} <= failed(review)


def test_cfo_forecast_rows_tie_to_recomputed_balances():
    src = source(shipper_invoice_amount=2600, shipper_paid_amount=0)
    artifact = ResultArtifact(outcome="verified", evidence_refs=REFS, findings={
        "invoiced_margin": 600, "contracted_margin": 600, "expected_collections": 2600,
        "outstanding_carrier_cost": 2000, "net_cash": 600, "period": "2026-10",
        "forecasts": [{"proposed_payload": {"category": "collections", "amount": 2600}},
                      {"proposed_payload": {"category": "carrier_payables", "amount": -2000}}]}).model_dump()
    assert deterministic_review(bundle(src, artifact, "cfo", "cash_forecast")).verdict == "pass"
    artifact["findings"]["forecasts"][1]["proposed_payload"]["amount"] = -1999
    assert "forecast_carrier_payables" in failed(deterministic_review(bundle(src, artifact, "cfo", "cash_forecast")))


def test_lessons_name_the_failed_control_and_never_carry_long_text():
    src = source(invoice_amount=2150, line_items=[{"kind": "linehaul", "amount": 1650},
                                                  {"kind": "fuel_surcharge", "amount": 500}])
    review = deterministic_review(bundle(src, approve(src)))
    lesson = review.lessons[0]
    assert lesson.agent == "carrier_ap" and lesson.scope == {"carrier_id": "C-1", "skill": "process_carrier_invoice"}
    assert "approve_clear_of_above_tolerance" in lesson.text_or_diff and "P-001" in lesson.text_or_diff
    long = [{"name": "x", "expected": "y" * 500, "actual": "z" * 500, "basis": ""}] * 20
    text = lesson_text("skill", long)
    assert len(text) <= 1000 and "[see review]" in text and "and 14 more" in text


@pytest.mark.parametrize("broken", ["failed_run", "missing_source"])
def test_failed_runs_and_missing_source_are_rerun_without_a_lesson(broken):
    src = source()
    reviewed = bundle(src, approve(src))
    if broken == "failed_run":
        reviewed["target"]["a2a_state"] = "failed"
    else:
        reviewed["source"] = {"source_error": "case not found"}
    review = deterministic_review(reviewed)
    assert review.verdict == "issue" and review.actions == ["rerun_task"]
    assert review.lessons == []


@pytest.mark.parametrize(("issues", "grounded", "ungrounded"), [
    (["approve_clear_of_above_tolerance was ignored"], 1, 0),
    (["doc:INV-1 shows a different payee"], 0, 1),
    (["net_cash_nonnegative failed"], 0, 1),
    (["evidence_present looks wrong"], 0, 1),
    (["the tone of the summary is odd"], 0, 1),
])
def test_model_issues_count_only_when_anchored_to_failed_issue_checks(issues, grounded, ungrounded):
    checks = [{"name": "approve_clear_of_above_tolerance", "ok": False, "severity": ISSUE},
              {"name": "net_cash_nonnegative", "ok": False, "severity": ADVISORY},
              {"name": "evidence_present", "ok": True, "severity": ISSUE}]
    found, dropped = grounded_issues(issues, checks)
    assert (len(found), len(dropped)) == (grounded, ungrounded)


def test_approval_driven_results_without_a_match_pass():
    src = source(invoice_amount=9400, contract_rate=9400, line_items=[{"kind": "linehaul", "amount": 9400}])
    rejected = ResultArtifact(outcome="reject", evidence_refs=REFS, policy_ids=["P-003"], findings={
        "approval": [{"outcome": "reject", "findings": {"approval_id": "APR-1", "rejected": True}}]}).model_dump()
    assert deterministic_review(bundle(src, rejected)).verdict == "pass"
    pending = ResultArtifact(outcome="unverified", evidence_refs=REFS, policy_ids=["P-003"], findings={
        "approval": [{"outcome": "unverified", "findings": {}}]}).model_dump()
    assert deterministic_review(bundle(src, pending)).verdict == "pass"
    paid = source(paid_amount=2000)
    cleared = ResultArtifact(outcome="clear", evidence_refs=REFS, findings={"already_paid": True}).model_dump()
    assert deterministic_review(bundle(paid, cleared)).verdict == "pass"
    rejected_clean = dict(rejected, findings={"approval": [{"outcome": "approve", "findings": {}}]})
    assert "decision_justified" in failed(deterministic_review(bundle(src, rejected_clean)))


def test_release_of_a_settled_hold_and_reconcile_follow_the_source():
    src = source(invoice_amount=2300, line_items=[{"kind": "linehaul", "amount": 2300}])
    released = ResultArtifact(outcome="clear", evidence_refs=REFS, findings={"already_released": True}).model_dump()
    assert deterministic_review(bundle(src, released, "controller", "review_hold")).verdict == "pass"
    unbalanced = source(journal_lines=[["cash", 100.0, 0.0], ["ar", 0.0, 99.0]])
    honest = ResultArtifact(outcome="unverified", evidence_refs=REFS, findings={
        "debits": 100, "credits": 99, "balanced": False}).model_dump()
    assert deterministic_review(bundle(unbalanced, honest, "controller", "reconcile")).verdict == "pass"
    dishonest = dict(honest, outcome="verified")
    assert "reconcile_outcome" in failed(deterministic_review(bundle(unbalanced, dishonest, "controller", "reconcile")))


def test_positional_journal_lines_from_the_sql_api_are_accepted():
    src = source()
    src["ledger_entries"] = [{"id": "LE-1", "lines": [["cash", 10.0, 0.0], ["ar", 0.0, 9.0]]}]
    review = deterministic_review(bundle(src, approve(src)))
    assert review.verdict == "pass"
    assert failed(review, ADVISORY) == {"ledger_entries_balanced"}


def test_unverified_above_threshold_needs_real_approval_evidence():
    src = source(invoice_amount=9400, contract_rate=9400, line_items=[{"kind": "linehaul", "amount": 9400}])
    bare = ResultArtifact(outcome="unverified", evidence_refs=REFS, findings={"match": match(src)}).model_dump()
    assert "decision_justified" in failed(deterministic_review(bundle(src, bare)))
    policy_stopped = dict(bare, findings={"match": match(src), "payment_proposal": {
        "executed": False, "decision": "needs_approval", "policy_ids": ["P-003"], "simulated": True}})
    assert deterministic_review(bundle(src, policy_stopped)).verdict == "pass"


def test_withdrawn_proposal_does_not_count_as_a_payment():
    src = source()
    artifact = approve(src)
    src["payment_proposals"] = [{"id": identifier(TASK, "payment_proposal"), "invoice_id": "INV-1", "amount": 2000,
                                 "status": "cancelled"}]
    assert "payment_proposal_open" in failed(deterministic_review(bundle(src, artifact)))
    src["payment_proposals"][0]["status"] = "proposed"
    assert deterministic_review(bundle(src, artifact)).verdict == "pass"


def test_fabricated_approval_reference_is_not_an_approval():
    src = source(invoice_amount=9400, contract_rate=9400, line_items=[{"kind": "linehaul", "amount": 9400}])
    artifact = approve(src)
    artifact["findings"]["payment_proposal"]["proposed_payload"]["approval_id"] = "APR-FAKE"
    src["approvals"] = []
    assert "approval_resolved" in failed(deterministic_review(bundle(src, artifact)))
    del src["approvals"]
    assert "approval_resolved" in failed(deterministic_review(bundle(src, artifact)), ADVISORY)


def test_positional_journal_lines_in_requests_are_summed():
    src = source(journal_requests=[{"id": "JR-1", "case_id": "CASE-1", "requested_by": "shipper_ar",
                                    "period": "2026-10", "lines": [["cash", 100.0, 0.0], ["ar", 0.0, 100.0]]}])
    posted = ResultArtifact(outcome="approve", evidence_refs=REFS, findings={"entries": [
        {"request_id": "JR-1", "outcome": "approve"}]}).model_dump()
    review = deterministic_review(bundle(src, posted, "controller", "approve_entry"))
    assert review.verdict == "pass"
