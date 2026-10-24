"""Controller: approvals and GL posting. Parks for a human above the threshold or on request."""
import uuid

from agents.controller.card import DESCRIPTIONS
from common import mirror, store
from common.envelope import ResultArtifact
from common.policy import APPROVAL_THRESHOLD_USD, check
from common.runtime import Case, FreightAgent, Park
from common.server import serve


class ControllerAgent(FreightAgent):
    name = "controller"

    async def handle(self, case: Case) -> ResultArtifact | Park:
        t, s = case.env.task_type, case.subject
        if t == "check_already_paid":
            rows = [c for c in store.sheet("ap_subledger") if c["row"] == s.get("invoice_id") and c["col"] == "status"]
            paid = any(str(c["value"]).startswith("paid") for c in rows)
            return ResultArtifact(outcome="hold" if paid else "clear", findings={"already_paid": paid})
        if t != "approve_entry":
            return ResultArtifact(outcome="not_implemented", findings={"task_type": t})
        d = check("post_journal_entry", self.name, s)
        if d.result == "deny":
            case.log("policy_denied", policy_ids=d.policy_ids, reason=d.reason)
            return ResultArtifact(outcome="reject", policy_ids=d.policy_ids, findings={"reason": d.reason})
        if s.get("amount", 0) > APPROVAL_THRESHOLD_USD or s.get("force_human"):
            appr_id = f"appr_{uuid.uuid4().hex[:8]}"
            req = {"approval_id": appr_id, "amount": s.get("amount"), "memo": s.get("memo"), "lines": s.get("lines"),
                   "requested_by": s.get("requested_by"), "policy_ids": ["P-003"]}
            store.park(appr_id, "approval", self.name, case.task_id, case.context_id, case.env.case_id, req)
            mirror.emit("upsert_approval", req)
            case.log("approval_requested", **req)
            return Park("awaiting_human_approval", appr_id, {"approval_id": appr_id})
        return self._post(case, s, ["P-005", "P-006"], approver="controller")

    async def on_resume(self, case: Case) -> ResultArtifact:
        r, s = case.resume or {}, case.subject
        approver = r.get("approver", "human")
        if approver == s.get("requested_by"):
            case.log("policy_denied", policy_ids=["P-005"], reason="requester cannot approve")
            return ResultArtifact(outcome="reject", policy_ids=["P-005"], findings={"reason": "requester cannot approve"})
        if r.get("decision") != "approve":
            case.log("human_override", decision=r.get("decision"), approver=approver, note=r.get("note"))
            return ResultArtifact(outcome="reject", policy_ids=["P-003"],
                                  findings={"approver": approver, "note": r.get("note")})
        return self._post(case, s, ["P-003", "P-005", "P-006"], approver=approver)

    def _post(self, case: Case, s: dict, policy_ids: list[str], approver: str) -> ResultArtifact:
        je = f"JE-{uuid.uuid4().hex[:6].upper()}"
        cells = [{"sheet": "gl", "row": f"{je}:{i}", "col": col, "after": val}
                 for i, ln in enumerate(s.get("lines", [])) for col, val in
                 (("account", ln["account"]), ("debit", ln["debit"]), ("credit", ln["credit"]), ("memo", s.get("memo")))]
        cs = case.commit("post_journal_entry", cells, policy_ids=policy_ids)
        return ResultArtifact(outcome="approve", confidence=1.0, policy_ids=policy_ids,
                              findings={"journal_entry": je, "lines": s.get("lines", []), "approver": approver,
                                        "change_set": cs["id"]})


if __name__ == "__main__":
    serve(ControllerAgent(), DESCRIPTIONS)
