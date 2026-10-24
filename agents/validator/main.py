from agents.validator.card import DESCRIPTIONS
from agents.validator.tools import deterministic_review, instruction_proposal, load_review
from common.agent_support import identifier, mutate
from common.envelope import DelegationMeta, Lesson, ResultArtifact, TaskEnvelope
from common.learning import reconstruct_instructions
from common.providers import infer
from common.runtime import FreightAgent
from common.server import serve
from common.spacetime_client import SpacetimeClient


class ValidatorAgent(FreightAgent):
    name = "validator"

    async def handle(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str, updater):
        if env.task_type not in {"review_output", "review_flag"}:
            raise ValueError("unsupported Settlement Quality Review skill")
        db = SpacetimeClient(actor=self.name)
        bundle = await self.step(task_id, "review_evidence", lambda: load_review(env, db))
        target, flag = bundle["target"], bundle.get("flag")
        if not target.get("task_id"):
            if flag and flag.get("status") not in {"resolved", "rejected"}:
                await mutate(self, env, task_id, "resolve_flag", {
                    "id": flag["id"], "status": "reviewing", "resolution_ref": "",
                    "summary": "Flag needs human provenance: no supported task or financial cell could be resolved.",
                })
            return ResultArtifact(outcome="issue", findings={"simulated": bundle["simulated"],
                "resolution": "target_provenance_unavailable", "review": {
                    "verdict": "issue", "checks": [{"name": "target_provenance", "ok": False}],
                    "friction": [], "lessons": [], "actions": ["none"]}},
                recommended_next=["Identify the source task or change set for human review."])
        review = deterministic_review(bundle)
        refs = bundle["artifact"].get("evidence_refs", []) if isinstance(bundle["artifact"], dict) else []
        review_env = env.model_copy(update={"evidence_refs": refs})
        assessment = await self.step(task_id, "review_assessment", lambda: infer(
            self.name, review_env, {"target": target, "checks": review.checks,
                "source": bundle["source"], "history": bundle["history"], "flag": flag}, purpose="review"))
        issues = assessment.get("assessment", {}).get("issues", [])
        if issues:
            review.checks.append({"name": "qualitative_review", "ok": False,
                                  "expected": "no unresolved evidence issues", "actual": issues})
            review.verdict = "issue"
        review_id = identifier(task_id, "review")
        receipts = []
        if target.get("a2a_state") in {"completed", "failed", "canceled", "rejected"}:
            receipts.append(await mutate(self, env, task_id, "add_review", {
                "id": review_id, "target_task_id": target["task_id"], "verdict": review.verdict,
                "checks_json": review.checks, "friction_json": [x.model_dump() for x in review.friction],
                "actions": review.actions,
            }))
            for index, lesson in enumerate(list(review.lessons)):
                receipts.append(await mutate(self, env, task_id, "upsert_memory", {
                    "id": identifier(task_id, f"lesson:{index}"), "agent": lesson.agent,
                    "scope": lesson.scope, "text": lesson.text_or_diff,
                    "source_ref": review_id, "active": True,
                }))
                history = [] if bundle["simulated"] else await db.get_instruction_history(lesson.agent)
                original, _ = reconstruct_instructions(lesson.agent, history)
                patch = instruction_proposal(original)
                if patch:
                    review.lessons.append(Lesson(agent=lesson.agent, type="instruction", scope=lesson.scope,
                        text_or_diff=patch, confidence=lesson.confidence, evidence_refs=lesson.evidence_refs))
                    # Publication requires an atomic base-version precondition in W2.
                    # Return a validated proposal; never race a human edit or apply flag text.
            if flag and flag.get("status") not in {"resolved", "rejected"}:
                receipts.append(await mutate(self, env, task_id, "resolve_flag", {
                    "id": flag["id"], "status": "resolved", "resolution_ref": review_id,
                }))
        else:
            review.actions = ["none"]
        return ResultArtifact(outcome=review.verdict, confidence=0.9,
            findings={"review": review.model_dump(), "target_task_id": target.get("task_id"),
                      "review_id": review_id, "receipts": receipts, "simulated": bundle["simulated"],
                      "assessment": assessment.get("assessment", {}),
                      "instruction_publication": "requires_version_precondition"},
            evidence_refs=refs, policy_ids=["P-012"], model_used=assessment.get("model_used"))


if __name__ == "__main__":
    serve(ValidatorAgent(), DESCRIPTIONS)
