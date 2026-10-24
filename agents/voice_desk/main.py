from agents.voice_desk.card import DESCRIPTIONS
from common.envelope import CallResult, ResultArtifact
from common.runtime import FreightAgent
from common.server import serve
from common.voice import start_call


class VoiceDeskAgent(FreightAgent):
    name = "voice_desk"
    async def handle(self, env, meta, task_id, updater) -> ResultArtifact:
        if env.task_type == "handle_inbound_call":
            # W4 normalizes inbound disputes into business events before first-hop dispatch.
            return ResultArtifact(outcome="unverified", findings={"requires_intake": True},
                                  recommended_next=["ingest.voice.inbound.dispute"])
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
