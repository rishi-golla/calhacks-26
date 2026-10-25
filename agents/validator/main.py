import asyncio
import logging

from agents.validator.card import DESCRIPTIONS
from agents.validator.tools import (
    ISSUE,
    brief,
    deterministic_review,
    drafted_lesson,
    grounded_issues,
    instruction_proposal,
    learned_line,
    review_bundle,
    review_view,
)
from common.agent_support import identifier, mutate
from common.envelope import DelegationMeta, Lesson, ResultArtifact, TaskEnvelope
from common.learning import reconstruct_instructions
from common.providers import ProviderConfigurationError, ProviderError, infer
from common.runtime import FreightAgent
from common.server import serve
from common.spacetime_client import SpacetimeClient, SpacetimeHTTPError

log = logging.getLogger(__name__)
# A model review turn that was never answered (transport, 429, 5xx) is asked once more before the outcome is
# checkpointed: a saved failure would leave this review without model commentary for good.
ASSESS_RETRY_BACKOFF_S = 1.0


class ValidatorAgent(FreightAgent):
    name = "validator"

    async def handle(self, env, meta, task_id, updater):
        from common.providers import mode
        if mode() == "live":
            from common.tool_loop import GuardedSkillFailed, run_tool_loop
            try:
                return await run_tool_loop(self, env, meta, task_id)
            except (ProviderError, GuardedSkillFailed) as error:
                # The verdict is the deterministic two-sided review's; the model only selects inspections and adds
                # grounded commentary. A model loop that ends before the guarded workflow returns (cut short,
                # budget, provider outage, or the workflow failing inside the loop and a limit ending it) therefore
                # runs that workflow once more, directly, instead of failing the review. Its checkpoints and write
                # IDs are the ones the tool used, so nothing is done twice; if it fails again, its own error ends
                # the task.
                log.warning("validator task %s: model tool loop failed (%s); running the deterministic review",
                            task_id, error)
                result = await self.run_workflow(env, meta, task_id, updater)
                return result.model_copy(update={"findings": {
                    **result.findings, "simulated": False, "tool_loop_unavailable": str(error)[:200]}})
        return await self.run_workflow(env, meta, task_id, updater)

    async def run_workflow(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str, updater):
        if env.task_type not in {"review_output", "review_flag"}:
            raise ValueError("unsupported Settlement Quality Review skill")
        db = SpacetimeClient(actor=self.name)
        bundle = await review_bundle(self, env, task_id)
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
        async def assess():
            from common.tool_loop import retryable_turn_error
            evidence = review_view(bundle, review.checks)
            try:
                try:
                    return await infer(self.name, review_env, evidence, purpose="review")
                except ProviderError as error:
                    if not retryable_turn_error(error):
                        raise
                    log.warning("validator task %s review assessment %s; retrying once", task_id, error)
                    await asyncio.sleep(ASSESS_RETRY_BACKOFF_S)
                    return await infer(self.name, review_env, evidence, purpose="review")
            except ProviderError as error:
                # The model pass only explains failures the checks found (grounded) or adds advisories, so the
                # deterministic verdict stands without it; the outage is recorded, never a silent pass.
                return {"assessment": {}, "model_used": None, "assessment_error": str(error)[:200]}
        assessment = await self.step(task_id, "review_assessment", assess)
        if assessment.get("assessment_error"):
            review.checks.append({"name": "qualitative_review", "ok": False, "severity": "advisory",
                                  "expected": "model commentary on the deterministic checks",
                                  "actual": f"unavailable: {assessment['assessment_error']}",
                                  "basis": "the deterministic verdict stands without model commentary"})
        issues = assessment.get("assessment", {}).get("issues", [])
        grounded, ungrounded = grounded_issues(issues, review.checks)
        if grounded:
            # Model commentary explains a failure the deterministic checks already established;
            # the verdict is already an issue, so this adds no authority of its own.
            review.checks.append({"name": "qualitative_review", "ok": False, "severity": "issue",
                                  "expected": "no unresolved evidence issues", "actual": grounded,
                                  "basis": "model concern anchored to a failed check"})
        if ungrounded:
            review.checks.append({"name": "qualitative_review", "ok": False, "severity": "advisory",
                                  "expected": "concerns anchored to checks or evidence", "actual": ungrounded,
                                  "basis": "unanchored model commentary never changes a verdict"})
        draft = {}
        if review.lessons:
            draft = await self.step(task_id, "lesson_draft", lambda: self.draft_lesson(review_env, target, review))
            for lesson in review.lessons:
                lesson.text_or_diff = drafted_lesson(draft) or lesson.text_or_diff
        review_id = identifier(task_id, "review")
        receipts = []
        superseded = False
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
                outcome = await self.publish_instruction(env, task_id, bundle, db, lesson, review_id,
                                                         learned_line(draft))
                if outcome["receipt"]:
                    receipts.append(outcome["receipt"])
                if outcome["patch"]:
                    review.lessons.append(Lesson(agent=lesson.agent, type="instruction", scope=lesson.scope,
                        text_or_diff=outcome["patch"], confidence=lesson.confidence,
                        evidence_refs=lesson.evidence_refs))
                superseded = superseded or outcome["superseded"]
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
                      **({"assessment_error": assessment["assessment_error"]}
                         if assessment.get("assessment_error") else {}),
                      "instruction_publication": "published" if any(
                          r.get("action") == "publish_instruction_version" and r.get("executed")
                          for r in receipts) else "superseded" if superseded
                      else "simulated" if bundle["simulated"] else "not_needed"},
            evidence_refs=refs, policy_ids=["P-012"], model_used=assessment.get("model_used"))

    async def draft_lesson(self, env: TaskEnvelope, target: dict, review) -> dict:
        """Model-drafted lesson and instruction line from the failed checks only.

        The model sees check names, bounded expected/actual values and their basis, never the source
        documents or earlier model commentary, so document text cannot steer what agents learn.
        Any provider failure falls back to the code-generated lesson.
        """
        failed = [{"name": x["name"], "expected": brief(x["expected"]), "actual": brief(x["actual"]),
                   "basis": x.get("basis", "")}
                  for x in review.checks if not x["ok"] and x.get("severity") == ISSUE
                  and x["name"] != "qualitative_review"][:12]
        try:
            result = await infer(self.name, env, {"reviewed_agent": target.get("agent"),
                                                  "reviewed_skill": target.get("skill"),
                                                  "failed_checks": failed}, purpose="lesson")
        except (ProviderError, ProviderConfigurationError):
            return {}
        return result.get("assessment", {})

    async def publish_instruction(self, env, task_id: str, bundle: dict, db: SpacetimeClient,
                                  lesson: Lesson, review_id: str, line: str = "") -> dict:
        """Publish the code-generated reminder against the reviewed base version.

        The plan (patch + base version) is checkpointed so retries reuse the same write. If the
        reducer rejects the write because another publication or human rollback advanced the
        version first, re-plan once from the new history: when the reminder is already present
        the publication is superseded, otherwise publish once more from the new base. Any other
        rejection, or a second conflict, propagates.
        """
        agent = lesson.agent

        async def prepare_instruction():
            history = [] if bundle["simulated"] else await db.get_instruction_history(agent)
            original, base_version = reconstruct_instructions(agent, history)
            return {"patch": instruction_proposal(original, line), "base_version": base_version}

        async def publish():
            plan = await self.step(task_id, f"instruction_plan:{agent}", prepare_instruction)
            for attempt in ("plan", "replan"):
                patch, base_version = plan["patch"], plan["base_version"]
                if not patch:
                    return {"receipt": None, "patch": "", "superseded": attempt == "replan"}
                try:
                    receipt = await mutate(self, env, task_id, "publish_instruction_version", {
                        "agent": agent, "diff": patch, "expected_version": base_version,
                        "reason": "Deterministic review found evidence or exact-cent issues",
                        "source_ref": review_id,
                    })
                except SpacetimeHTTPError as error:
                    if attempt == "replan" or error.kind not in {"rejected", "conflict"}:
                        raise
                    plan = await self.step(task_id, f"instruction_replan:{agent}:{base_version}",
                                           prepare_instruction)
                    if plan["base_version"] == base_version:
                        raise  # Not a version race: the reducer refused this content.
                    continue
                return {"receipt": receipt, "patch": patch, "superseded": False}
            raise AssertionError("unreachable")

        # The settled outcome is checkpointed so a replayed task never re-sends a conflicting write.
        return await self.step(task_id, f"instruction_outcome:{agent}", publish)


if __name__ == "__main__":
    serve(ValidatorAgent(), DESCRIPTIONS)
