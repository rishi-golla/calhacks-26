from agents.carrier_ap.tools import duplicate_invoices
from agents.risk.card import DESCRIPTIONS
from agents.risk.tools import audit_charges
from common.agent_support import child_envelope, evidence, finish
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import FreightAgent
from common.server import serve
from common.source_repository import SourceRepository


class RiskAgent(FreightAgent):
    name = "risk"

    async def handle(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str, updater):
        source = await SourceRepository().load(env)
        refs = evidence(source, env)
        if env.task_type == "investigate_charge":
            findings = audit_charges(source)
            if duplicate_invoices(source):
                return await finish(self, env, task_id, outcome="reject", findings=findings,
                                    policy_ids=["P-007"], evidence_refs=refs)
            if findings["needs_verification"] or findings["insufficient_timing_evidence"]:
                checked = await self.delegate("verify_claim", child_envelope(env, "verify_claim"),
                                               meta, task_id, invocation_key="charge_verification")
                findings["verification"] = checked
            suspicious = findings["needs_verification"]
            return await finish(self, env, task_id, outcome="suspicious" if suspicious else
                                "unverified" if findings["insufficient_timing_evidence"] else "clear",
                                findings=findings, evidence_refs=refs,
                                recommended_next=["controller.review_hold"] if suspicious else [])
        if env.task_type in {"screen_bank_change", "screen_counterparty"}:
            skill = "verify_bank_change" if env.task_type == "screen_bank_change" else "verify_dispute"
            checked = await self.delegate(skill, child_envelope(env, skill), meta, task_id,
                                          invocation_key="carrier_trust")
            verified = checked and checked[-1].get("outcome") == "verified"
            return await finish(self, env, task_id, outcome="clear" if verified else "suspicious",
                                findings={"verification": checked, "bank_details_changed": False},
                                evidence_refs=refs, policy_ids=["P-004"] if skill == "verify_bank_change" else [])
        raise ValueError("unsupported Accessorial Audit skill")


if __name__ == "__main__":
    serve(RiskAgent(), DESCRIPTIONS)
