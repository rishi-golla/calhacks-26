"""Carrier AP: three-way match, holds, short-pay proposals. Never moves money without Controller approval."""
from agents.carrier_ap.card import DESCRIPTIONS
from agents.carrier_ap.tools import detention_amount, three_way_match
from common import store
from common.envelope import ResultArtifact
from common.runtime import Case, FreightAgent, Park, child_result
from common.server import serve

SHEET = "ap_subledger"


def _row(s: dict) -> str:
    return s.get("invoice_id") or s.get("load_id") or "unknown"


class CarrierAPAgent(FreightAgent):
    name = "carrier_ap"

    async def handle(self, case: Case) -> ResultArtifact | Park:
        s = case.subject
        if case.env.task_type == "answer_payment_status":
            cells = {c["col"]: c["value"] for c in store.sheet(SHEET) if c["row"] == _row(s)}
            return ResultArtifact(outcome="info", confidence=1.0, findings={"invoice": _row(s), "status": cells})
        match = three_way_match(s)
        row = _row(s)
        case.commit("record_invoice", [
            {"sheet": SHEET, "row": row, "col": "carrier", "after": s.get("carrier", {}).get("name")},
            {"sheet": SHEET, "row": row, "col": "invoiced", "after": s["invoice_amount"]},
            {"sheet": SHEET, "row": row, "col": "contract", "after": s["contract_rate"]},
            {"sheet": SHEET, "row": row, "col": "status", "after": "received"},
        ])
        if match["matched"]:
            case.commit("approve_invoice", [{"sheet": SHEET, "row": row, "col": "status", "after": "approved"},
                                            {"sheet": SHEET, "row": row, "col": "approved", "after": s["invoice_amount"]}])
            return ResultArtifact(outcome="approve", confidence=0.95, findings={"match": match, "approved_amount":
                                  s["invoice_amount"], "disputed_amount": 0, "invoice_amount": s["invoice_amount"]})
        case.commit("place_hold", [{"sheet": SHEET, "row": row, "col": "status", "after": "on_hold"},
                                   {"sheet": SHEET, "row": row, "col": "hold_reason", "after": match["reason"]}],
                    policy_ids=match["policy_ids"])
        await case.status("awaiting_other_agent", waiting_on="risk")
        d = await case.delegate("investigate_charge")
        if d.parked:
            return Park("awaiting_other_agent", "risk", {"step": "risk", "match": match})
        return await self._after_risk(case, match, d.result)

    async def on_resume(self, case: Case) -> ResultArtifact | Park:
        st = case.state
        if st["step"] == "risk":
            return await self._after_risk(case, st["match"], child_result(case))
        return self._final(case, st, child_result(case))

    async def _after_risk(self, case: Case, match: dict, risk: dict) -> ResultArtifact | Park:
        s = case.subject
        f = risk.get("findings", {})
        row = _row(s)
        if f.get("claim_denied"):
            disputed = detention_amount(s)  # counterparty affirmatively withdrew/denied the charge
        elif f.get("claim_confirmed"):
            disputed = 0.0  # verified exception: still needs a human to approve the over-contract variance
        else:
            case.commit("keep_hold", [{"sheet": SHEET, "row": row, "col": "status", "after": "on_hold"},
                                      {"sheet": SHEET, "row": row, "col": "hold_reason",
                                       "after": f"unresolved variance; risk {risk.get('outcome')}"}],
                        policy_ids=match["policy_ids"])
            return ResultArtifact(outcome="hold", confidence=0.7, policy_ids=match["policy_ids"],
                                  evidence_refs=case.env.evidence_refs,
                                  findings={"invoice_amount": s["invoice_amount"], "match": match, "risk": risk},
                                  recommended_next=["controller.review_hold"])
        approved = round(s["invoice_amount"] - disputed, 2)
        if approved > s["contract_rate"] * 1.02:
            s = {**s, "force_human_approval": True}
        lines = [{"account": "5000 Purchased Transportation", "debit": approved, "credit": 0},
                 {"account": "2000 Accounts Payable", "debit": 0, "credit": approved}]
        case.commit("propose_short_pay" if disputed else "propose_payment", [
            {"sheet": SHEET, "row": row, "col": "approved", "after": approved},
            {"sheet": SHEET, "row": row, "col": "disputed", "after": disputed},
            {"sheet": SHEET, "row": row, "col": "status", "after": "pending_approval"},
        ], policy_ids=match["policy_ids"])
        st = {"step": "controller", "match": match, "risk": risk, "approved": approved, "disputed": disputed,
              "lines": lines}
        await case.status("awaiting_other_agent", waiting_on="controller")
        d = await case.delegate("approve_entry", subject={
            "invoice_id": row, "amount": approved, "disputed": disputed, "requested_by": self.name, "lines": lines,
            "memo": f"{row} pay {approved:.2f}, dispute {disputed:.2f}", "carrier": s.get("carrier", {}),
            "force_human": s.get("force_human_approval", False)})
        if d.parked:
            return Park("awaiting_human_approval", "controller", st)
        return self._final(case, st, d.result)

    def _final(self, case: Case, st: dict, ctrl: dict) -> ResultArtifact:
        s, row = case.subject, _row(case.subject)
        approved = ctrl.get("outcome") == "approve"
        status = ("approved_short_pay" if st["disputed"] else "approved") if approved else "on_hold"
        case.commit("finalize_invoice", [{"sheet": SHEET, "row": row, "col": "status", "after": status}],
                    policy_ids=ctrl.get("policy_ids", []))
        return ResultArtifact(
            outcome=("short_pay" if st["disputed"] else "approve") if approved else "hold",
            confidence=0.9, policy_ids=sorted(set(st["match"]["policy_ids"] + ctrl.get("policy_ids", []))),
            evidence_refs=case.env.evidence_refs,
            findings={"invoice_amount": s["invoice_amount"], "approved_amount": st["approved"],
                      "disputed_amount": st["disputed"], "journal": st["lines"], "risk": st["risk"],
                      "controller": ctrl, "match": st["match"]},
            recommended_next=["carrier_ap.propose_payment"] if approved else ["controller.review_hold"])


if __name__ == "__main__":
    serve(CarrierAPAgent(), DESCRIPTIONS)
