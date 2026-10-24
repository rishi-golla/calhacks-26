"""Voice Desk: the only service that dials. Enforces caller->purpose, directory contacts and the demo allowlist."""
import uuid

from agents.voice_desk.card import DESCRIPTIONS
from agents.voice_desk.tools import dial
from common import mirror, store
from common.config import CALL_PURPOSES
from common.envelope import CallResult, PlaceCall, ResultArtifact
from common.outreach import route_target
from common.policy import check
from common.runtime import Case, FreightAgent, Park
from common.server import serve


class VoiceDeskAgent(FreightAgent):
    name = "voice_desk"

    async def handle(self, case: Case) -> ResultArtifact | Park:
        if case.env.task_type == "handle_inbound_call":
            return ResultArtifact(outcome="info", findings={"note": "inbound handled via gateway in-call tools"})
        pc = PlaceCall.model_validate(case.subject)
        caller = case.meta.from_agent
        if CALL_PURPOSES.get(caller) != pc.purpose:
            case.log("policy_denied", reason=f"{caller} may not place {pc.purpose} calls")
            return ResultArtifact(outcome="reject", findings={"error": f"{caller} may not place {pc.purpose} calls"})
        contact = pc.facts.get("contact", {})
        d = check("contact_counterparty", self.name, contact)
        if d.result != "allow":
            case.log("policy_denied", policy_ids=d.policy_ids, reason=d.reason)
            return ResultArtifact(outcome="reject", policy_ids=d.policy_ids, findings={"error": d.reason})
        target = route_target("phone", contact.get("phone") or "unknown", pc.facts.get("demo_target"))
        call_id = f"call_{uuid.uuid4().hex[:10]}"
        call = {"call_id": call_id, "purpose": pc.purpose, "caller": caller, "intended_to": target.intended_to,
                "routed_to": target.routed_to, "mode": target.mode, "contact": contact,
                "questions": pc.questions, "claim": pc.facts.get("claim")}
        store.park(call_id, "call", self.name, case.task_id, case.context_id, case.env.case_id, call)
        mirror.emit("upsert_call", call)
        placed = await dial(call_id, target.routed_to, pc.purpose, {**pc.facts, "questions": pc.questions})
        case.log("call_placed", **call, provider=placed)
        if placed.get("error"):
            store.take_parked(call_id)
            return ResultArtifact(outcome="failed", findings={"error": placed["error"], **call})
        return Park("awaiting_call", call_id, {"call": call})

    async def on_resume(self, case: Case) -> ResultArtifact:
        r, call = case.resume or {}, case.state["call"]
        reached = r.get("status", "completed") == "completed"
        res = CallResult(reached=reached, intended_to=call["intended_to"], routed_to=call["routed_to"],
                         findings={"transcript": r.get("transcript", []), "status": r.get("status")},
                         transcript_ref=call["call_id"])
        case.log("call_ended", call_id=call["call_id"], status=r.get("status"))
        mirror.emit("upsert_call", {**call, "status": r.get("status"), "transcript": r.get("transcript", [])})
        return ResultArtifact(outcome="info" if reached else "failed", confidence=1.0, findings=res.model_dump(),
                              evidence_refs=[call["call_id"]])


if __name__ == "__main__":
    serve(VoiceDeskAgent(), DESCRIPTIONS)
