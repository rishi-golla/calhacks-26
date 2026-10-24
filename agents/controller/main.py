import os
from decimal import Decimal

from agents.controller.card import DESCRIPTIONS
from agents.risk.tools import audit_charges
from common.agent_support import child_envelope, evidence, finish, identifier, money, mutate, number, rows
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import FreightAgent
from common.server import serve
from common.source_repository import SourceRepository, SourceUnavailable, fixture_mode


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

    async def handle(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str, updater):
        source = await SourceRepository().load(env)
        refs = evidence(source, env)
        if env.task_type == "check_already_paid":
            paid = money(source.get("paid_amount", 0))
            return await finish(self, env, task_id, outcome="verified", findings={
                "paid_amount": number(paid), "already_paid": paid >= money(source["invoice_amount"])},
                evidence_refs=refs)
        if env.task_type == "reconcile":
            lines = source.get("journal_lines", [])
            debit = sum((money(line.get("debit", 0)) for line in lines), Decimal(0))
            credit = sum((money(line.get("credit", 0)) for line in lines), Decimal(0))
            return await finish(self, env, task_id, outcome="verified" if lines and debit == credit else "unverified",
                                findings={"debits": number(debit), "credits": number(credit),
                                          "balanced": bool(lines) and debit == credit},
                                policy_ids=["P-006"], evidence_refs=refs)
        if env.task_type == "approve_entry":
            request_id = env.subject.get("request_id")
            if not request_id:
                raise SourceUnavailable("approve_entry requires an existing journal request reference")
            requests = [source["journal_request"]] if fixture_mode() and source.get("journal_request") else (
                [] if fixture_mode() else await rows(self.name, "journal_request", "id", request_id))
            if not requests or requests[0].get("id") != request_id or requests[0].get("case_id") != env.case_id:
                raise SourceUnavailable("matching source journal request was not found")
            request = requests[0]
            if request.get("posted_entry_id"):
                return await finish(self, env, task_id, outcome="verified", findings={
                    "already_posted": True, "journal_entry_id": request["posted_entry_id"]}, evidence_refs=refs)
            if request.get("requested_by") == self.name:
                return await finish(self, env, task_id, outcome="reject", findings={"reason": "requester is approver"},
                                    policy_ids=["P-005"], evidence_refs=refs)
            journal = await mutate(self, env, task_id, "post_journal_entry", {
                "id": identifier(request_id, "journal"), "request_id": request_id,
                "lines": request["lines"], "period": request["period"],
                "requested_by": request["requested_by"],
            })
            return await finish(self, env, task_id, outcome="approve" if journal.get("decision", "allow") == "allow"
                                else "reject", findings={"journal": journal},
                                policy_ids=["P-005", "P-006"], evidence_refs=refs)
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
            if money(hold["amount"]) > 5000:
                decision = await self.approval(env, task_id, source, "release_hold", hold_id, release)
                if not decision["approved"]:
                    return await finish(self, env, task_id, outcome="reject", findings=decision,
                                        evidence_refs=refs, policy_ids=["P-003"])
                payload["approval_id"] = decision["approval_id"]
            released = await mutate(self, env, task_id, "release_hold", payload)
            return await finish(self, env, task_id, outcome="approve" if released.get("decision", "allow") == "allow"
                                else "unverified", findings={"release": released},
                                evidence_refs=refs)
        unsupported = money(audit_charges(source)["unsupported_amount"])
        if amount <= contract * Decimal("1.02") and not unsupported and not source.get("bank_change_requested"):
            return await finish(self, env, task_id, outcome="clear", findings={"hold_required": False},
                                evidence_refs=refs)
        hold_amount = amount if source.get("bank_change_requested") else max(amount - contract, unsupported)
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
                            "hold_amount": number(hold_amount), "payment_blocked": True},
                            policy_ids=["P-004"] if source.get("bank_change_requested") else ["P-001", "P-002"],
                            evidence_refs=refs)


if __name__ == "__main__":
    serve(ControllerAgent(), DESCRIPTIONS)
