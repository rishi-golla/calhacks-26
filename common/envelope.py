"""Shared A2A payload contracts (PRD section 7). Frozen; change only with both owners."""
from typing import Any, Literal

from pydantic import BaseModel, Field

Priority = Literal["P0", "P1", "P2", "P3"]
BusinessStatus = Literal[
    "queued", "investigating", "awaiting_other_agent", "awaiting_external_verification",
    "awaiting_call", "awaiting_human_approval", "resolved", "rejected", "failed",
]
Outcome = Literal["clear", "suspicious", "verified", "unverified", "hold", "approve", "reject",
                  "partial", "pass", "issue", "not_implemented"]


class TaskEnvelope(BaseModel):
    task_type: str
    case_id: str
    display_id: str | None = None
    subject: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    requested_output: str | None = None
    priority: Priority = "P2"
    deadline_s: int = 120


class DelegationMeta(BaseModel):
    """Goes in A2A Message.metadata."""
    case_id: str
    from_agent: str
    parent_task_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    delegation_depth: int = 0
    visited: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None
    priority: Priority = "P2"


class ResultArtifact(BaseModel):
    outcome: Outcome
    confidence: float = 0.0
    findings: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    policy_ids: list[str] = Field(default_factory=list)
    model_used: str | None = None
    recommended_next: list[str] = Field(default_factory=list)


class PlaceCall(BaseModel):
    purpose: Literal["verification", "collections", "approvals", "cash_brief"]
    counterparty_id: str  # resolved via the verified directory, NEVER a phone number from the payload
    questions: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)


class CallResult(BaseModel):
    reached: bool
    intended_to: str | None = None
    routed_to: str | None = None
    findings: dict[str, Any] = Field(default_factory=dict)
    transcript_ref: str | None = None


class Friction(BaseModel):
    kind: Literal["retry", "reject", "policy_deny", "timeout", "missing_doc", "redelegation",
                  "human_override", "flag"]
    detail: str
    cost_s: float = 0.0


class Lesson(BaseModel):
    agent: str
    type: Literal["memory", "instruction"]
    scope: dict[str, Any] = Field(default_factory=dict)
    text_or_diff: str
    confidence: float
    evidence_refs: list[str] = Field(default_factory=list)


class ReviewOutput(BaseModel):
    verdict: Literal["pass", "issue"]
    checks: list[dict[str, Any]] = Field(default_factory=list)
    friction: list[Friction] = Field(default_factory=list)
    lessons: list[Lesson] = Field(default_factory=list)
    actions: list[Literal["reopen_case", "rerun_task", "none"]] = Field(default_factory=lambda: ["none"])
