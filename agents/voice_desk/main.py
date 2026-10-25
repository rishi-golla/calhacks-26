from agents.voice_desk.card import DESCRIPTIONS
from common.agent_support import evidence, finish
from common.envelope import CallResult, ResultArtifact
from common.outreach import OutreachBlocked
from common.runtime import FreightAgent
from common.server import serve
from common.source_repository import NotInDirectory, SourceRepository, SourceUnavailable
from common.voice import CallRefused, call_attempt_exists, call_preflight, redact_transcript, start_call


def not_reachable(reason: str) -> ResultArtifact:
    """A call that cannot be placed ends at once, unreached, so its caller decides (unverified, hold) without it."""
    call = CallResult(reached=False, findings={"end_reason": "not_reachable", "reason": reason[:200],
                                               "verified": False, "call_accepted": False})
    return ResultArtifact(outcome="unverified", confidence=0.0, model_used="deterministic", findings={
        "call_result": call.model_dump(), "outbound_call_placed": False, "reachable": False, "reason": reason[:200]})


class VoiceDeskAgent(FreightAgent):
    name = "voice_desk"
    async def handle(self, env, meta, task_id, updater):
        from common.providers import mode
        if mode() == "live":
            if env.task_type == "place_call":
                # Decided once, before any attempt: a call already started (including one parked before this check
                # existed) resumes through start_call instead of being re-decided mid-call.
                async def preflight():
                    return None if await call_attempt_exists(task_id) else await call_preflight(env, meta)
                blocked = await self.step(task_id, "call_preflight", preflight)
                if blocked:
                    return not_reachable(blocked)
            from common.tool_loop import run_tool_loop
            return await run_tool_loop(self, env, meta, task_id)
        return await self.run_workflow(env, meta, task_id, updater)

    async def run_workflow(self, env, meta, task_id, updater) -> ResultArtifact:
        if env.task_type == "handle_inbound_call":
            # Assess an already ingested transcript. This path never dials or treats
            # caller speech as financial authorization; W4 still owns event intake.
            source = await SourceRepository().load(env)
            inbound = source.get("inbound_call")
            if not inbound:
                return ResultArtifact(outcome="unverified", findings={"requires_intake": True},
                                      recommended_next=["ingest.voice.inbound.dispute"])
            refs = evidence(source, env)
            if not isinstance(inbound, dict) or inbound.get("source_ref") not in refs:
                raise SourceUnavailable("inbound transcript requires a source evidence reference")
            transcript = inbound.get("transcript")
            if (not isinstance(transcript, list) or not transcript or len(transcript) > 200
                    or any(not isinstance(item, dict) or not isinstance(item.get("text"), str)
                           or not isinstance(item.get("role"), str) for item in transcript)):
                raise SourceUnavailable("inbound transcript must contain source speaker turns")
            return await finish(self, env, task_id, outcome="unverified", confidence=0,
                findings={"requires_intake": True, "source_synthetic": bool(source.get("synthetic")),
                          "transcript_ref": inbound["source_ref"],
                          "transcript_redacted": redact_transcript(transcript),
                          "outbound_call_placed": False, "financial_authorization": False},
                evidence_refs=refs, recommended_next=["ingest.voice.inbound.dispute"])
        from common.demo_calls import DemoCallBlocked
        try:
            attempt_id, result = await start_call(env, meta, task_id)
        except (CallRefused, NotInDirectory, DemoCallBlocked) as refusal:
            return not_reachable(str(refusal))
        except OutreachBlocked:
            return not_reachable("the call has no allowlisted demo destination")
        if result is None:
            data = await self.resume_data(task_id, f"call:{attempt_id}")
            if data:
                result = CallResult.model_validate(data)
            else:
                await self.park(task_id, "awaiting_call", "call", f"call:{attempt_id}",
                                {"attempt_id": attempt_id})
        return ResultArtifact(outcome="partial" if result.reached else "unverified", confidence=0.0,
                              findings={"call_result": result.model_dump()},
                              evidence_refs=[result.transcript_ref] if result.transcript_ref else [])


if __name__ == "__main__":
    serve(VoiceDeskAgent(), DESCRIPTIONS)
