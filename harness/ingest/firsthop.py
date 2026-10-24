"""Deterministic first hop: (agent, TaskEnvelope, DelegationMeta) for a typed event, with no model calls.

The full intake path (harness.ingest.pipeline.process) adds model extraction, classification and verification;
this is the typed fast path and the stable helper other services and tests use (exported as
router.main.route_event). It still redacts with the rules layer, so it never hands raw personal data to an agent.
"""
from common.config import SKILL_OWNER
from common.envelope import DelegationMeta, TaskEnvelope
from harness.ingest.redact import known_spans, redact_obj, replace_spans
from harness.ingest.routing import table_route
from harness.ingest.vault import Vault

_vault: Vault | None = None


def first_hop_meta(event_id: str, env: TaskEnvelope) -> DelegationMeta:
    """Identity conventions shared with the dispatcher: the agent dedupes on event:<id>; the case correlates."""
    return DelegationMeta(case_id=env.case_id, from_agent="router", delegation_depth=0, visited=[],
                          idempotency_key=f"event:{event_id}", correlation_id=env.case_id, causation_id=event_id,
                          priority=env.priority)


def route_event(event: dict, vault: Vault | None = None) -> tuple[str, TaskEnvelope, DelegationMeta]:
    """Raises ValueError/TypeError for events that cannot be routed deterministically (the consumer
    dead-letters or triages those)."""
    from harness.ingest.pipeline import P_ORDER, case_id_for  # local: pipeline imports the dispatcher

    global _vault
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        raise TypeError("event payload must be an object")
    route = table_route(event.get("type") or "", payload)
    if route is None or route.skill is None:
        raise ValueError("event needs an owning intake route")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id is required for durable deduplication")
    case_id = case_id_for(event)
    if case_id is None:
        raise ValueError("intake must resolve a case_id before routing")
    vault = vault or _vault or Vault()
    _vault = vault
    clean, tokens = redact_obj(payload, vault)
    clean = replace_spans(clean, known_spans(tokens, vault))
    refs = [f"event:{event_id}"] + [r for r in payload.get("evidence_refs", []) if isinstance(r, str)]
    env = TaskEnvelope(task_type=route.skill, case_id=case_id, subject=clean, evidence_refs=refs,
                       priority=event.get("priority") if event.get("priority") in P_ORDER else "P2")
    return SKILL_OWNER[route.skill], env, first_hop_meta(event_id, env)
