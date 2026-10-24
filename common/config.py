"""Agent registry: ports, skills and who may delegate to whom (PRD section 8)."""
import os
from dataclasses import dataclass, field

HOST = os.getenv("AGENT_HOST", "127.0.0.1")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    title: str
    port: int
    skills: tuple[str, ...]
    may_delegate_to: tuple[str, ...] = field(default_factory=tuple)

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"


AGENTS: dict[str, AgentSpec] = {a.name: a for a in [
    AgentSpec("carrier_ap", "Carrier AP", 9101,
              ("process_carrier_invoice", "propose_payment", "answer_payment_status"),
              ("risk", "controller", "verification")),
    AgentSpec("shipper_ar", "Shipper AR", 9102,
              ("reconcile_customer_payment", "chase_overdue", "set_credit_limit"),
              ("controller", "verification", "voice_desk", "cfo")),
    AgentSpec("cfo", "CFO", 9103, ("assess_financial_impact", "cash_forecast"),
              ("shipper_ar", "carrier_ap", "controller")),
    AgentSpec("risk", "Risk & Compliance", 9104,
              ("investigate_charge", "screen_bank_change", "screen_counterparty"),
              ("verification", "controller")),
    AgentSpec("controller", "Controller", 9105,
              ("review_hold", "check_already_paid", "reconcile", "approve_entry"),
              ("cfo", "verification", "voice_desk")),
    AgentSpec("verification", "Verification", 9106,
              ("verify_claim", "verify_bank_change", "verify_dispute"), ("voice_desk",)),
    AgentSpec("voice_desk", "Voice Desk", 9107, ("place_call", "handle_inbound_call"), ()),
    AgentSpec("validator", "Validator", 9108, ("review_output", "review_flag"), ()),
]}

SKILL_OWNER: dict[str, str] = {s: a.name for a in AGENTS.values() for s in a.skills}

MAX_DELEGATION_DEPTH = 3
MAX_FANOUT = 3

# Terminal outputs of these agents are reviewed by the Validator (PRD Validator loop)
REVIEWED_AGENTS = {"carrier_ap", "shipper_ar", "controller", "cfo"}

# Voice Desk: which caller may request which call purpose (PRD 7.5)
CALL_PURPOSES = {"verification": "verification", "shipper_ar": "collections",
                 "controller": "approvals", "cfo": "cash_brief"}
