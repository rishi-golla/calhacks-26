"""Deterministic controls (PRD section 9). Called before every state-changing tool.
SpacetimeDB reducers enforce the ledger rules again (defense in depth)."""
from dataclasses import dataclass, field
from typing import Literal

APPROVAL_THRESHOLD_USD = 5_000
CONTRACT_TOLERANCE = 0.02
# P-013 = signed POD required; P-014 = accessorials need a receipt.


@dataclass
class Decision:
    result: Literal["allow", "deny", "needs_approval"]
    policy_ids: list[str] = field(default_factory=list)
    reason: str = ""


def check(action: str, actor: str, payload: dict) -> Decision:
    if action == "propose_payment":
        if payload.get("on_hold"):
            return Decision("deny", ["P-002"], "invoice has an open hold")
        if payload["amount"] > APPROVAL_THRESHOLD_USD:
            return Decision("needs_approval", ["P-003"], "payment above threshold")
    if action == "evaluate_invoice":
        policy_ids, reasons = [], []
        dup = payload.get("duplicate_of")
        if dup:
            policy_ids.append("P-007")
            reasons.append(f"duplicate of {dup} (same load/BOL)")
        if payload.get("pod_signed") is False:
            policy_ids.append("P-013")
            reasons.append("no signed POD")
        if (any(any(term in item.get("desc", "").lower() for term in ("detention", "lumper"))
                for item in payload.get("line_items", [])) and payload.get("receipt_attached") is not True):
            policy_ids.append("P-014")
            reasons.append("accessorial billed without receipt")
        remit = (payload.get("remit_to") or {}).get("name")
        carrier = (payload.get("carrier") or {}).get("name")
        if remit and remit.strip().lower() != (carrier or "").strip().lower() \
                and not (payload.get("carrier") or {}).get("factor"):
            policy_ids.append("P-004")
            reasons.append(f"remit-to {remit} is not the carrier and no NOA is on file")
        limit = payload["contract_rate"] * (1 + CONTRACT_TOLERANCE)
        if payload["invoice_amount"] > limit:
            policy_ids.append("P-001")
            reasons.append(f"invoice exceeds contract by {payload['invoice_amount'] - payload['contract_rate']:.2f}")
        if policy_ids:
            return Decision("needs_approval", policy_ids, "; ".join(reasons))
    if action in ("release_hold", "post_journal_entry", "approve"):
        if payload.get("requested_by") and payload.get("requested_by") == actor:
            return Decision("deny", ["P-005"], "requester cannot approve")
        if action == "release_hold" and payload.get("amount", 0) > APPROVAL_THRESHOLD_USD \
                and not payload.get("human_approval_id"):
            return Decision("needs_approval", ["P-003"], "release above threshold needs a human")
    if action == "post_journal_entry":
        lines = payload.get("lines", [])
        if round(sum(x.get("debit", 0) for x in lines) - sum(x.get("credit", 0) for x in lines), 2) != 0:
            return Decision("deny", ["P-006"], "unbalanced entry")
    if action == "contact_counterparty" and not payload.get("directory_verified"):
        return Decision("deny", ["P-008"], "contact target not from verified directory")
    if action == "change_policy" and actor == "validator":
        return Decision("deny", ["P-012"], "validator cannot change policy, limits or permissions")
    return Decision("allow")
