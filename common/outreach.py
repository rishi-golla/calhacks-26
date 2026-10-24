"""Outreach guard (PRD W4 R5, P-011). Every call/email target goes through route_target().
The real contact is kept as intended_to; only allowlisted team targets are ever dialed or emailed."""
import os
from dataclasses import dataclass


class OutreachBlocked(Exception):
    pass


@dataclass(frozen=True)
class RoutedTarget:
    intended_to: str
    routed_to: str
    mode: str


def _allowlist(channel: str) -> list[str]:
    raw = os.getenv("DEMO_ALLOWLIST_PHONES" if channel == "phone" else "DEMO_ALLOWLIST_EMAILS", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def route_target(channel: str, intended_to: str, demo_target: str | None = None) -> RoutedTarget:
    allow = _allowlist(channel)
    if not allow:
        raise OutreachBlocked(f"no {channel} allowlist configured")
    routed = demo_target or allow[0]
    # Enforced regardless of OUTREACH_MODE: nothing outside the allowlist is ever contacted.
    if routed not in allow:
        raise OutreachBlocked(f"{routed} is not on the demo allowlist")
    return RoutedTarget(intended_to=intended_to, routed_to=routed, mode=os.getenv("OUTREACH_MODE", "demo"))
