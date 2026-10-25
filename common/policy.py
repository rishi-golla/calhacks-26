"""Deterministic controls (PRD section 9). Called before every state-changing tool.
SpacetimeDB reducers enforce the ledger rules again (defense in depth)."""
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

APPROVAL_THRESHOLD_USD = 5_000
CONTRACT_TOLERANCE = 0.02


@dataclass
class Decision:
    result: Literal["allow", "deny", "needs_approval"]
    policy_ids: list[str] = field(default_factory=list)
    reason: str = ""


def _usd(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("amount must be a number")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("amount must be a number") from None
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    return amount


def invoice_hold_exposure(holds: Iterable[Mapping[str, Any]]) -> Decimal:
    """P-003 base for release_hold: the original amounts of every hold ever placed on one
    invoice, released holds included. Holds are never deleted or shrunk, so this total only
    grows, and at most the threshold of an invoice can be released without a human."""
    return sum((_usd(hold["amount"]) for hold in holds), Decimal(0))


def release_hold_needs_approval(payload: Mapping[str, Any]) -> bool:
    """Mirror of the release_hold reducer. ``invoice_hold_total`` is invoice_hold_exposure() of
    the hold's invoice; ``hold_amount`` is the hold's original amount; ``amount`` the release."""
    keys = ("amount", "hold_amount", "invoice_hold_total")
    return any(_usd(payload[key]) > APPROVAL_THRESHOLD_USD for key in keys if key in payload)


def check(action: str, actor: str, payload: dict) -> Decision:
    if action == "propose_payment":
        if payload.get("on_hold"):
            return Decision("deny", ["P-002"], "invoice has an open hold")
        if payload["amount"] > APPROVAL_THRESHOLD_USD:
            return Decision("needs_approval", ["P-003"], "payment above threshold")
    if action == "evaluate_invoice":
        limit = payload["contract_rate"] * (1 + CONTRACT_TOLERANCE)
        if payload["invoice_amount"] > limit:
            return Decision("needs_approval", ["P-001"],
                            f"invoice exceeds contract by {payload['invoice_amount'] - payload['contract_rate']:.2f}")
    if action in ("release_hold", "post_journal_entry", "approve"):
        if payload.get("requested_by") and payload.get("requested_by") == actor:
            return Decision("deny", ["P-005"], "requester cannot approve")
        # Split or partially released holds cannot dodge P-003: the threshold applies to
        # everything held on the invoice, not to this release or this hold alone.
        if action == "release_hold" and release_hold_needs_approval(payload) \
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
