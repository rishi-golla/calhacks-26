from agents.verification.card import DESCRIPTIONS
from common.agent_support import evidence, finish
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import FreightAgent
from common.server import serve
from common.source_repository import SourceRepository, SourceUnavailable


class VerificationAgent(FreightAgent):
    name = "verification"

    async def handle(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str, updater):
        if env.task_type not in {"verify_claim", "verify_bank_change", "verify_dispute"}:
            raise ValueError("unsupported Carrier Trust & Verification skill")
        repository = SourceRepository()
        source = await repository.load(env)
        refs = evidence(source, env)
        company_id = source.get("carrier_id") or source.get("counterparty_id")
        if not company_id:
            raise SourceUnavailable("source counterparty identifier is missing")
        try:
            company = await repository.get_company(company_id)
            contact = await repository.get_contact(company_id)
        except SourceUnavailable:
            return await finish(self, env, task_id, outcome="unverified", confidence=0,
                                findings={"reason": "verified directory contact is unavailable"},
                                policy_ids=["P-008"], evidence_refs=refs)
        flags = []
        submitted_phone = source.get("submitted_phone")
        if submitted_phone and submitted_phone != contact.get("phone"):
            flags.append("submitted_phone_differs_from_verified_directory")
        if company.get("authority_status") not in {"ACTIVE", "active"}:
            flags.append("carrier_authority_not_confirmed_active")
        if company.get("insurance_verified") is not True:
            flags.append("carrier_insurance_not_confirmed")
        if env.task_type == "verify_dispute" and not source.get("dispute"):
            return await finish(self, env, task_id, outcome="verified" if not flags else "unverified",
                                findings={"carrier_id": company_id, "risk_signals": flags},
                                evidence_refs=refs, policy_ids=["P-008"])
        stored = source.get("verification", {}).get(env.task_type, {})
        if stored.get("verified") is True and stored.get("evidence_refs"):
            verified = not flags
            if env.task_type == "verify_bank_change":
                verified = verified and stored.get("out_of_band") is True
                if company.get("owner_operator"):
                    verified = verified and stored.get("identity_verified") is True
            return await finish(self, env, task_id, outcome="verified" if verified else "unverified",
                                findings={"verification": stored, "risk_signals": flags,
                                          "bank_details_changed": False},
                                evidence_refs=list(dict.fromkeys(refs + stored["evidence_refs"])),
                                policy_ids=["P-004", "P-008"] if env.task_type == "verify_bank_change"
                                else ["P-008"])
        questions = {
            "verify_claim": ["What supports the additional charge?", "Can you provide the receipt and timestamps?"],
            "verify_bank_change": ["Did your authorized representative request a payment-detail change?"],
            "verify_dispute": ["Please describe the disputed load and supporting evidence."],
        }[env.task_type]
        call = TaskEnvelope(task_type="place_call", case_id=env.case_id, evidence_refs=refs,
                            priority=env.priority, deadline_s=env.deadline_s,
                            subject={"purpose": "verification", "counterparty_id": company_id,
                                     "questions": questions, "facts": {"load_id": source.get("load_id"),
                                     "invoice_id": source.get("invoice_id"), "claim_type": env.task_type}})
        results = await self.delegate("place_call", call, meta, task_id, invocation_key="verified_callback")
        call_result = results[-1].get("findings", {}) if results else {}
        if "call_result" in call_result:
            call_result = call_result["call_result"]
        extracted = call_result.get("findings", {})
        if env.task_type == "verify_bank_change":
            # A phone conversation does not satisfy owner-operator IDV by itself.
            verified = (call_result.get("reached") is True and extracted.get("authorized_change") is True
                        and (not company.get("owner_operator") or source.get("identity_verified") is True)
                        and not flags)
        else:
            verified = call_result.get("reached") is True and extracted.get("claim_confirmed") is True and not flags
        return await finish(self, env, task_id, outcome="verified" if verified else "unverified",
                            findings={"call_result": call_result, "risk_signals": flags,
                                      "bank_details_changed": False}, evidence_refs=refs,
                            policy_ids=["P-004", "P-008"] if env.task_type == "verify_bank_change" else ["P-008"])


if __name__ == "__main__":
    serve(VerificationAgent(), DESCRIPTIONS)
