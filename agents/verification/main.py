"""Verification: out-of-band checks with the real counterparty (demo-routed). Uses only directory contacts."""
import os
import uuid

from agents.verification.card import DESCRIPTIONS
from common import a2a_client, store
from common.envelope import PlaceCall, ResultArtifact
from common.outreach import route_target
from common.parking import resume_parked
from common.policy import check
from common.runtime import Case, FreightAgent, Park, child_result, spawn
from common.server import serve
from common.transcripts import extract_claim_findings
from integrations.tools import enrich_contact, fmcsa_lookup


def directory_contact(company: dict) -> dict:
    """Verified directory: FMCSA registration first, then enrichment (PRD enrichment waterfall)."""
    if company.get("directory"):
        return {**company["directory"], "directory_verified": True}
    fm = fmcsa_lookup(company.get("usdot"))
    en = enrich_contact(company.get("id", ""), "dispatch")
    return {"company": company.get("name") or fm.get("legal_name"), "name": en.get("name"),
            "phone": fm.get("phone") or en.get("phone"), "email": en.get("email"),
            "source": fm.get("source", "fmcsa"), "confidence": en.get("confidence"),
            "directory_verified": "error" not in fm}


class VerificationAgent(FreightAgent):
    name = "verification"

    async def handle(self, case: Case) -> ResultArtifact | Park:
        s = case.subject
        company = s.get("carrier") or s.get("counterparty") or {}
        contact = directory_contact(company)
        d = check("contact_counterparty", self.name, contact)
        if d.result != "allow":
            case.log("policy_denied", policy_ids=d.policy_ids, reason=d.reason)
            return ResultArtifact(outcome="unverified", policy_ids=d.policy_ids, findings={"error": d.reason})
        if s.get("channel") == "email":
            return await self._email(case, contact)
        pc = PlaceCall(purpose="verification", counterparty_id=company.get("id", "unknown"),
                       questions=s.get("questions", []),
                       facts={"contact": contact, "claim": s.get("claim"), "load_id": s.get("load_id"),
                              "demo_target": s.get("demo_target"), "script": s.get("script", "detention_dispute"),
                              **s.get("facts", {})})
        await case.status("awaiting_call", waiting_on="voice_desk", intended_to=contact.get("phone"))
        r = await case.delegate("place_call", subject=pc.model_dump())
        if r.parked:
            return Park("awaiting_external_verification", "voice_desk", {"contact": contact, "channel": "call"})
        return await self._verdict(case, contact, r.result.get("findings", {}).get("findings", {}).get("transcript", []))

    async def _email(self, case: Case, contact: dict) -> Park:
        s = case.subject
        target = route_target("email", contact.get("email") or "unknown", s.get("demo_target"))
        email_id = f"email_{uuid.uuid4().hex[:8]}"
        body = {"intended_to": target.intended_to, "routed_to": target.routed_to, "mode": target.mode,
                "subject": f"Verification needed: {s.get('claim')}", "questions": s.get("questions", []),
                "contact": contact}
        store.park(email_id, "email", self.name, case.task_id, case.context_id, case.env.case_id, body)
        case.log("email_sent", email_id=email_id, intended_to=target.intended_to, routed_to=target.routed_to)
        if os.getenv("MOCK_INTEGRATIONS", "1") == "1" and os.getenv("EMAIL_AUTOREPLY", "1") == "1":
            spawn(_mock_reply(email_id))
        return Park("awaiting_external_verification", email_id, {"contact": contact, "channel": "email",
                                                                  "email_id": email_id})

    async def on_resume(self, case: Case) -> ResultArtifact:
        r = case.resume or {}
        if case.state.get("channel") == "email":
            return await self._verdict(case, case.state["contact"], r.get("text", ""), ref=r.get("key"))
        call = child_result(case).get("findings", {})
        return await self._verdict(case, case.state["contact"], call.get("findings", {}).get("transcript", []),
                                   ref=call.get("transcript_ref"), call=call)

    async def _verdict(self, case: Case, contact: dict, transcript, ref: str | None = None,
                       call: dict | None = None) -> ResultArtifact:
        f = await extract_claim_findings(transcript, case.subject.get("claim", ""), case.env.case_id, self.name)
        return ResultArtifact(outcome="verified" if f["claim_confirmed"] else "unverified", confidence=0.9,
                              findings={**f, "contact": contact, "call": call or {}}, model_used=f.get("model_used"),
                              evidence_refs=[x for x in [ref] if x])


async def _mock_reply(email_id: str) -> None:
    import asyncio
    await asyncio.sleep(float(os.getenv("VOICE_MOCK_DELAY_S", "2")))
    await resume_parked(email_id, {"kind": "email_reply", "text": "We checked: the driver was in and out in about "
                                   "45 minutes. No receipt. We withdraw the detention."})


__all__ = ["a2a_client"]

if __name__ == "__main__":
    serve(VerificationAgent(), DESCRIPTIONS)
