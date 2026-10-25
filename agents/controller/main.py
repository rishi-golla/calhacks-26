import os
from decimal import Decimal

from agents.controller.card import DESCRIPTIONS
from agents.risk.tools import audit_charges, charge_workings
from common import policy
from common.agent_support import (
    child_envelope,
    evidence,
    finish,
    identifier,
    journal_lines,
    money,
    mutate,
    number,
    rows,
)
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import FreightAgent
from common.server import serve
from common.source_repository import SourceRepository, SourceUnavailable, fixture_mode
from common.workings import refs_for, working


def journal_working(label: str, lines: list[dict], debit: Decimal, credit: Decimal) -> dict:
    inputs = {f"{line.get('account', 'line')}_{i + 1}": {"debit": line.get("debit", 0), "credit": line.get("credit", 0)}
              for i, line in enumerate(lines)}
    return working(label, "sum(debits) == sum(credits)", inputs, {"debits": debit, "credits": credit,
                   "balanced": bool(lines) and debit == credit}, unit="text", basis="P-006")


class ControllerAgent(FreightAgent):
    name = "controller"

    async def approval(self, env, task_id, source, action, target_id, amount):
        approval_id = identifier(task_id, "approval:" + action + ":" + target_id)
        stored = [] if fixture_mode() else await rows(self.name, "approval", "id", approval_id)
        if stored:
            approval = stored[0]
            if approval.get("decision") == "rejected":
                return {"approval_id": approval_id, "approved": False, "rejected": True}
            if approval.get("decision") == "approved" and approval.get("code_ok") is True:
                return {"approval_id": approval_id, "approved": True}
        approver = os.getenv("HUMAN_APPROVER_ID")
        if not approver:
            await self.park(task_id, "awaiting_human_approval", "approval", approval_id,
                            details={"action": action, "amount": number(amount),
                                     "reason": "designated human approver is not configured"})
        if not stored:
            await mutate(self, env, task_id, "request_approval", {
                "id": approval_id, "action": action, "target_id": target_id,
                "invoice_id": source["invoice_id"], "amount": number(amount), "approver": approver,
            })
        await self.park(task_id, "awaiting_human_approval", "approval", approval_id,
                        details={"action": action, "amount": number(amount), "approval_id": approval_id})

    async def handle(self, env, meta, task_id, updater):
        from common.providers import mode
        if mode() == "live":
            from common.tool_loop import run_tool_loop
            return await run_tool_loop(self, env, meta, task_id)
        return await self.run_workflow(env, meta, task_id, updater)

    async def run_workflow(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str, updater):
        source = await SourceRepository().load(env)
        refs = evidence(source, env)
        if env.task_type == "check_already_paid":
            paid = money(source.get("paid_amount", 0))
            return await finish(self, env, task_id, outcome="verified", findings={
                "paid_amount": number(paid), "already_paid": paid >= money(source["invoice_amount"])},
                evidence_refs=refs)
        if env.task_type == "reconcile":
            lines = journal_lines(source.get("journal_lines", []))
            debit = sum((money(line.get("debit", 0)) for line in lines), Decimal(0))
            credit = sum((money(line.get("credit", 0)) for line in lines), Decimal(0))
            return await finish(self, env, task_id, outcome="verified" if lines and debit == credit else "unverified",
                                findings={"debits": number(debit), "credits": number(credit),
                                          "balanced": bool(lines) and debit == credit,
                                          "workings": [journal_working("Journal balances", lines, debit, credit)]},
                                policy_ids=["P-006"], evidence_refs=refs)
        if env.task_type == "approve_entry":
            request_ids = env.subject.get("request_ids")
            if request_ids is None and env.subject.get("request_id"):
                request_ids = [env.subject["request_id"]]
            if not isinstance(request_ids, list) or not request_ids or len(set(request_ids)) != len(request_ids) \
                    or any(not isinstance(item, str) or not item for item in request_ids):
                raise SourceUnavailable("approve_entry requires existing journal request references")
            # Resolve every request before posting any, so a missing one cannot leave a partial batch.
            requests = []
            for request_id in request_ids:
                if fixture_mode():
                    recorded = [*source.get("journal_requests", []), *filter(None, [source.get("journal_request")])]
                    found = [item for item in recorded if item.get("id") == request_id][:1]
                else:
                    found = await rows(self.name, "journal_request", "id", request_id)
                if not found or found[0].get("id") != request_id or found[0].get("case_id") != env.case_id:
                    raise SourceUnavailable("matching source journal request was not found")
                requests.append(found[0])
            entries, policy_ids, workings = [], set(), []
            for request in requests:
                entry = {"request_id": request["id"]}
                if request.get("posted_entry_id"):
                    entry.update(outcome="verified", already_posted=True, journal_entry_id=request["posted_entry_id"])
                elif request.get("requested_by") == self.name:
                    entry.update(outcome="reject", reason="requester is approver")
                    policy_ids.add("P-005")
                else:
                    lines = journal_lines(request["lines"])
                    debit = sum((money(line.get("debit", 0)) for line in lines), Decimal(0))
                    credit = sum((money(line.get("credit", 0)) for line in lines), Decimal(0))
                    workings.append(journal_working(f"Entry {request['id'][:12]} balances", lines, debit, credit))
                    journal = await mutate(self, env, task_id, "post_journal_entry", {
                        "id": identifier(request["id"], "journal"), "request_id": request["id"],
                        "lines": lines, "period": request["period"],
                        "requested_by": request["requested_by"],
                    })
                    entry.update(outcome="approve" if journal.get("decision", "allow") == "allow" else "reject",
                                 journal=journal)
                    policy_ids.update({"P-005", "P-006"})
                entries.append(entry)
            outcomes = {entry["outcome"] for entry in entries}
            outcome = "reject" if outcomes == {"reject"} else "partial" if "reject" in outcomes else \
                "approve" if "approve" in outcomes else "verified"
            return await finish(self, env, task_id, outcome=outcome,
                                findings={"entries": entries, "workings": workings},
                                policy_ids=sorted(policy_ids), evidence_refs=refs)
        if env.task_type != "review_hold":
            raise ValueError("unsupported Settlement Approvals skill")
        amount = money(source["invoice_amount"])
        contract = money(source["contract_rate"])
        action = env.subject.get("requested_action")
        if action == "propose_payment":
            target_id = env.subject.get("target_id")
            if not target_id or meta.from_agent != "carrier_ap":
                raise SourceUnavailable("payment approval requires a Carrier Settlement proposal reference")
            decision = await self.approval(env, task_id, source, "propose_payment", target_id,
                                           amount - money(source.get("paid_amount", 0)))
            return await finish(self, env, task_id, outcome="approve" if decision["approved"] else "reject",
                                findings=decision, policy_ids=["P-003", "P-005"], evidence_refs=refs)
        if action == "release_hold":
            hold_id = env.subject.get("hold_id")
            if not isinstance(hold_id, str) or not hold_id:
                raise SourceUnavailable("release_hold requires a stored hold reference")
            holds = source.get("holds", []) if fixture_mode() else await rows(self.name, "hold", "id", hold_id)
            holds = [hold for hold in holds if hold.get("id") == hold_id and hold.get("case_id") == env.case_id]
            if not holds:
                raise SourceUnavailable("hold does not exist for this case")
            hold = holds[0]
            release = money(hold["amount"]) - money(hold.get("released_amount", 0))
            if release <= 0:
                return await finish(self, env, task_id, outcome="clear", findings={"already_released": True},
                                    evidence_refs=refs)
            if source.get("release_verified") is not True:
                return await finish(self, env, task_id, outcome="unverified", findings={
                    "reason": "source evidence does not authorize releasing the hold"}, evidence_refs=refs)
            payload = {"id": hold_id, "amount": number(release)}
            # P-003 counts every hold ever placed on this invoice, so split holds still need a human.
            if fixture_mode():
                invoice_holds = [item for item in source.get("holds", [])
                                 if item.get("invoice_id", hold.get("invoice_id")) == hold.get("invoice_id")]
            else:
                invoice_holds = await rows(self.name, "hold", "invoice_id", hold["invoice_id"])
            if policy.release_hold_needs_approval({
                    "amount": release, "hold_amount": hold["amount"],
                    "invoice_hold_total": policy.invoice_hold_exposure(invoice_holds)}):
                decision = await self.approval(env, task_id, source, "release_hold", hold_id, release)
                if not decision["approved"]:
                    return await finish(self, env, task_id, outcome="reject", findings=decision,
                                        evidence_refs=refs, policy_ids=["P-003"])
                payload["approval_id"] = decision["approval_id"]
            released = await mutate(self, env, task_id, "release_hold", payload)
            return await finish(self, env, task_id, outcome="approve" if released.get("decision", "allow") == "allow"
                                else "unverified", findings={"release": released},
                                evidence_refs=refs)
        charges = audit_charges(source)
        unsupported = money(charges["unsupported_amount"])
        bank = bool(source.get("bank_change_requested"))
        workings = charge_workings(source, charges)
        workings.append(working("Hold required", "bank_change or invoice_amount > contract_rate * 1.02 or unsupported > 0",
                                {"bank_change": bank, "invoice_amount": amount, "contract_rate": contract,
                                 "unsupported": unsupported},
                                bank or amount > contract * Decimal("1.02") or unsupported > 0, unit="bool",
                                evidence_refs=refs_for(source, "invoice_amount", "contract_rate"),
                                basis="P-001/P-004"))
        if amount <= contract * Decimal("1.02") and not unsupported and not bank:
            return await finish(self, env, task_id, outcome="clear",
                                findings={"hold_required": False, "workings": workings}, evidence_refs=refs)
        hold_amount = amount if bank else max(amount - contract, unsupported)
        workings.append(working("Amount to hold", "invoice_amount if bank_change else max(invoice_amount - contract_rate, unsupported)",
                                {"bank_change": bank, "invoice_amount": amount, "contract_rate": contract,
                                 "unsupported": unsupported}, hold_amount,
                                evidence_refs=refs_for(source, "invoice_amount", "contract_rate"),
                                basis="hold only the disputed money; the undisputed amount still waits on P-002"))
        reason = "UNVERIFIED_BANK_CHANGE" if source.get("bank_change_requested") else "UNSUPPORTED_ACCESSORIAL"
        hold = await mutate(self, env, task_id, "place_hold", {
            "id": identifier(task_id, "hold"), "invoice_id": source["invoice_id"],
            "amount": number(hold_amount), "reason_code": reason,
        })
        cash = await self.delegate("assess_financial_impact", child_envelope(env, "assess_financial_impact"),
                                   meta, task_id, invocation_key="settlement_cash_impact")
        await mutate(self, env, task_id, "update_case", {"status": "resolved", "reason": reason})
        if source.get("carrier_id") and source.get("claim_type") and refs:
            await mutate(self, env, task_id, "add_precedent", {
                "id": identifier(task_id, "precedent"), "carrier_id": source["carrier_id"],
                "claim_type": source["claim_type"], "lane": source.get("lane", ""),
                "pattern_json": {"free_time_min": source.get("free_time_min"),
                                 "receipt_attached": source.get("receipt_attached"),
                                 "source_evidence": refs}, "outcome": "hold", "source_case": env.case_id,
            })
        return await finish(self, env, task_id, outcome="hold", findings={"hold": hold, "cash_impact": cash,
                            "hold_amount": number(hold_amount), "payment_blocked": True, "workings": workings},
                            policy_ids=["P-004"] if source.get("bank_change_requested") else ["P-001", "P-002"],
                            evidence_refs=refs)


if __name__ == "__main__":
    serve(ControllerAgent(), DESCRIPTIONS)
