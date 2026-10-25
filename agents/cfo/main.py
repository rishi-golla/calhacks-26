from decimal import Decimal

from agents.cfo.card import DESCRIPTIONS
from common.agent_support import evidence, finish, identifier, matched_deposit_total, money, mutate, number
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import FreightAgent
from common.server import serve
from common.source_repository import SourceRepository, SourceUnavailable
from common.workings import refs_for, working


class CfoAgent(FreightAgent):
    name = "cfo"

    async def handle(self, env, meta, task_id, updater):
        from common.providers import mode
        if mode() == "live":
            from common.tool_loop import run_tool_loop
            return await run_tool_loop(self, env, meta, task_id)
        return await self.run_workflow(env, meta, task_id, updater)

    async def run_workflow(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str, updater):
        if env.task_type not in {"assess_financial_impact", "cash_forecast"}:
            raise ValueError("unsupported Load Profitability skill")
        source = await SourceRepository().load(env)
        refs = evidence(source, env)
        period = source.get("period")
        if not period:
            raise SourceUnavailable("forecast period is missing from source records")
        receivables = source.get("receivables")
        if receivables is None:
            collected = source.get("shipper_paid_amount")
            if collected is None:
                collected = matched_deposit_total(source)
            receivables = [{"amount": source.get("shipper_invoice_amount"),
                            "collected": collected}]
        payables = source.get("payables")
        if payables is None:
            payables = [{"amount": source.get("invoice_amount"), "paid": source.get("paid_amount", 0)}]
        incoming = sum((money(item["amount"]) - money(item.get("collected", 0))
                        for item in receivables), Decimal(0))
        outgoing = sum((money(item["amount"]) - money(item.get("paid", 0)) for item in payables), Decimal(0))
        if incoming < 0 or outgoing < 0:
            raise SourceUnavailable("source balances cannot be negative")
        net = incoming - outgoing
        revenue = money(source.get("shipper_invoice_amount"))
        cost = money(source.get("invoice_amount"))
        contract = money(source.get("contract_rate"))
        revenue_refs = refs_for(source, "shipper_invoice_amount")
        cost_refs = refs_for(source, "invoice_amount", "contract_rate")
        findings = {"load_id": source.get("load_id"), "revenue": number(revenue),
                    "invoiced_carrier_cost": number(cost), "contracted_carrier_cost": number(contract),
                    "invoiced_margin": number(revenue - cost), "contracted_margin": number(revenue - contract),
                    "expected_collections": number(incoming), "outstanding_carrier_cost": number(outgoing),
                    "net_cash": number(net), "period": period,
                    "assumptions": ["Outstanding invoices are included at face value; holds do not erase obligations."],
                    "workings": [
                        working("Margin as invoiced", "revenue - invoiced_carrier_cost",
                                {"revenue": revenue, "invoiced_carrier_cost": cost}, revenue - cost,
                                evidence_refs=revenue_refs + cost_refs, basis="load profitability"),
                        working("Margin at contract", "revenue - contracted_carrier_cost",
                                {"revenue": revenue, "contracted_carrier_cost": contract}, revenue - contract,
                                evidence_refs=revenue_refs + cost_refs, basis="load profitability"),
                        working("Expected collections", "sum(receivable amount - collected)",
                                {f"receivable_{i + 1}": {"amount": r["amount"], "collected": r.get("collected", 0)}
                                 for i, r in enumerate(receivables)}, incoming,
                                evidence_refs=revenue_refs, basis="open AR at face value"),
                        working("Outstanding carrier cost", "sum(payable amount - paid)",
                                {f"payable_{i + 1}": {"amount": p["amount"], "paid": p.get("paid", 0)}
                                 for i, p in enumerate(payables)}, outgoing,
                                evidence_refs=cost_refs, basis="open AP at face value; holds do not erase obligations"),
                        working("Net cash on this load", "expected_collections - outstanding_carrier_cost",
                                {"expected_collections": incoming, "outstanding_carrier_cost": outgoing}, net,
                                evidence_refs=revenue_refs + cost_refs, basis="cash forecast"),
                    ]}
        receipts = []
        # Store cash-flow components; the derived net is not another cash flow.
        for category, amount in (("collections", incoming), ("carrier_payables", -outgoing)):
            receipts.append(await mutate(self, env, task_id, "upsert_forecast", {
                "id": identifier(env.case_id, f"{period}:{category}"), "period": period, "category": category,
                "amount": number(amount), "confidence": 0.8,
                "assumptions_json": {"basis": "source outstanding balances", "evidence_refs": refs},
            }))
        findings["forecasts"] = receipts
        return await finish(self, env, task_id, outcome="verified", findings=findings, evidence_refs=refs)


if __name__ == "__main__":
    serve(CfoAgent(), DESCRIPTIONS)
