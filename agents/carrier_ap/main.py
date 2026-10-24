from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState

from agents.carrier_ap.card import DESCRIPTIONS
from agents.carrier_ap.tools import three_way_match
from common.envelope import DelegationMeta, ResultArtifact, TaskEnvelope
from common.runtime import FreightAgent
from common.server import serve


class CarrierAPAgent(FreightAgent):
    name = "carrier_ap"

    async def handle(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str,
                     updater: TaskUpdater) -> ResultArtifact:
        match = three_way_match(env.subject)
        if match["matched"]:
            return ResultArtifact(outcome="approve", confidence=0.95, findings=match)
        await updater.update_status(TaskState.TASK_STATE_WORKING,
                                    metadata={"business_status": "awaiting_other_agent", "waiting_on": "risk"})
        child = TaskEnvelope(task_type="investigate_charge", case_id=env.case_id, subject=env.subject,
                             evidence_refs=env.evidence_refs, priority=env.priority)
        risk = await self.delegate("investigate_charge", child, meta, task_id)
        return ResultArtifact(outcome="hold", confidence=0.8, policy_ids=match["policy_ids"],
                              findings={"match": match, "risk": risk},
                              recommended_next=["controller.review_hold"])


if __name__ == "__main__":
    serve(CarrierAPAgent(), DESCRIPTIONS)
