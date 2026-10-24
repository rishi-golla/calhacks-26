from a2a.server.tasks import TaskUpdater

from agents.risk.card import DESCRIPTIONS
from agents.risk.tools import gps_dwell_minutes
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.runtime import FreightAgent
from common.server import serve


class RiskAgent(FreightAgent):
    name = "risk"

    async def handle(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str,
                     updater: TaskUpdater) -> ResultArtifact:
        s = env.subject
        dwell = gps_dwell_minutes(s)
        free = s.get("free_time_min", 120)
        claimed = s.get("detention_hours_claimed", 0)
        suspicious = dwell is not None and claimed > 0 and dwell <= free
        return ResultArtifact(
            outcome="suspicious" if suspicious else "clear",
            confidence=0.85 if suspicious else 0.6,
            findings={"gps_dwell_min": dwell, "free_time_min": free, "detention_hours_claimed": claimed,
                      "needs_verification": suspicious},
            recommended_next=["verification.verify_claim"] if suspicious else [],
        )


if __name__ == "__main__":
    serve(RiskAgent(), DESCRIPTIONS)
