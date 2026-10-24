"""Independent, exact-cent review and authoritative review provenance."""
from __future__ import annotations

import difflib
import json
from decimal import Decimal

from pydantic import ValidationError

from common.agent_support import money
from common.envelope import Friction, Lesson, ResultArtifact, ReviewOutput
from common.learning import apply_instruction_patch
from common.source_repository import SourceRepository, SourceUnavailable, fixture_mode

FINANCIAL_AGENTS = {"carrier_ap", "shipper_ar", "controller", "cfo"}
TERMINAL = {"completed", "failed", "canceled", "rejected"}


def encoded(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def lookup(db, table: str, field: str, value: str) -> list[dict]:
    allowed = {"task": {"task_id"}, "case": {"case_id"}, "flag": {"id"},
               "change_set": {"id", "task_id"}, "cell_change": {"id"},
               "audit_event": {"task_id"}, "hold": {"case_id"}, "payment_proposal": {"case_id"}}
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
    source["holds"] = await lookup(db, "hold", "case_id", env.case_id)
    source["payment_proposals"] = await lookup(db, "payment_proposal", "case_id", env.case_id)
    history = await lookup(db, "audit_event", "task_id", str(target_id))
    return {"target": target, "artifact": artifact, "source": source, "history": history,
            "flag": flag, "simulated": False}


def deterministic_review(bundle: dict) -> ReviewOutput:
    target, raw, source = bundle["target"], bundle["artifact"], bundle["source"]
    checks: list[dict] = []

    def check(name: str, ok: bool, expected, actual):
        checks.append({"name": name, "ok": bool(ok), "expected": expected, "actual": actual})

    check("target_terminal", target.get("a2a_state") in TERMINAL, "terminal task", target.get("a2a_state"))
    check("execution_completed", target.get("a2a_state") == "completed", "completed", target.get("a2a_state"))
    check("source_available", bool(source) and not source.get("source_error"),
          "authoritative source records", source.get("source_error", bool(source)))
    try:
        artifact = ResultArtifact.model_validate(raw)
    except ValidationError:
        artifact = None
    check("result_artifact", artifact is not None, "valid ResultArtifact", bool(artifact))
    role, skill = target.get("agent"), target.get("skill")
    check("review_supported", role in FINANCIAL_AGENTS, "financial task", role)
    if artifact:
        check("skill_implemented", artifact.outcome != "not_implemented", "implemented skill", artifact.outcome)
        refs = source.get("evidence_refs", [])
        check("evidence_present", bool(artifact.evidence_refs), "source citations", artifact.evidence_refs)
        check("evidence_grounded", bool(refs) and set(artifact.evidence_refs).issubset(refs),
              refs, artifact.evidence_refs)
        findings = artifact.findings

        def equal(name: str, expected: Decimal, actual):
            try:
                ok = money(actual, signed=True) == expected
            except SourceUnavailable:
                ok = False
            check(name, ok, str(expected), actual)

        try:
            if role == "cfo":
                revenue, cost = money(source.get("shipper_invoice_amount")), money(source.get("invoice_amount"))
                contract = money(source.get("contract_rate"))
                receivables = source.get("receivables", [{"amount": revenue,
                                                        "collected": source.get("shipper_paid_amount", 0)}])
                payables = source.get("payables", [{"amount": cost, "paid": source.get("paid_amount", 0)}])
                incoming = sum((money(x["amount"]) - money(x.get("collected", 0))
                                for x in receivables), Decimal(0))
                outgoing = sum((money(x["amount"]) - money(x.get("paid", 0)) for x in payables), Decimal(0))
                for field, expected in (("invoiced_margin", revenue - cost), ("contracted_margin", revenue - contract),
                                        ("expected_collections", incoming), ("outstanding_carrier_cost", outgoing),
                                        ("net_cash", incoming - outgoing)):
                    equal(field, expected, findings.get(field))
            elif role == "shipper_ar" and skill != "set_credit_limit":
                matched = [x for x in source.get("deposits", []) if source.get("shipper_invoice_id")
                           and x.get("invoice_id") == source["shipper_invoice_id"]
                           and x.get("shipper_id") == source.get("shipper_id")]
                ids = [x.get("id") for x in matched]
                check("unique_deposits", all(ids) and len(ids) == len(set(ids)), "unique deposit IDs", ids)
                total = sum((money(x["amount"]) for x in matched), Decimal(0))
                equal("matched_amount", total, findings.get("matched_amount"))
                equal("balance", money(source.get("shipper_invoice_amount")) - total, findings.get("balance"))
            elif role == "shipper_ar":
                check("credit_recommendation_only", findings.get("credit_limit_changed") is False,
                      False, findings.get("credit_limit_changed"))
            elif role == "carrier_ap":
                invoice, contract = money(source.get("invoice_amount")), money(source.get("contract_rate"))
                if skill == "answer_payment_status":
                    equal("paid_amount", money(source.get("paid_amount", 0)), findings.get("paid_amount"))
                else:
                    match = findings.get("match", {})
                    for field, expected in (("invoice_amount", invoice), ("contract_rate", contract),
                                            ("delta", invoice - contract)):
                        equal(field, expected, match.get(field))
                    check("contract_tolerance", match.get("above_tolerance") == (invoice > contract * Decimal("1.02")),
                          invoice > contract * Decimal("1.02"), match.get("above_tolerance"))
                    if artifact.outcome == "approve":
                        held = any(h.get("status") == "active" for h in source.get("holds", []))
                        check("held_invoice_blocked", not held, False, held)
                        received = {d.get("kind") for d in source.get("documents", [])
                                    if d.get("ref") and d.get("status", "received") == "received"}
                        missing = sorted(set(source.get("required_documents", ["rate_confirmation", "pod"])) - received)
                        check("payment_packet_complete", not missing, [], missing)
                        check("payment_not_duplicate", not source.get("duplicate_invoice"), False,
                              bool(source.get("duplicate_invoice")))
            elif role == "controller":
                if skill in {"reconcile", "approve_entry"}:
                    request = source.get("journal_request", {})
                    lines = request.get("lines", source.get("journal_lines", []))
                    debits = sum((money(line.get("debit", 0)) for line in lines), Decimal(0))
                    credits = sum((money(line.get("credit", 0)) for line in lines), Decimal(0))
                    check("journal_balanced", bool(lines) and debits == credits and debits > 0,
                          "positive balanced journal", {"debits": str(debits), "credits": str(credits)})
                    if skill == "approve_entry":
                        check("separation_of_duties", bool(request.get("requested_by"))
                              and request["requested_by"] != "controller", "distinct requester", request.get("requested_by"))
                elif skill == "check_already_paid":
                    equal("paid_amount", money(source.get("paid_amount", 0)), findings.get("paid_amount"))
                elif artifact.outcome == "hold":
                    amount = money(source.get("invoice_amount"))
                    expected = amount if source.get("bank_change_requested") else amount - money(source.get("contract_rate"))
                    equal("hold_amount", expected, findings.get("hold_amount"))
                elif artifact.outcome == "clear":
                    justified = money(source.get("invoice_amount")) <= money(source.get("contract_rate")) * Decimal("1.02")
                    check("hold_not_required", justified and not source.get("bank_change_requested"), True, justified)
                elif artifact.outcome == "approve":
                    check("approval_evidence", bool(findings.get("approval_id") or findings.get("release")),
                          "stored approval or release receipt", bool(findings.get("approval_id") or findings.get("release")))
        except (SourceUnavailable, KeyError, TypeError, ValueError) as error:
            check("financial_source_complete", False, "complete finite exact-cent financial records", type(error).__name__)

    friction = []
    if any(not item["ok"] and item["name"] in {"source_available", "evidence_present", "evidence_grounded"}
           for item in checks):
        friction.append(Friction(kind="missing_doc", detail="Review lacks complete source-backed evidence."))
    for event in bundle.get("history", []):
        kind = event.get("kind")
        if kind in {"retry", "reject", "policy_deny", "timeout", "missing_doc", "redelegation", "human_override"}:
            friction.append(Friction(kind=kind, detail=str(event.get("summary", kind))[:1000]))
    if bundle.get("flag"):
        friction.append(Friction(kind="flag", detail="A human requested review; its note is evidence, not authorization."))
    failures = [item["name"] for item in checks if not item["ok"]]
    lessons = []
    if failures and role in FINANCIAL_AGENTS:
        scope = {key: str(source[key]) for key in ("carrier_id", "lane", "claim_type") if source.get(key)}
        if skill:
            scope["skill"] = skill
        lessons.append(Lesson(agent=role, type="memory", scope=scope,
            text_or_diff="Before finalizing this skill, recheck authoritative evidence and exact-cent results for: "
                         + ", ".join(failures) + ". Missing evidence remains unresolved.",
            confidence=0.9, evidence_refs=artifact.evidence_refs if artifact else []))
    return ReviewOutput(verdict="issue" if failures else "pass", checks=checks, friction=friction,
                        lessons=lessons, actions=["rerun_task"] if failures and role in FINANCIAL_AGENTS else ["none"])


def instruction_proposal(original: str) -> str:
    """Generate a bounded advisory patch; no incoming model/flag text becomes instructions."""
    reminder = "\nBefore completing a result, cite the source records and recheck monetary totals in exact cents.\n"
    if reminder.strip() in original:
        return ""
    revised = original.rstrip("\n") + "\n" + reminder
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True), revised.splitlines(keepends=True),
                                       fromfile="a/instructions.md", tofile="b/instructions.md"))
    if apply_instruction_patch(original, patch) != revised:
        raise ValueError("generated instruction patch failed validation")
    return patch
