from decimal import Decimal

from agents.carrier_ap.tools import deposit_inputs
from agents.shipper_ar.card import DESCRIPTIONS
from common.agent_support import child_envelope, evidence, finish, identifier, money, mutate, number, rows
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import FreightAgent
from common.server import serve
from common.source_repository import SourceRepository, SourceUnavailable, fixture_mode, source_fixtures_only
from common.workings import refs_for, working


class ShipperArAgent(FreightAgent):
    name = "shipper_ar"

    async def handle(self, env, meta, task_id, updater):
        from common.providers import mode
        if mode() == "live":
            from common.tool_loop import run_tool_loop
            return await run_tool_loop(self, env, meta, task_id)
        return await self.run_workflow(env, meta, task_id, updater)

    async def run_workflow(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str, updater):
        source = await SourceRepository().load(env)
        refs = evidence(source, env)
        if env.task_type == "set_credit_limit":
            credit = source.get("credit_report")
            if not isinstance(credit, dict) or not credit.get("source_ref"):
                return await finish(self, env, task_id, outcome="unverified", findings={
                    "credit_limit_changed": False, "reason": "a sourced credit report is required"}, evidence_refs=refs)
            return await finish(self, env, task_id, outcome="verified", findings={
                "credit_limit_changed": False, "recommendation_only": True, "credit_report": credit,
                "reason": "persistent credit-limit changes require an agreed source-system interface"},
                evidence_refs=refs + [credit["source_ref"]])
        if env.task_type not in {"reconcile_customer_payment", "chase_overdue"}:
            raise ValueError("unsupported Shipper Billing & Collections skill")
        owed = money(source.get("shipper_invoice_amount"))
        deposits = source.get("deposits", [])
        if not isinstance(deposits, list):
            raise SourceUnavailable("source deposits must be a list")
        invoice_id = source.get("shipper_invoice_id")
        matched = [deposit for deposit in deposits if deposit.get("invoice_id") == invoice_id
                   and deposit.get("shipper_id") == source.get("shipper_id") and invoice_id]
        seen, total = set(), Decimal(0)
        for deposit in matched:
            deposit_id = deposit.get("id")
            if not deposit_id or deposit_id in seen:
                raise SourceUnavailable("duplicate or missing source deposit ID")
            seen.add(deposit_id)
            total += money(deposit.get("amount"))
        balance = owed - total
        applied = deposit_inputs(matched)
        # Cash whose bank reference named no invoice, matched here only by shipper and amount. It is never applied
        # automatically: a person confirms the match first, so this reconciliation cannot come out `verified`.
        unapplied = source.get("unapplied_deposits") or []
        if not isinstance(unapplied, list) or not all(isinstance(item, dict) for item in unapplied):
            raise SourceUnavailable("source unapplied deposits must be a list of objects")
        invoice_refs = refs_for(source, "shipper_invoice_amount")
        findings = {"shipper_invoice_id": invoice_id, "invoice_amount": number(owed),
                    "matched_amount": number(total), "balance": number(balance),
                    "matched_deposit_ids": sorted(seen),
                    "unmatched_deposit_ids": [item.get("id") for item in deposits if item not in matched],
                    "workings": [
                        working("Cash applied to this invoice (" + ", ".join(sorted(seen)) + ")",
                                " + ".join(applied) if applied else "0", applied, total,
                                evidence_refs=invoice_refs, basis="each deposit applies once, to its own invoice"),
                        working("Open balance", "shipper_invoice_amount - matched_amount",
                                {"shipper_invoice_amount": owed, "matched_amount": total}, balance,
                                evidence_refs=invoice_refs, basis="short pay is partial, overpayment is suspicious"),
                    ]}
        if unapplied or env.subject.get("reference_mismatch") is True:
            findings.update(reference_mismatch=True, cash_applied_automatically=False,
                            unapplied_deposit_ids=sorted(str(item.get("id")) for item in unapplied),
                            unapplied_amount=number(sum((money(item.get("amount")) for item in unapplied),
                                                        Decimal(0))),
                            review_reason="deposit reference matches no shipper invoice; a person must confirm "
                                          "the shipper-and-amount match before the cash is applied")
        if env.task_type == "reconcile_customer_payment" and total > 0:
            if not source.get("period"):
                raise SourceUnavailable("journal period is required")
            existing = source.get("journal_requests", []) if source_fixtures_only() else await rows(
                self.name, "journal_request", "case_id", env.case_id)
            requests, posted, pending = [], [], []
            for deposit in matched:
                # Bind an entry to its bank deposit, so a later reconciliation
                # with more deposits cannot post earlier cash a second time.
                request_id = identifier(f"{source.get('shipper_id')}:{deposit['id']}", "receivable_journal_request")
                stored = next((item for item in existing if item.get("id") == request_id), None)
                amount = money(deposit["amount"])
                lines = [{"account": "cash", "debit": number(amount), "credit": 0},
                         {"account": "accounts_receivable", "debit": 0, "credit": number(amount)}]
                if stored and stored.get("case_id") != env.case_id:
                    raise SourceUnavailable("deposit journal belongs to a different case")
                if stored and stored.get("posted_entry_id"):
                    posted.append(stored["posted_entry_id"])
                    continue
                if stored:
                    request = {"executed": False, "existing": True, "request_id": request_id,
                               "simulated": fixture_mode()}
                else:
                    request = await mutate(self, env, task_id, "request_journal_entry", {
                        "id": request_id, "period": source["period"], "lines": lines,
                        "memo": "Matched source deposit: " + deposit["id"],
                    })
                requests.append(request)
                # A fixture mutation is only a proposal until independently
                # represented in source data; live posting is a Controller task.
                if request.get("executed") or stored:
                    pending.append(request_id)
            if pending:
                # One Settlement Approvals child posts every deposit's request, so
                # any number of deposits fits the fan-out bound beside cash impact.
                posted.extend(await self.delegate("approve_entry", child_envelope(
                    env, "approve_entry", request_ids=pending), meta, task_id,
                    invocation_key="post_receivable_journals:" + identifier(",".join(pending), "journal_batch")))
            findings["journal_requests"], findings["controller"] = requests, posted
        if env.task_type == "chase_overdue" and balance > 0 and unapplied:
            # The shipper may already have paid under the wrong reference: apply that cash before chasing them.
            findings["outreach_needed"] = False
        elif env.task_type == "chase_overdue" and balance > 0:
            if not source.get("overdue"):
                return await finish(self, env, task_id, outcome="clear", findings={**findings,
                                    "outreach_needed": False}, evidence_refs=refs)
            if not source.get("shipper_id"):
                raise SourceUnavailable("shipper identifier is required for collections")
            call = TaskEnvelope(task_type="place_call", case_id=env.case_id, priority=env.priority,
                                evidence_refs=refs, subject={"purpose": "collections",
                                "counterparty_id": source["shipper_id"],
                                "questions": ["When will the outstanding invoice be paid?",
                                              "Is any amount disputed, and why?"],
                                "facts": {"invoice_id": invoice_id, "balance": number(balance)}})
            findings["outreach"] = await self.delegate("place_call", call, meta, task_id,
                                                       invocation_key="collections_contact")
        if total > 0 or env.task_type == "chase_overdue":
            findings["cash_impact"] = await self.delegate("assess_financial_impact", child_envelope(
                env, "assess_financial_impact"), meta, task_id, invocation_key="receivable_cash_impact")
        return await finish(self, env, task_id, outcome="partial" if balance > 0 else
                            "suspicious" if balance < 0 else "hold" if unapplied else "verified",
                            findings=findings, evidence_refs=refs)


if __name__ == "__main__":
    serve(ShipperArAgent(), DESCRIPTIONS)
