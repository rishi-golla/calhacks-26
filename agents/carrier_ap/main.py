from agents.carrier_ap.card import DESCRIPTIONS
from agents.carrier_ap.tools import duplicate_invoices, match_workings, three_way_match
from agents.risk.tools import audit_charges, charge_workings
from common.agent_support import (
    child_envelope,
    evidence,
    finish,
    identifier,
    journal_requests,
    money,
    mutate,
    number,
    payable_journal_id,
    payable_lines,
    request_journal,
    rows,
)
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import DelegationRefused, FreightAgent
from common.server import serve
from common.source_repository import SourceRepository, SourceUnavailable, source_fixtures_only
from common.spacetime_client import REDUCER_REFUSED_STATUS, SpacetimeHTTPError
from common.workings import refs_for, working


class CarrierAPAgent(FreightAgent):
    name = "carrier_ap"

    async def handle(self, env, meta, task_id, updater):
        from common.providers import mode
        if mode() == "live":
            from common.tool_loop import run_tool_loop
            return await run_tool_loop(self, env, meta, task_id)
        return await self.run_workflow(env, meta, task_id, updater)

    async def run_workflow(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str, updater):
        source = await SourceRepository().load(env)
        refs = evidence(source, env)
        if env.task_type == "answer_payment_status":
            proposals = source.get("payment_proposals", []) if source_fixtures_only() else await rows(
                self.name, "payment_proposal", "case_id", env.case_id)
            return await finish(self, env, task_id, outcome="verified",
                                findings={"payment_proposals": proposals,
                                          "paid_amount": source.get("paid_amount", 0)}, evidence_refs=refs)
        if env.task_type not in {"process_carrier_invoice", "propose_payment"}:
            raise ValueError("unsupported Carrier Settlement skill")
        if source.get("invoice_amount") is None and not source.get("invoice_id"):
            # Delivery evidence can precede the carrier's bill; there is nothing to settle yet.
            return await finish(self, env, task_id, outcome="unverified", findings={
                "awaiting_invoice": True, "load_id": source.get("load_id")}, evidence_refs=refs)
        match = three_way_match(source)
        workings = match_workings(source, match)
        duplicates = duplicate_invoices(source)
        if duplicates:
            return await finish(self, env, task_id, outcome="reject", findings={"match": match, "workings": workings,
                                "duplicate_invoice_ids": duplicates}, policy_ids=["P-007"], evidence_refs=refs)
        if source.get("carrier_identity_mismatch"):
            # The invoice names a different carrier than the one tendered: possible double brokering.
            return await finish(self, env, task_id, outcome="hold", findings={
                "match": match, "workings": workings, "carrier_identity": source.get("carrier_identity"),
                "payment_blocked": True},
                policy_ids=["P-002"], evidence_refs=refs)
        if match["missing_documents"]:
            documents = await self.delegate("collect_documents", child_envelope(
                env, "collect_documents", required_documents=match["missing_documents"]), meta, task_id,
                invocation_key="settlement_packet")
            if not documents or documents[-1].get("outcome") != "verified":
                return await finish(self, env, task_id, outcome="unverified", findings={
                    "match": match, "workings": workings, "documents": documents}, evidence_refs=refs)
        if not match["totals_match"]:
            return await finish(self, env, task_id, outcome="reject", findings={"match": match, "workings": workings},
                                policy_ids=["P-010"], evidence_refs=refs)
        charge_checks = audit_charges(source)
        workings.extend(charge_workings(source, charge_checks))
        if source.get("bank_change_requested") or match["above_tolerance"] or charge_checks["needs_verification"]:
            skill = "screen_bank_change" if source.get("bank_change_requested") else "investigate_charge"
            risk = await self.delegate(skill, child_envelope(env, skill),
                                       meta, task_id, invocation_key="charge_audit")
            controller = await self.delegate("review_hold", child_envelope(env, "review_hold"),
                                             meta, task_id, invocation_key="settlement_decision")
            held = controller and controller[-1].get("outcome") == "hold"
            return await finish(self, env, task_id, outcome="hold" if held else "unverified", findings={
                "match": match, "workings": workings, "risk": risk, "controller": controller,
                "undisputed_amount": match["contract_rate"], "payment_blocked": bool(held)},
                policy_ids=["P-004", "P-002"] if source.get("bank_change_requested") else ["P-001", "P-002"],
                evidence_refs=refs)
        invoice_id = source.get("invoice_id")
        if not invoice_id:
            raise SourceUnavailable("source invoice_id is required for a payment proposal")
        holds = source.get("holds", []) if source_fixtures_only() else await rows(
            self.name, "hold", "invoice_id", invoice_id)
        if any(hold.get("status") == "active" for hold in holds):
            return await finish(self, env, task_id, outcome="hold", findings={"match": match, "workings": workings,
                                "payment_blocked": True}, policy_ids=["P-002"], evidence_refs=refs)
        amount = money(source["invoice_amount"]) - money(source.get("paid_amount", 0))
        workings.append(working("Open balance to pay", "invoice_amount - paid_amount",
                                {"invoice_amount": source["invoice_amount"], "paid_amount": source.get("paid_amount", 0)},
                                amount, evidence_refs=refs_for(source, "invoice_amount"),
                                basis="pay once, never the disputed delta"))
        if amount <= 0:
            return await finish(self, env, task_id, outcome="clear", findings={"already_paid": True, "workings": workings},
                                evidence_refs=refs)
        # A rerun (for example after a Validator review) must not propose the same invoice twice: the module
        # refuses a proposal beyond the invoice's available balance, which used to fail the rerun outright.
        existing = source.get("payment_proposals", []) if source_fixtures_only() else await rows(
            self.name, "payment_proposal", "invoice_id", invoice_id)
        proposed = sum((money(row.get("amount", 0)) for row in existing if row.get("status") != "cancelled"),
                       money(0))
        if proposed >= amount:
            current = next((row for row in existing if row.get("status") != "cancelled"), {})
            workings.append(working("Already proposed for this invoice", "sum(payment_proposal.amount)",
                                    {"proposed": number(proposed), "open_balance": number(amount)}, proposed,
                                    basis="pay once: reuse the standing proposal"))
            findings = {"match": match, "workings": workings, "payment_proposal": {
                "id": current.get("id", ""), "decision": "allow", "reused": True}}
            findings.update(await self.book_payable(env, meta, task_id, source, invoice_id, amount))
            return await finish(self, env, task_id, outcome="approve", findings=findings, evidence_refs=refs,
                                policy_ids=["P-002"])
        proposal_id = identifier(task_id, "payment_proposal")
        payload = {"id": proposal_id, "invoice_id": invoice_id, "amount": number(amount)}
        if money(source["invoice_amount"]) > 5000:
            decision = await self.delegate("review_hold", child_envelope(env, "review_hold",
                requested_action="propose_payment", target_id=proposal_id), meta, task_id,
                invocation_key="payment_approval")
            approved = decision[-1].get("findings", {}) if decision else {}
            if decision and decision[-1].get("outcome") == "reject":
                return await finish(self, env, task_id, outcome="reject", findings={"approval": decision, "workings": workings},
                                    policy_ids=["P-003"], evidence_refs=refs)
            if not approved.get("approval_id"):
                return await finish(self, env, task_id, outcome="unverified", findings={"approval": decision, "workings": workings},
                                    policy_ids=["P-003"], evidence_refs=refs)
            payload["approval_id"] = approved["approval_id"]
        proposal = await mutate(self, env, task_id, "propose_payment", payload)
        findings = {"match": match, "workings": workings, "payment_proposal": proposal}
        allowed = proposal.get("decision", "allow") == "allow"
        if allowed:
            findings.update(await self.book_payable(env, meta, task_id, source, invoice_id, amount))
        return await finish(self, env, task_id, outcome="approve" if allowed else "unverified", findings=findings,
                            evidence_refs=refs, policy_ids=proposal.get("policy_ids", []))

    async def book_payable(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str, source: dict,
                           invoice_id: str, amount) -> dict:
        """Accrue the proposed (undisputed) carrier cost once per invoice: Dr freight expense / Cr AP.

        Nothing is ever paid, so no payment entry follows and AP stays open. Settlement Approvals posts the
        request; that delegation is at most the third beside the packet chase and the payment approval, and a
        depth, cycle or fan-out refusal leaves the request pending for a later run instead of failing settlement.
        """
        request_id = payable_journal_id(invoice_id)
        period = source.get("period") or str(source.get("issued_at") or "")[:7]
        if not period:
            return {"payable_journal": {"request_id": request_id, "requested": False,
                                        "reason": "journal period is required"}}
        existing = await journal_requests(self.name, env, source, request_id)
        try:
            result = await request_journal(self, env, task_id, existing, request_id, period=period,
                                           lines=payable_lines(amount), memo="Carrier settlement: " + str(invoice_id),
                                           label="payable")
        except SpacetimeHTTPError as error:
            # The proposal is already stored, so a refused journal (e.g. the invoice's period is missing or
            # locked) must not fail settlement: that would strand the proposal, whose reserved balance blocks
            # any retry. Report the bill as not booked; the validator flags it for a person.
            if error.status_code != REDUCER_REFUSED_STATUS:
                raise
            return {"payable_journal": {"request_id": request_id, "requested": False, "amount": number(amount),
                                        "period": period, "reason": error.reason or str(error)}}
        booked = {"request_id": request_id, "requested": True, "amount": number(amount), "period": period}
        posted = [result["posted_entry_id"]] if result["posted_entry_id"] else []
        if result["pending"]:
            try:
                posted.extend(await self.delegate("approve_entry", child_envelope(
                    env, "approve_entry", request_ids=[request_id]), meta, task_id,
                    invocation_key="post_payable_journal"))
            except DelegationRefused as error:
                booked["posting_deferred"] = str(error)
        return {"payable_journal": booked,
                "journal_requests": [result["receipt"]] if result["receipt"] else [], "journal_posting": posted}


if __name__ == "__main__":
    serve(CarrierAPAgent(), DESCRIPTIONS)
