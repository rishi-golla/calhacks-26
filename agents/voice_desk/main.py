from agents.voice_desk.card import DESCRIPTIONS
from common.agent_support import evidence, finish
from common.envelope import CallResult, ResultArtifact
from common.runtime import FreightAgent
from common.server import serve
from common.source_repository import SourceRepository, SourceUnavailable
from common.voice import redact_transcript, start_call


class VoiceDeskAgent(FreightAgent):
    name = "voice_desk"
    async def handle(self, env, meta, task_id, updater):
        from common.providers import mode
        if mode() == "live":
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
        attempt_id, result = await start_call(env, meta, task_id)
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
