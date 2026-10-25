"""Identity of the first hop, shared by the router (harness.ingest) and the dispatch worker that delivers
SpacetimeDB `first_hop` outbox rows. Lives outside the harness so the worker can rebuild the same
DelegationMeta without importing the harness package (which pulls in the vault)."""
from common.envelope import DelegationMeta, TaskEnvelope


def first_hop_meta(event_id: str, env: TaskEnvelope) -> DelegationMeta:
    """Identity conventions shared with the dispatcher: the agent dedupes on event:<id>; the case correlates."""
    return DelegationMeta(case_id=env.case_id, from_agent="router", delegation_depth=0, visited=[],
                          idempotency_key=f"event:{event_id}", correlation_id=env.case_id, causation_id=event_id,
                          priority=env.priority)
