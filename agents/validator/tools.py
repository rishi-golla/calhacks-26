"""Independent, exact-cent review and authoritative review provenance.

The review is two-sided: an agent must not approve money it should have held, and it
must not hold money it should have paid. Every check names the control it applies
(a policy ID or accounting principle) and a severity. Only ``issue`` failures change
the verdict, trigger a corrective rerun or produce a lesson; ``advisory`` findings are
stored for the dashboard.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import re
from decimal import Decimal

from pydantic import ValidationError

from agents.carrier_ap.tools import deposit_inputs, duplicate_invoices, match_workings, three_way_match
from agents.documents.tools import check_packet
from agents.risk.tools import audit_charges, charge_workings
from common.agent_support import (
    billing_journal_id,
    identifier,
    journal_lines,
    matched_deposit_total,
    money,
    payable_journal_id,
)
from common.envelope import Friction, Lesson, ResultArtifact, ReviewOutput
from common.learning import apply_instruction_patch, memory_scope
from common.policy import APPROVAL_THRESHOLD_USD, CONTRACT_TOLERANCE
from common.source_repository import SourceRepository, SourceUnavailable, fixture_mode
from common.workings import plain, refs_for, working

FINANCIAL_AGENTS = {"carrier_ap", "shipper_ar", "controller", "cfo"}
TERMINAL = {"completed", "failed", "canceled", "rejected"}
ISSUE, ADVISORY = "issue", "advisory"
# Checks that fail because the run or its evidence is missing, not because the agent decided wrongly.
# Controls whose failure says something about this counterparty's records rather than the agent's method.
CASE_SPECIFIC_CONTROLS = {"contract_tolerance", "duplicate_invoice_ids", "line_items_total", "hold_required",
                          "hold_not_required", "short_pay_explained", "already_paid_in_full", "already_paid_flag",
                          "awaiting_invoice_has_no_bill", "unverified_reason", "matched_deposit_ids", "unique_deposits",
                          "approve_clear_of_duplicate_invoice"}
RUN_FAILURES = {"target_terminal", "execution_completed", "source_available", "result_artifact",
                "financial_source_complete"}
TOLERANCE = Decimal(1) + Decimal(str(CONTRACT_TOLERANCE))
THRESHOLD = Decimal(APPROVAL_THRESHOLD_USD)
# Why each settlement blocker matters, for the dashboard and the lesson text.
BLOCKER_BASIS = {
    "above_tolerance": "P-001 contract tolerance", "totals_mismatch": "P-010 invoice total equals its lines",
    "unsupported_charges": "P-001 accessorials need supporting evidence",
    "duplicate_invoice": "P-007 duplicate invoice", "missing_documents": "payment packet (rate con, signed POD)",
    "carrier_identity_mismatch": "P-002 tendered carrier must be the payee",
    "bank_change_requested": "P-004 unverified remit-to change", "active_hold": "P-002 hold blocks payment",
}
SOURCE_ERRORS = (SourceUnavailable, KeyError, TypeError, ValueError)


def encoded(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def lookup(db, table: str, field: str, value: str) -> list[dict]:
    allowed = {"task": {"task_id"}, "case": {"case_id"}, "flag": {"id"},
               "change_set": {"id", "task_id"}, "cell_change": {"id"},
               "audit_event": {"task_id"}, "hold": {"case_id"}, "payment_proposal": {"case_id"},
               "approval": {"case_id"}, "ledger_entry": {"case_id"}, "journal_request": {"case_id"},
               "cash_forecast": {"case_id"}}
    if field not in allowed.get(table, set()):
        raise ValueError("unsupported review query")
    return await db.query_rows(f'SELECT * FROM "{table}" WHERE {field} = {encoded(value)}')


async def flag_target(db, flag: dict) -> str | None:
    """Resolve supported UI references without trusting flag text or guessing a task."""
    ref = flag.get("target_ref", "")
    kind, _, key = ref.partition(":")
    if kind == "task":
        return key
    if kind in {"change_set", "changeset"}:
        changes = await lookup(db, "change_set", "id", key)
    elif kind == "cell_change":
        cells = await lookup(db, "cell_change", "id", key)
        changes = await lookup(db, "change_set", "id", cells[0]["change_set_id"]) if cells else []
    elif kind == "cell":
        sheet, _, rest = key.partition(":")
        row, _, column = rest.rpartition(":")
        if not all((sheet, row, column)):
            return None
        cells = await db.query_rows("SELECT * FROM cell_change WHERE "
            f"sheet = {encoded(sheet)} AND row_key = {encoded(row)} AND column = {encoded(column)}")
        changes = []
        for cell in cells:
            changes.extend(await lookup(db, "change_set", "id", cell["change_set_id"]))
        changes.sort(key=lambda item: int(item.get("sequence", 0)), reverse=True)
    else:
        return None
    for change in changes:
        if flag.get("case_id") and change.get("case_id") != flag["case_id"]:
            continue
        if change.get("task_id"):
            return change["task_id"]
    return None


# Reducer tables the review reads next to the source case. Fixture bundles may supply the
# same keys inside ``source`` so offline tests exercise identical checks.
LEDGER_TABLES = (("holds", "hold"), ("payment_proposals", "payment_proposal"), ("approvals", "approval"),
                 ("ledger_entries", "ledger_entry"), ("journal_requests", "journal_request"),
                 ("cash_forecasts", "cash_forecast"))


async def load_review(env, db) -> dict:
    if fixture_mode():
        target = dict(env.subject.get("target_task", {}))
        target.setdefault("task_id", env.subject.get("target_task_id", ""))
        target.setdefault("case_id", env.case_id)
        return {"target": target, "artifact": env.subject.get("artifact", {}),
                "source": env.subject.get("source", {}), "history": env.subject.get("history", []),
                "flag": env.subject.get("flag"), "simulated": True}
    if db.disabled:
        raise SourceUnavailable("live review requires SpacetimeDB")
    flag = None
    target_id = env.subject.get("target_task_id")
    if env.task_type == "review_flag":
        flags = await lookup(db, "flag", "id", str(env.subject.get("flag_id", "")))
        if not flags:
            raise SourceUnavailable("review flag does not exist")
        flag = flags[0]
        if flag.get("case_id") and flag["case_id"] != env.case_id:
            raise SourceUnavailable("flag belongs to another case")
        target_id = await flag_target(db, flag)
    if not target_id:
        return {"target": {}, "artifact": {}, "source": {}, "history": [], "flag": flag,
                "resolution": "target_provenance_unavailable", "simulated": False}
    targets = await lookup(db, "task", "task_id", str(target_id))
    if not targets or targets[0].get("case_id") != env.case_id:
        raise SourceUnavailable("review target is missing or belongs to another case")
    target = targets[0]
    try:
        artifact = json.loads(target.get("artifact_json", "{}"))
    except (ValueError, TypeError):
        artifact = {}
    try:
        source = await SourceRepository().load(env)
    except SourceUnavailable as error:
        source = {"source_error": str(error)}
    rows = await asyncio.gather(*(lookup(db, table, "case_id", env.case_id) for _, table in LEDGER_TABLES),
                                lookup(db, "audit_event", "task_id", str(target_id)))
    for (key, _), value in zip(LEDGER_TABLES, rows, strict=False):
        source[key] = value
    history = rows[-1]
    return {"target": target, "artifact": artifact, "source": source, "history": history,
            "flag": flag, "simulated": False}


async def review_bundle(agent, env, task_id: str) -> dict:
    """The review evidence, loaded once per task: the inspection tools and the guarded workflow judge the same
    bundle, and a replay reads the checkpoint instead of reading SpacetimeDB again."""
    from common.spacetime_client import SpacetimeClient
    db = SpacetimeClient(actor=agent.name)
    return await agent.step(task_id, "review_evidence", lambda: load_review(env, db))


class Checks:
    """Ordered review checks; each names the control it applies and how serious a failure is."""

    def __init__(self):
        self.items: list[dict] = []

    def add(self, name: str, ok: bool, expected, actual, *, basis: str = "", severity: str = ISSUE,
            work: dict | None = None) -> bool:
        item = {"name": name, "ok": bool(ok), "expected": plain(expected), "actual": plain(actual),
                "severity": severity, "basis": basis}
        if work:
            item["work"] = work  # the Validator's own recomputation, shown next to the agent's
        self.items.append(item)
        return bool(ok)

    def equal(self, name: str, expected: Decimal, actual, *, basis: str = "exact-cent recomputation",
              work: dict | None = None) -> bool:
        try:
            ok = money(actual, signed=True) == expected
        except SourceUnavailable:
            ok = False
        return self.add(name, ok, str(expected), actual, basis=basis, work=work)

    def failures(self, severity: str = ISSUE) -> list[dict]:
        return [item for item in self.items if not item["ok"] and item["severity"] == severity]


def totals(lines) -> tuple[Decimal, Decimal, list[dict]]:
    items = journal_lines(lines)
    debit = sum((money(x.get("debit", 0)) for x in items), Decimal(0))
    credit = sum((money(x.get("credit", 0)) for x in items), Decimal(0))
    return debit, credit, items


def journal_request_checks(c: Checks, name: str, receipts: list, stored: list[dict], request_id: str,
                           amount: Decimal, debit_account: str, credit_account: str, *, posted: bool,
                           basis: str) -> None:
    """One expected journal request, matched by its deterministic ID: requested (or already posted) once,
    balanced at the expected amount, and booked to the expected accounts."""
    receipt = next((r for r in receipts if isinstance(r, dict) and (r.get("request_id") == request_id
                    or (r.get("proposed_payload") or {}).get("id") == request_id)), None)
    recorded = recorded_write(receipt, stored, request_id)
    known = recorded is not None or receipt is not None or posted
    if not c.add(f"{name}_requested", known, request_id[:12], known, basis=basis):
        return
    lines = (recorded or {}).get("lines")
    if not lines:
        return
    debit, credit, items = totals(lines)
    c.add(f"{name}_balanced", debit == credit == amount, str(amount),
          {"debits": str(debit), "credits": str(credit)}, basis="P-006")
    accounts = {"debit": sorted(str(x.get("account")) for x in items if money(x.get("debit", 0)) > 0),
                "credit": sorted(str(x.get("account")) for x in items if money(x.get("credit", 0)) > 0)}
    c.add(f"{name}_accounts", accounts == {"debit": [debit_account], "credit": [credit_account]},
          {"debit": [debit_account], "credit": [credit_account]}, accounts, basis=basis)


def active_holds(source: dict, invoice_id=None) -> list[dict]:
    return [hold for hold in source.get("holds", []) if hold.get("status") == "active"
            and (not invoice_id or not hold.get("invoice_id") or hold.get("invoice_id") == invoice_id)]


def recorded_write(receipt, rows: list[dict], row_id: str) -> dict | None:
    """The reducer row when it exists, else the explicit fixture proposal the agent journaled."""
    for row in rows:
        if row.get("id") == row_id:
            return row
    if isinstance(receipt, dict) and isinstance(receipt.get("proposed_payload"), dict):
        return receipt["proposed_payload"]
    return None


def settlement_blockers(source: dict, match: dict) -> dict[str, bool]:
    """Every source fact that forbids paying this invoice as billed. Unverifiable means blocked."""
    try:
        duplicates = bool(duplicate_invoices(source))
    except SOURCE_ERRORS:
        duplicates = True
    try:
        unsupported = bool(audit_charges(source)["needs_verification"])
    except SOURCE_ERRORS:
        unsupported = True
    try:
        missing = bool(check_packet(source)["missing"])
    except SOURCE_ERRORS:
        missing = True
    return {"above_tolerance": bool(match["above_tolerance"]), "totals_mismatch": not match["totals_match"],
            "unsupported_charges": unsupported, "duplicate_invoice": duplicates, "missing_documents": missing,
            "carrier_identity_mismatch": bool(source.get("carrier_identity_mismatch")),
            "bank_change_requested": bool(source.get("bank_change_requested")),
            "active_hold": bool(active_holds(source, source.get("invoice_id")))}


def approval_checks(c: Checks, source: dict, approval_id, *, requested_by: str | None = None) -> None:
    """P-003: money above the threshold moves only on a decided human approval, never self-approved."""
    if not c.add("approval_present", bool(approval_id), "human approval reference", approval_id, basis="P-003"):
        return
    row = next((item for item in source.get("approvals", []) if item.get("id") == approval_id), None)
    if row is None:
        # Unverifiable means blocked: a reference the case's approval rows cannot resolve is not an approval.
        # Fixture bundles that carry no approval table at all are recorded as advisory, not fabricated.
        c.add("approval_resolved", False, "approval row in this case", approval_id, basis="P-003",
              severity=ISSUE if "approvals" in source else ADVISORY)
        return
    c.add("approval_decided", row.get("decision") == "approved" and row.get("code_ok") is not False,
          "approved with a verified code", {"decision": row.get("decision"), "code_ok": row.get("code_ok")},
          basis="P-003")
    requester = requested_by or row.get("requested_by")
    approver = row.get("approver")
    c.add("separation_of_duties", bool(approver) and approver != requester and approver not in FINANCIAL_AGENTS,
          "human approver distinct from requester", {"requested_by": requester, "approver": approver}, basis="P-005")


def review_carrier_ap(c: Checks, artifact: ResultArtifact, source: dict, skill: str, target: dict) -> None:
    findings = artifact.findings
    if skill == "answer_payment_status":
        c.equal("paid_amount", money(source.get("paid_amount", 0)), findings.get("paid_amount"))
        return
    if findings.get("awaiting_invoice"):
        c.add("awaiting_invoice_has_no_bill", source.get("invoice_amount") is None and not source.get("invoice_id"),
              "no carrier invoice in source", {"invoice_id": source.get("invoice_id")},
              basis="nothing to settle before the bill arrives")
        return
    invoice, contract = money(source.get("invoice_amount")), money(source.get("contract_rate"))
    paid = money(source.get("paid_amount", 0))
    match = three_way_match(source)
    shown = {item["label"]: item for item in match_workings(source, match)}
    outcome = artifact.outcome
    reported = findings.get("match")
    if outcome == "approve" or reported is not None:
        # Approvals always carry the match; approval-driven rejects and clears legitimately do not.
        reported = reported or {}
        for field, expected in (("invoice_amount", invoice), ("contract_rate", contract)):
            c.equal(field, expected, reported.get(field), work=working(
                f"Source {field.replace('_', ' ')}", field, {field: expected}, expected,
                evidence_refs=refs_for(source, field), basis="read from the source document"))
        c.equal("delta", invoice - contract, reported.get("delta"), work=shown.get("Invoice vs rate confirmation"))
        c.add("contract_tolerance", reported.get("above_tolerance") == match["above_tolerance"],
              match["above_tolerance"], reported.get("above_tolerance"), basis="P-001",
              work=shown.get("Within tolerance"))
        if "totals_match" in reported:
            c.add("line_items_total", reported["totals_match"] == match["totals_match"], match["totals_match"],
                  reported["totals_match"], basis="P-010", work=shown.get("Lines equal stated total"))
    blockers = settlement_blockers(source, match)
    try:
        charges = {item["label"]: item for item in charge_workings(source, audit_charges(source))}
    except SOURCE_ERRORS:
        charges = {}
    blocker_work = {"above_tolerance": shown.get("Within tolerance"), "totals_mismatch": shown.get("Lines equal stated total"),
                    "unsupported_charges": charges.get("Unsupported charges total"),
                    "missing_documents": shown.get("Payment packet")}
    present = sorted(name for name, blocked in blockers.items() if blocked)
    # A human approval decision (P-003) is a legitimate reason to stop without a source blocker.
    approval = findings.get("approval")
    decided = approval[-1] if isinstance(approval, list) and approval and isinstance(approval[-1], dict) else None
    human_rejected = outcome == "reject" and decided is not None and decided.get("outcome") == "reject"
    # Above the threshold an agent may stop only because approval is genuinely pending: a human decision
    # it waited on, or a proposal the policy returned as needs_approval. A bare "unverified" is not that.
    proposal_receipt = findings.get("payment_proposal") if isinstance(findings.get("payment_proposal"), dict) else {}
    pending_approval = outcome == "unverified" and invoice > THRESHOLD and (
        approval is not None or proposal_receipt.get("decision") == "needs_approval")
    if outcome == "approve":
        for name, blocked in blockers.items():
            c.add(f"approve_clear_of_{name}", not blocked, False, blocked, basis=BLOCKER_BASIS[name],
                  work=blocker_work.get(name))
        expected_amount = invoice - paid
        balance_work = working("Open balance to pay", "invoice_amount - paid_amount",
                               {"invoice_amount": invoice, "paid_amount": paid}, expected_amount,
                               evidence_refs=refs_for(source, "invoice_amount"), basis="pay once, never the disputed delta")
        c.add("approve_pays_open_balance", expected_amount > 0, "unpaid balance", str(expected_amount),
              basis="nothing left to pay is a clear, not an approve")
        recorded = recorded_write(findings.get("payment_proposal"), source.get("payment_proposals", []),
                                  identifier(str(target.get("task_id", "")), "payment_proposal"))
        if c.add("payment_proposal_recorded", recorded is not None, "stored or explicitly proposed payment",
                 recorded is not None, basis="an approve must leave a payment proposal"):
            c.equal("payment_proposal_amount", expected_amount, recorded.get("amount"),
                    basis="pay invoice minus paid, never the disputed delta (P-001)", work=balance_work)
            c.add("payment_proposal_invoice", recorded.get("invoice_id") == source.get("invoice_id"),
                  source.get("invoice_id"), recorded.get("invoice_id"), basis="proposal binds to this invoice")
            c.add("payment_proposal_open", recorded.get("status") not in {"canceled", "cancelled", "rejected"},
                  "proposed or paid", recorded.get("status", "proposed"),
                  basis="an approve whose proposal was withdrawn paid nothing")
            if invoice > THRESHOLD:
                approval_checks(c, source, recorded.get("approval_id"), requested_by="carrier_ap")
        if expected_amount > 0 and source.get("invoice_id"):
            # Proposing the bill accrues the carrier's cost once; no payment ever executes, so AP stays open.
            if source.get("period") or source.get("issued_at"):
                journal_request_checks(c, "payable_journal", findings.get("journal_requests") or [],
                                       source.get("journal_requests", []), payable_journal_id(source["invoice_id"]),
                                       expected_amount, "freight_expense", "accounts_payable",
                                       posted=bool(findings.get("journal_posting")),
                                       basis="a proposed carrier bill is booked once: Dr expense / Cr payable")
            else:
                c.add("payable_journal_requested", False, "an accounting period", None, severity=ADVISORY,
                      basis="the carrier bill cannot be booked without a period")
    elif outcome in {"hold", "reject", "unverified"}:
        reasons = present + (["human_approval_rejected"] if human_rejected else []) \
            + (["approval_pending_above_threshold"] if pending_approval else [])
        c.add("decision_justified", bool(reasons), "a source-backed blocker or human approval decision", reasons,
              basis="hold or reject only what the source disputes",
              work=next((blocker_work[name] for name in present if blocker_work.get(name)), None))
        if outcome == "hold":
            c.add("hold_blocks_payment", findings.get("payment_blocked") is True, True,
                  findings.get("payment_blocked"), basis="P-002")
        if outcome == "reject" and findings.get("duplicate_invoice_ids") is not None:
            try:
                expected_ids = sorted(duplicate_invoices(source))
            except SOURCE_ERRORS:
                expected_ids = None
            c.add("duplicate_invoice_ids", expected_ids == sorted(findings["duplicate_invoice_ids"]),
                  expected_ids, findings["duplicate_invoice_ids"], basis="P-007")
    elif outcome == "clear":
        c.add("already_paid_in_full", paid >= invoice, str(invoice), str(paid),
              basis="clear means the invoice is settled")
    else:
        c.add("settlement_outcome_known", False, ["approve", "hold", "reject", "unverified", "clear"], outcome,
              basis="settlement outcomes are a closed set")


def review_controller(c: Checks, artifact: ResultArtifact, source: dict, skill: str, target: dict) -> None:
    findings, outcome = artifact.findings, artifact.outcome
    if skill in {"reconcile", "approve_entry"}:
        request = source.get("journal_request") or {}
        requests = [*source.get("journal_requests", []), *filter(None, [request])]
        entries = findings.get("entries") or [] if skill == "approve_entry" else []
        if skill == "approve_entry":
            c.add("entries_reported", bool(entries), "one entry per journal request", len(entries),
                  basis="every request resolves")
            outcomes = {entry.get("outcome") for entry in entries}
            expected = "reject" if outcomes == {"reject"} else "partial" if "reject" in outcomes else \
                "approve" if "approve" in outcomes else "verified"
            if entries:
                c.add("entries_outcome", outcome == expected, expected, outcome,
                      basis="the batch outcome summarizes its entries")
        for entry in entries:
            stored = next((r for r in requests if r.get("id") == entry.get("request_id")), request)
            requester = stored.get("requested_by")
            if entry.get("outcome") == "approve":
                c.add("separation_of_duties", bool(requester) and requester != "controller",
                      "distinct requester", requester, basis="P-005")
                d, k, stored_lines = totals(stored.get("lines"))
                c.add("posted_entry_balanced", bool(stored_lines) and d == k and d > 0, "debits equal credits",
                      {"debits": str(d), "credits": str(k)}, basis="P-006")
            elif entry.get("outcome") == "reject":
                c.add("rejection_has_cause", requester == "controller" or bool(entry.get("reason")),
                      "self-request or stated reason", entry.get("reason"), basis="P-005")
        if skill == "reconcile":
            debits, credits, lines = totals(source.get("journal_lines", []))
            balanced = bool(lines) and debits == credits and debits > 0
            c.equal("debits", debits, findings.get("debits"))
            c.equal("credits", credits, findings.get("credits"))
            c.add("reconcile_outcome", outcome == ("verified" if balanced else "unverified"),
                  "verified" if balanced else "unverified", outcome,
                  basis="P-006: report the journal as the source shows it")
        elif request and not entries:
            debits, credits, lines = totals(request.get("lines", source.get("journal_lines", [])))
            c.add("journal_balanced", bool(lines) and debits == credits and debits > 0, "positive balanced journal",
                  {"debits": str(debits), "credits": str(credits)}, basis="P-006")
            c.add("separation_of_duties", bool(request.get("requested_by"))
                  and request["requested_by"] != "controller", "distinct requester",
                  request.get("requested_by"), basis="P-005")
        return
    if skill == "check_already_paid":
        paid, invoice = money(source.get("paid_amount", 0)), money(source.get("invoice_amount"))
        c.equal("paid_amount", paid, findings.get("paid_amount"))
        c.add("already_paid_flag", findings.get("already_paid") == (paid >= invoice), paid >= invoice,
              findings.get("already_paid"), basis="paid status follows the ledger")
        return
    amount, contract = money(source.get("invoice_amount")), money(source.get("contract_rate"))
    try:
        unsupported = money(audit_charges(source)["unsupported_amount"])
    except SOURCE_ERRORS:
        unsupported = Decimal(0)
    bank = bool(source.get("bank_change_requested"))
    hold_required = bank or amount > contract * TOLERANCE or unsupported > 0
    expected_hold = amount if bank else max(amount - contract, unsupported)
    hold_inputs = {"bank_change": bank, "invoice_amount": amount, "contract_rate": contract, "unsupported": unsupported}
    hold_refs = refs_for(source, "invoice_amount", "contract_rate")
    required_work = working("Hold required", "bank_change or invoice_amount > contract_rate * 1.02 or unsupported > 0",
                            hold_inputs, hold_required, unit="bool", evidence_refs=hold_refs, basis="P-001/P-004")
    amount_work = working("Amount to hold", "invoice_amount if bank_change else max(invoice_amount - contract_rate, unsupported)",
                          hold_inputs, expected_hold, evidence_refs=hold_refs, basis="hold only the disputed money")
    if outcome == "hold":
        c.add("hold_required", hold_required, "tolerance breach, unsupported charge or bank change",
              {"delta": str(amount - contract), "unsupported": str(unsupported), "bank_change": bank},
              basis="P-001/P-004: hold only disputed money", work=required_work)
        c.equal("hold_amount", expected_hold, findings.get("hold_amount"),
                basis="hold the disputed delta, or the full invoice on a bank change", work=amount_work)
        c.add("hold_blocks_payment", findings.get("payment_blocked") is True, True, findings.get("payment_blocked"),
              basis="P-002")
        recorded = recorded_write(findings.get("hold"), source.get("holds", []),
                                  identifier(str(target.get("task_id", "")), "hold"))
        if c.add("hold_recorded", recorded is not None, "stored or explicitly proposed hold", recorded is not None,
                 basis="a hold decision must leave a hold row"):
            c.equal("hold_recorded_amount", expected_hold, recorded.get("amount"))
    elif outcome == "clear" and findings.get("already_released"):
        c.add("release_already_complete", True, "nothing left to release", True, basis="idempotent release")
    elif outcome == "clear":
        c.add("hold_not_required", not hold_required, False,
              {"delta": str(amount - contract), "unsupported": str(unsupported), "bank_change": bank},
              basis="P-001/P-004", work=required_work)
    elif outcome == "approve":
        c.add("approval_evidence", bool(findings.get("approval_id") or findings.get("release")),
              "stored approval or release receipt", bool(findings.get("approval_id") or findings.get("release")),
              basis="P-003")
        if findings.get("approval_id") and source.get("approvals"):
            approval_checks(c, source, findings["approval_id"])
    elif outcome == "reject":
        c.add("rejection_recorded", bool(findings.get("rejected") or findings.get("reason")
                                         or findings.get("approval_id")),
              "rejected approval or stated reason", findings.get("reason"), basis="P-003")
    elif outcome == "unverified":
        c.add("unverified_reason", bool(findings.get("reason")), "stated missing evidence", findings.get("reason"),
              basis="an unverified decision names what is missing")


def review_shipper_ar(c: Checks, artifact: ResultArtifact, source: dict, skill: str) -> None:
    findings, outcome = artifact.findings, artifact.outcome
    if skill == "set_credit_limit":
        c.add("credit_recommendation_only", findings.get("credit_limit_changed") is False, False,
              findings.get("credit_limit_changed"), basis="agents recommend limits, humans set them")
        return
    invoice_id, shipper_id = source.get("shipper_invoice_id"), source.get("shipper_id")
    deposits = source.get("deposits", [])
    matched = [x for x in deposits if invoice_id and x.get("invoice_id") == invoice_id
               and x.get("shipper_id") == shipper_id]
    ids = [x.get("id") for x in matched]
    c.add("unique_deposits", all(ids) and len(ids) == len(set(ids)), "unique deposit IDs", ids,
          basis="each deposit is applied once")
    total = sum((money(x["amount"]) for x in matched), Decimal(0))
    owed = money(source.get("shipper_invoice_amount"))
    balance = owed - total
    applied = deposit_inputs(matched)
    invoice_refs = refs_for(source, "shipper_invoice_amount")
    c.equal("matched_amount", total, findings.get("matched_amount"), work=working(
        "Cash applied to this invoice (" + ", ".join(str(x.get("id")) for x in matched) + ")",
        " + ".join(applied) if applied else "0", applied, total,
        evidence_refs=invoice_refs, basis="each deposit applies once, to its own invoice"))
    c.equal("balance", balance, findings.get("balance"), work=working(
        "Open balance", "shipper_invoice_amount - matched_amount", {"shipper_invoice_amount": owed, "matched_amount": total},
        balance, evidence_refs=invoice_refs, basis="short pay is partial, overpayment is suspicious"))
    if findings.get("matched_deposit_ids") is not None:
        c.add("matched_deposit_ids", sorted(findings["matched_deposit_ids"]) == sorted(ids), sorted(ids),
              findings["matched_deposit_ids"], basis="cash applies only to this shipper's invoice")
    # Unreferenced cash matched to a settled invoice is held for a person, never reported as settled.
    expected = "partial" if balance > 0 else "suspicious" if balance < 0 else (
        "hold" if source.get("unapplied_deposits") else "verified")
    not_due = skill == "chase_overdue" and balance > 0 and not source.get("overdue") and outcome == "clear"
    c.add("outcome_matches_balance", outcome == expected or not_due, expected, outcome,
          basis="short pay is partial, overpayment is suspicious, settled is verified")
    if balance > 0 and total > 0:
        explanation = {"deduction_reason": source.get("deduction_reason"), "outreach": bool(findings.get("outreach")),
                       "cash_impact": bool(findings.get("cash_impact"))}
        c.add("short_pay_explained", bool(explanation["deduction_reason"] or explanation["outreach"]
                                          or explanation["cash_impact"]),
              "deduction reason, collections contact or cash impact", explanation, severity=ADVISORY,
              basis="a short pay needs a reason code or a collections follow-up")
    if skill == "reconcile_customer_payment":
        posted = bool(findings.get("controller"))
        receipts = findings.get("journal_requests") or []
        stored = source.get("journal_requests", [])
        if invoice_id and owed > 0:
            # The shipper invoice is recognised once, before any cash is applied against it.
            journal_request_checks(c, "billing_journal", receipts, stored, billing_journal_id(invoice_id), owed,
                                   "accounts_receivable", "freight_revenue", posted=posted,
                                   basis="the shipper invoice is billed once: Dr receivable / Cr revenue")
        for deposit in matched:
            journal_request_checks(c, "receivable_journal", receipts, stored,
                                   identifier(f"{shipper_id}:{deposit['id']}", "receivable_journal_request"),
                                   money(deposit["amount"]), "cash", "accounts_receivable", posted=posted,
                                   basis="matched cash posts to the books once per deposit")


def review_cfo(c: Checks, artifact: ResultArtifact, source: dict) -> None:
    findings = artifact.findings
    revenue, cost = money(source.get("shipper_invoice_amount")), money(source.get("invoice_amount"))
    contract = money(source.get("contract_rate"))
    receivables = source.get("receivables")
    if receivables is None:
        # Same collections basis as the CFO: matched deposits when no paid summary exists.
        collected = source.get("shipper_paid_amount")
        if collected is None:
            collected = matched_deposit_total(source)
        receivables = [{"amount": revenue, "collected": collected}]
    payables = source.get("payables", [{"amount": cost, "paid": source.get("paid_amount", 0)}])
    incoming = sum((money(x["amount"]) - money(x.get("collected", 0)) for x in receivables), Decimal(0))
    outgoing = sum((money(x["amount"]) - money(x.get("paid", 0)) for x in payables), Decimal(0))
    revenue_refs, cost_refs = refs_for(source, "shipper_invoice_amount"), refs_for(source, "invoice_amount", "contract_rate")
    formulas = {
        "invoiced_margin": ("revenue - invoiced_carrier_cost", {"revenue": revenue, "invoiced_carrier_cost": cost}),
        "contracted_margin": ("revenue - contracted_carrier_cost", {"revenue": revenue, "contracted_carrier_cost": contract}),
        "expected_collections": ("sum(receivable amount - collected)",
                                 {f"receivable_{i + 1}": {"amount": r["amount"], "collected": r.get("collected", 0)}
                                  for i, r in enumerate(receivables)}),
        "outstanding_carrier_cost": ("sum(payable amount - paid)",
                                     {f"payable_{i + 1}": {"amount": p["amount"], "paid": p.get("paid", 0)}
                                      for i, p in enumerate(payables)}),
        "net_cash": ("expected_collections - outstanding_carrier_cost",
                     {"expected_collections": incoming, "outstanding_carrier_cost": outgoing}),
    }
    for field, expected in (("invoiced_margin", revenue - cost), ("contracted_margin", revenue - contract),
                            ("expected_collections", incoming), ("outstanding_carrier_cost", outgoing),
                            ("net_cash", incoming - outgoing)):
        formula, inputs = formulas[field]
        c.equal(field, expected, findings.get(field), work=working(
            field.replace("_", " ").capitalize(), formula, inputs, expected,
            evidence_refs=revenue_refs + cost_refs, basis="recomputed from source balances"))
    for field, expected in (("revenue", revenue), ("invoiced_carrier_cost", cost), ("contracted_carrier_cost", contract)):
        if field in findings:
            c.equal(field, expected, findings[field])
    receipts = findings.get("forecasts") or []
    rows = source.get("cash_forecasts", [])
    period = source.get("period") or findings.get("period")
    for category, expected in (("collections", incoming), ("carrier_payables", -outgoing)):
        row_id = identifier(str(source.get("case_id", "")), f"{period}:{category}")
        receipt = next((r for r in receipts if isinstance(r, dict)
                        and (r.get("proposed_payload") or {}).get("category") == category), None)
        recorded = recorded_write(receipt, rows, row_id)
        if c.add(f"forecast_{category}_recorded", recorded is not None, "stored or proposed forecast row",
                 recorded is not None, severity=ADVISORY, basis="forecast rows tie to the recomputed balances"):
            c.equal(f"forecast_{category}", expected, recorded.get("amount"),
                    basis="forecast equals subledger balance")
    c.add("net_cash_nonnegative", incoming - outgoing >= 0, ">= 0", str(incoming - outgoing), severity=ADVISORY,
          basis="negative net cash on a load is worth a human look")


def review_books(c: Checks, source: dict) -> None:
    """Case-level invariants. They are advisory: a violation belongs to the case, not to the
    agent under review, so it must reach a human without rerunning unrelated work."""
    entries = source.get("ledger_entries", [])
    unbalanced = []
    for entry in entries:
        debit, credit, lines = totals(entry.get("lines"))
        if not lines or debit != credit:
            unbalanced.append(entry.get("id"))
    if entries:
        c.add("ledger_entries_balanced", not unbalanced, [], unbalanced, severity=ADVISORY, basis="P-006")
    held_invoices = {hold.get("invoice_id") for hold in active_holds(source) if hold.get("invoice_id")}
    paid_under_hold = [p.get("id") for p in source.get("payment_proposals", [])
                       if p.get("status") not in {"canceled", "cancelled", "rejected"}
                       and p.get("invoice_id") in held_invoices]
    if source.get("payment_proposals"):
        c.add("no_payment_under_hold", not paid_under_hold, [], paid_under_hold, severity=ADVISORY, basis="P-002")


def deterministic_review(bundle: dict) -> ReviewOutput:
    target, raw, source = bundle["target"], bundle["artifact"], bundle["source"]
    c = Checks()
    c.add("target_terminal", target.get("a2a_state") in TERMINAL, "terminal task", target.get("a2a_state"),
          basis="only finished work is reviewed")
    c.add("execution_completed", target.get("a2a_state") == "completed", "completed", target.get("a2a_state"),
          basis="a failed run has no trustworthy result")
    c.add("source_available", bool(source) and not source.get("source_error"), "authoritative source records",
          source.get("source_error", bool(source)), basis="review needs the same source the agent had")
    try:
        artifact = ResultArtifact.model_validate(raw)
    except ValidationError:
        artifact = None
    c.add("result_artifact", artifact is not None, "valid ResultArtifact", bool(artifact), basis="PRD 7 contract")
    role, skill = target.get("agent"), target.get("skill")
    c.add("review_supported", role in FINANCIAL_AGENTS, "financial task", role, basis="PRD 7.7 scope")
    if artifact:
        c.add("skill_implemented", artifact.outcome != "not_implemented", "implemented skill", artifact.outcome,
              basis="runtime guard")
        refs = source.get("evidence_refs", [])
        c.add("evidence_present", bool(artifact.evidence_refs), "source citations", artifact.evidence_refs,
              basis="every result cites its documents")
        c.add("evidence_grounded", bool(refs) and set(artifact.evidence_refs).issubset(refs), refs,
              artifact.evidence_refs, basis="citations come from the source, never invented")
        try:
            if role == "cfo":
                review_cfo(c, artifact, source)
            elif role == "shipper_ar":
                review_shipper_ar(c, artifact, source, skill)
            elif role == "carrier_ap":
                review_carrier_ap(c, artifact, source, skill, target)
            elif role == "controller":
                review_controller(c, artifact, source, skill, target)
            if role in FINANCIAL_AGENTS:
                review_books(c, source)
        except SOURCE_ERRORS as error:
            c.add("financial_source_complete", False, "complete finite exact-cent financial records",
                  type(error).__name__, basis="incomplete evidence never passes")

    friction = []
    if any(not item["ok"] and item["name"] in {"source_available", "evidence_present", "evidence_grounded"}
           for item in c.items):
        friction.append(Friction(kind="missing_doc", detail="Review lacks complete source-backed evidence."))
    for event in bundle.get("history", []):
        kind = event.get("kind")
        if kind in {"retry", "reject", "policy_deny", "timeout", "missing_doc", "redelegation", "human_override"}:
            friction.append(Friction(kind=kind, detail=str(event.get("summary", kind))[:1000]))
    if bundle.get("flag"):
        friction.append(Friction(kind="flag", detail="A human requested review; its note is evidence, not authorization."))
    failures = c.failures(ISSUE)
    lessons = []
    # A failed run, or a review without the agent's source, says nothing about the agent's judgment:
    # the task is still rerun, but it teaches no lesson.
    judged = not any(item["name"] in RUN_FAILURES for item in failures)
    if failures and role in FINANCIAL_AGENTS and judged:
        # The same scope every reader derives (common.learning.memory_scope), so the lesson is found again.
        # A lesson about the agent's procedure (grounding, justification, bookkeeping) applies to every case of
        # the skill; pinning it to this carrier and lane left 46 near-identical lessons that no new load recalled.
        # Only a finding about this counterparty's own records keeps the narrow carrier/lane/claim scope.
        case_specific = any(item["name"] in CASE_SPECIFIC_CONTROLS for item in failures)
        scope = memory_scope(source if case_specific else None, skill)
        lessons.append(Lesson(agent=role, type="memory", scope=scope, text_or_diff=lesson_text(skill, failures),
                              confidence=0.9, evidence_refs=artifact.evidence_refs if artifact else []))
    return ReviewOutput(verdict="issue" if failures else "pass", checks=c.items, friction=friction,
                        lessons=lessons, actions=["rerun_task"] if failures and role in FINANCIAL_AGENTS else ["none"])


# What the model sees of a review bundle. complete() refuses evidence over 100,000 characters and the tool loop
# refuses a tool result over 90,000 bytes; live bundles carry the target's artifact twice (artifact_json and the
# parsed artifact) and every audit event with its hash chain and up to 4,096-character summary, and on Oct 25 they
# were 104 to 126 KB. Every review then failed: inspect_review_target returned tool_result_exceeds_limit and the
# guarded workflow's model pass raised "evidence exceeds inference input limit" on each attempt. The deterministic
# checks still read the whole bundle; only the model's view is bounded, as tool audits are (MAX_AUDIT_SUMMARY_CHARS).
REVIEW_EVIDENCE_CHARS = 60_000
REVIEW_TOOL_CHARS = 30_000
MAX_HISTORY_EVENTS = 40
HISTORY_FIELDS = ("sequence", "ts", "kind", "reason_code", "model_used", "policy_ids", "evidence_refs")
# Per-call model telemetry (ids, timings, token usage) says nothing about the decision under review.
INTERNAL_FINDINGS = ("tool_loop",)


def encoded_size(value) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")))


def clip(value, *, text: int, items: int, depth: int = 0, citations: bool = False):
    """A bounded copy: strings cut to ``text`` characters, lists and objects to ``items`` entries, each cut marked.
    An ``evidence_refs`` list is cut without a marker: every string in it is a citation the model may use."""
    if depth >= 8:
        return "[details omitted]"
    if isinstance(value, dict):
        keys = list(value)
        clipped = {str(key)[:100]: clip(value[key], text=text, items=items, depth=depth + 1,
                                        citations=key == "evidence_refs")
                   for key in keys[:items]}
        if len(keys) > items:
            clipped["omitted_fields"] = len(keys) - items
        return clipped
    if isinstance(value, list):
        if citations:
            return [item for item in value[:items] if isinstance(item, str) and len(item) <= 500]
        clipped = [clip(item, text=text, items=items, depth=depth + 1) for item in value[:items]]
        if len(value) > items:
            clipped.append(f"[{len(value) - items} more omitted]")
        return clipped
    if isinstance(value, str) and len(value) > text:
        return value[:text] + "...[truncated]"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)[:text]


def _sequence(event) -> int:
    try:
        return int(event.get("sequence", 0)) if isinstance(event, dict) else 0
    except (TypeError, ValueError):
        return 0


def history_view(history: list) -> list[dict]:
    """Audit events without their hash chain; a tool event keeps only what ran and how it ended. SQL returns rows
    in no particular order, so the latest MAX_HISTORY_EVENTS are chosen by ``sequence``."""
    ordered = sorted(history, key=_sequence) if isinstance(history, list) else []
    events = []
    for event in ordered[-MAX_HISTORY_EVENTS:]:
        if not isinstance(event, dict):
            continue
        item = {key: event[key] for key in HISTORY_FIELDS if key in event}
        summary = event.get("summary")
        parsed = None
        if event.get("kind") == "tool_call" and isinstance(summary, str):
            try:
                parsed = json.loads(summary)
            except ValueError:
                parsed = None
        if isinstance(parsed, dict):
            item["tool"] = {key: parsed[key] for key in ("tool_name", "status", "duration_ms") if key in parsed}
            result = parsed.get("result")
            if isinstance(result, dict) and result.get("error"):
                item["tool"]["error"] = result["error"]
        elif summary:
            item["summary"] = str(summary)[:500]
        events.append(item)
    if len(ordered) > MAX_HISTORY_EVENTS:
        events.insert(0, {"omitted_earlier_events": len(ordered) - MAX_HISTORY_EVENTS})
    return events


def capped_refs(refs, chars: int) -> list[str]:
    """Citations in order, as many whole ones as fit in ``chars`` encoded characters (never a cut string)."""
    kept, used = [], 2
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, str) or len(ref) > 500:
            continue
        used += len(json.dumps(ref)) + 1
        if used > chars:
            break
        kept.append(ref)
    return kept


def bounded_checks(checks: list[dict], limit: int) -> list[dict]:
    """The authoritative checks within ``limit`` encoded characters.

    First every check is kept with only its ``expected``/``actual`` (and shown ``work``) values bounded. If the list
    is still too long (a payment with hundreds of matched deposits has a check per deposit), checks keep only name,
    result, severity and basis; failed checks come first, then passes as the budget allows, with a marker counting
    the passes left out. A failed check is never dropped while a passed one is kept."""
    for text, items in ((600, 20), (200, 10), (80, 5)):
        bounded = [{**item, **{key: clip(item[key], text=text, items=items)
                               for key in ("expected", "actual", "work") if key in item}} for item in checks]
        if encoded_size(bounded) <= limit:
            return bounded
    bare = [{key: item.get(key) for key in ("name", "ok", "severity", "basis")} for item in checks]
    failed = [item for item in bare if not item["ok"]]
    passed = [item for item in bare if item["ok"]]
    kept, used = [], 2
    for group, label in ((failed, "failed"), (passed, "passed")):
        for index, item in enumerate(group):
            # Room is left for the marker that counts what this and any later group lose.
            if used + encoded_size(item) + 1 + 60 > limit:
                omitted = len(group) - index + (len(passed) if group is failed else 0)
                left_out = f"{len(group) - index} more {label} checks omitted"
                if group is failed and passed:
                    left_out += f"; {len(passed)} passed checks omitted"
                return [*kept, {"omitted_checks": omitted, "note": left_out}]
            kept.append(item)
            used += encoded_size(item) + 1
    return kept


def review_view(bundle: dict, checks: list[dict] | None = None, *, limit: int = REVIEW_EVIDENCE_CHARS) -> dict:
    """The review bundle as the model reads it, within ``limit`` encoded characters.

    The target row loses artifact_json (the parsed artifact is beside it), the artifact loses per-call telemetry and
    audit events lose their hash chain. The checks are kept whole, except that their expected/actual values are
    bounded; everything else is clipped progressively into the budget the checks leave. Last of all only the
    checks, the target's identity and as many of the artifact's citations as fit remain."""
    target = {key: value for key, value in (bundle.get("target") or {}).items() if key != "artifact_json"}
    artifact = bundle.get("artifact") if isinstance(bundle.get("artifact"), dict) else {}
    findings = artifact.get("findings")
    if isinstance(findings, dict):
        artifact = {**artifact, "findings": {key: value for key, value in findings.items()
                                             if key not in INTERNAL_FINDINGS}}
    rest = {"target": target, "artifact": artifact, "source": bundle.get("source") or {},
            "history": history_view(bundle.get("history") or []), "flag": bundle.get("flag"),
            "simulated": bundle.get("simulated")}
    if bundle.get("resolution"):
        rest["resolution"] = bundle["resolution"]
    head = {} if checks is None else {"checks": bounded_checks(checks, limit // 2)}
    for text, items in ((100_000, 1000), (2000, 60), (600, 30), (200, 15), (80, 8)):
        clipped = clip(rest, text=text, items=items)
        if encoded_size({**head, **clipped}) <= limit:
            return {**head, **clipped}
    keep = ("task_id", "case_id", "agent", "skill", "a2a_state", "business_status", "outcome", "confidence")
    identity = {key: clip(target.get(key), text=100, items=1) for key in keep}
    policies = [item for item in artifact.get("policy_ids") or [] if isinstance(item, str)][:20]
    minimal = {**head, "target": identity, "artifact": {
        "outcome": clip(artifact.get("outcome"), text=100, items=1),
        "confidence": artifact.get("confidence") if isinstance(artifact.get("confidence"), (int, float)) else None,
        "policy_ids": [item[:40] for item in policies], "evidence_refs": []}, "details_omitted": True}
    # Whatever the checks and identity leave goes to citations, whole ones only, so the view never passes `limit`.
    minimal["artifact"]["evidence_refs"] = capped_refs(artifact.get("evidence_refs"), limit - encoded_size(minimal))
    return minimal


def brief(value, limit: int = 80) -> str:
    """Compact, bounded rendering of a check value; long or nested values stay in the review row."""
    text = json.dumps(value, default=str, separators=(",", ":"))
    return text if len(text) <= limit else "[see review]"


def lesson_text(skill: str | None, failures: list[dict]) -> str:
    """Code-generated memory naming each failed control; no model or document text is included."""
    parts = []
    for item in failures[:6]:
        detail = f"{item['name']}: expected {brief(item['expected'])}, got {brief(item['actual'])}"
        if item.get("basis"):
            detail += f" ({item['basis']})"
        parts.append(detail)
    if len(failures) > 6:
        parts.append(f"and {len(failures) - 6} more")
    text = f"Before finalizing {skill or 'this skill'}, recheck against the source: " + "; ".join(parts) + \
        ". Missing evidence remains unresolved."
    return text[:1000]


def grounded_issues(issues: list[str], checks: list[dict]) -> tuple[list[str], list[str]]:
    """Model-reported issues count only when they name a failed issue-severity check.

    The model cannot raise a verdict on its own: advisories stay advisory and an evidence
    mention is commentary, not a contradiction. Grounded issues add explanation to a failure
    the deterministic checks already established."""
    anchors = {item["name"] for item in checks if not item["ok"] and item.get("severity", ISSUE) == ISSUE}
    patterns = [re.compile(rf"(?<![A-Za-z0-9_]){re.escape(anchor)}(?![A-Za-z0-9_])", re.IGNORECASE)
                for anchor in anchors]
    grounded = [issue for issue in issues if any(pattern.search(issue) for pattern in patterns)]
    return grounded, [issue for issue in issues if issue not in grounded]


# Mirrors validateAutomatedInstruction in the SpacetimeDB module, which enforces the same bounds.
CONTROL_WORDS = re.compile(r"\b(ignore|disregard|override|bypass|disable|allowlist|permissions?|credentials?"
                           r"|passwords?|tokens?|api keys?)\b", re.IGNORECASE)
MAX_LEARNED_LINES = 8


def plain_text(value, limit: int) -> str:
    """One printable-ASCII line: model-drafted learning never carries markup, control characters or newlines."""
    text = " ".join(str(value or "").split())
    return "".join(ch for ch in text if " " <= ch <= "~")[:limit].strip()


def drafted_lesson(draft: dict) -> str:
    text = plain_text(draft.get("lesson"), 900)
    return text if len(text) >= 20 and not CONTROL_WORDS.search(text) else ""


def learned_line(draft: dict) -> str:
    text = plain_text(draft.get("instruction"), 290)
    return f"Learned: {text}" if len(text) >= 10 and not CONTROL_WORDS.search(text) else ""


def instruction_proposal(original: str, line: str = "") -> str:
    """Generate a bounded append-only patch: the code-owned reminder, or one model-drafted learned line."""
    line = line or "Before completing a result, cite the source records and recheck monetary totals in exact cents."
    lines = original.splitlines()
    if line in lines or (line.startswith("Learned: ") and
                         sum(x.startswith("Learned: ") for x in lines) >= MAX_LEARNED_LINES):
        return ""
    revised = original + f"\n{line}\n"
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True), revised.splitlines(keepends=True),
                                       fromfile="a/instructions.md", tofile="b/instructions.md", n=0))
    if apply_instruction_patch(original, patch) != revised:
        raise ValueError("generated instruction patch failed validation")
    return patch
