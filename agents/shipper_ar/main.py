"""Shipper AR: cash application, short-pay detection, collections calls via Voice Desk."""
from agents.shipper_ar.card import DESCRIPTIONS
from common.envelope import PlaceCall, ResultArtifact
from common.runtime import Case, FreightAgent, Park, child_result
from common.server import serve
from common.transcripts import promise_to_pay
from integrations.tools import crs_business_credit

SHEET = "ar_subledger"


class ShipperARAgent(FreightAgent):
    name = "shipper_ar"

    async def handle(self, case: Case) -> ResultArtifact | Park:
        t, s = case.env.task_type, case.subject
        shipper = s.get("shipper", {})
        if t == "set_credit_limit":
            cr = crs_business_credit(shipper.get("name", ""), shipper.get("state", ""))
            limit = 50_000 if cr.get("score", 0) >= 70 else 15_000
            case.commit("set_credit_limit", [{"sheet": "credit", "row": shipper.get("id", "?"), "col": "limit",
                                              "after": limit}], sources=["crs"])
            return ResultArtifact(outcome="info", findings={"credit": cr, "limit": limit})
        inv = s.get("invoice_id", "?")
        billed, paid = float(s.get("invoice_amount", 0)), float(s.get("paid_amount", 0))
        short = round(billed - paid, 2)
        case.commit("apply_cash", [{"sheet": SHEET, "row": inv, "col": "billed", "after": billed},
                                   {"sheet": SHEET, "row": inv, "col": "paid", "after": paid},
                                   {"sheet": SHEET, "row": inv, "col": "open", "after": short},
                                   {"sheet": SHEET, "row": inv, "col": "status",
                                    "after": "paid" if short <= 0 else "short_paid"}])
        if short <= 0:
            return ResultArtifact(outcome="reconciled", confidence=1.0, findings={"invoice_id": inv, "open": 0})
        if not s.get("chase", True):
            return ResultArtifact(outcome="short_pay", findings={"invoice_id": inv, "open": short})
        contact = {**(shipper.get("directory") or {}), "directory_verified": bool(shipper.get("directory"))}
        pc = PlaceCall(purpose="collections", counterparty_id=shipper.get("id", "?"),
                       questions=[f"Invoice {inv} was short-paid by ${short:,.2f}. When will the balance be paid?"],
                       facts={"contact": contact, "demo_target": s.get("demo_target"), "script": "collections"})
        await case.status("awaiting_call", waiting_on="voice_desk")
        d = await case.delegate("place_call", subject=pc.model_dump())
        if d.parked:
            return Park("awaiting_call", "voice_desk", {"inv": inv, "short": short})
        return self._after_call(case, inv, short, d.result)

    async def on_resume(self, case: Case) -> ResultArtifact:
        return self._after_call(case, case.state["inv"], case.state["short"], child_result(case))

    def _after_call(self, case: Case, inv: str, short: float, call: dict) -> ResultArtifact:
        ptp = promise_to_pay(call.get("findings", {}).get("findings", {}).get("transcript", []))
        if ptp:
            case.commit("record_promise_to_pay", [{"sheet": SHEET, "row": inv, "col": "promise_to_pay", "after": ptp}],
                        sources=call.get("evidence_refs", []))
        return ResultArtifact(outcome="short_pay", confidence=0.85, evidence_refs=call.get("evidence_refs", []),
                              findings={"invoice_id": inv, "open": short, "promise_to_pay": ptp})


if __name__ == "__main__":
    serve(ShipperARAgent(), DESCRIPTIONS)
