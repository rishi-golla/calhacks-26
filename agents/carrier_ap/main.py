from agents.carrier_ap.card import DESCRIPTIONS
from agents.carrier_ap.tools import duplicate_invoices, match_workings, three_way_match
from agents.risk.tools import audit_charges, charge_workings
from common.agent_support import child_envelope, evidence, finish, identifier, money, mutate, number, rows
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import FreightAgent
from common.server import serve
from common.source_repository import SourceRepository, SourceUnavailable, source_fixtures_only
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
        proposal_id = identifier(task_id, "payment_proposal")
        payload = {"id": proposal_id, "invoice_id": invoice_id, "amount": number(amount)}
        if money(source["invoice_amount"]) > 5000:
            decision = await self.delegate("review_hold", child_envelope(env, "review_hold",
                requested_action="propose_payment", target_id=proposal_id), meta, task_id,
                invocation_key="payment_approval")
            findings = decision[-1].get("findings", {}) if decision else {}
            if decision and decision[-1].get("outcome") == "reject":
                return await finish(self, env, task_id, outcome="reject", findings={"approval": decision, "workings": workings},
                                    policy_ids=["P-003"], evidence_refs=refs)
            if not findings.get("approval_id"):
                return await finish(self, env, task_id, outcome="unverified", findings={"approval": decision, "workings": workings},
                                    policy_ids=["P-003"], evidence_refs=refs)
            payload["approval_id"] = findings["approval_id"]
        proposal = await mutate(self, env, task_id, "propose_payment", payload)
        return await finish(self, env, task_id, outcome="approve" if proposal.get("decision", "allow") == "allow"
                            else "unverified", findings={"match": match, "workings": workings, "payment_proposal": proposal},
                            evidence_refs=refs, policy_ids=proposal.get("policy_ids", []))


if __name__ == "__main__":
    serve(CarrierAPAgent(), DESCRIPTIONS)
