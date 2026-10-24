"""Dialer adapter. Live Cartesia outbound is configured by W4 (agent ids, number id); mock otherwise."""
import asyncio
import os

from common.parking import resume_parked
from common.transcripts import scripted


def live() -> bool:
    """Opt-in only: a configured key alone must not route calls into the unwired live branch."""
    return os.getenv("VOICE_PROVIDER") == "cartesia" and bool(os.getenv("CARTESIA_API_KEY"))


async def dial(call_id: str, routed_to: str, persona: str, variables: dict) -> dict:
    if live():
        # TODO(W4): POST to Cartesia's outbound-call API with agent_id per persona, from-number id,
        # routed_to and dynamic variables; the call webhook (gateway /voice/webhook) resumes the task.
        return {"status": "not_configured", "error": "live Cartesia dialing not wired yet"}
    if os.getenv("VOICE_AUTOCOMPLETE", "1") == "1":
        asyncio.get_running_loop().create_task(_mock_call(call_id, variables.get("script", "detention_dispute")))
    return {"status": "ringing", "provider": "mock"}


async def _mock_call(call_id: str, script: str) -> None:
    await asyncio.sleep(float(os.getenv("VOICE_MOCK_DELAY_S", "2")))
    await resume_parked(call_id, {"kind": "call_ended", "status": "completed", "transcript": scripted(script)})
