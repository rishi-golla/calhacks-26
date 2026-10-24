"""Agent registry: ports, skills and who may delegate to whom (PRD section 8)."""
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

HOST = os.getenv("AGENT_HOST", "127.0.0.1")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    title: str
    port: int
    skills: tuple[str, ...]
    may_delegate_to: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        port = int(os.getenv(f"AGENT_PORT_{self.name.upper()}", str(self.port)))
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid port for {self.name}")
        object.__setattr__(self, "port", port)

    @property
    def url(self) -> str:
        value = os.getenv(f"AGENT_URL_{self.name.upper()}", f"http://{HOST}:{self.port}").rstrip("/")
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError(f"Invalid advertised URL for {self.name}")
        return value


AGENTS: dict[str, AgentSpec] = {a.name: a for a in [
    AgentSpec("carrier_ap", "Carrier Settlement", 9101,
              ("process_carrier_invoice", "propose_payment", "answer_payment_status"),
              ("risk", "controller", "verification", "documents")),
    AgentSpec("shipper_ar", "Shipper Billing & Collections", 9102,
              ("reconcile_customer_payment", "chase_overdue", "set_credit_limit"),
              ("controller", "verification", "voice_desk", "cfo", "documents")),
    AgentSpec("cfo", "Load Profitability", 9103, ("assess_financial_impact", "cash_forecast"),
              ("shipper_ar", "carrier_ap", "controller")),
    AgentSpec("risk", "Accessorial Audit", 9104,
              ("investigate_charge", "screen_bank_change", "screen_counterparty"),
              ("verification", "controller", "documents")),
    AgentSpec("controller", "Settlement Approvals", 9105,
              ("review_hold", "check_already_paid", "reconcile", "approve_entry"),
              ("cfo", "verification", "voice_desk", "documents")),
    AgentSpec("verification", "Carrier Trust & Verification", 9106,
              ("verify_claim", "verify_bank_change", "verify_dispute"), ("voice_desk",)),
    AgentSpec("voice_desk", "Dispatcher Outreach", 9107, ("place_call", "handle_inbound_call"), ()),
    AgentSpec("validator", "Settlement Quality Review", 9108, ("review_output", "review_flag"), ()),
    AgentSpec("documents", "POD & Document Chase", 9109,
              ("collect_documents", "check_document_packet"), ("voice_desk",)),
]}

SKILL_OWNER: dict[str, str] = {s: a.name for a in AGENTS.values() for s in a.skills}

MAX_DELEGATION_DEPTH = 3
MAX_FANOUT = 3

# Voice Desk: which caller may request which call purpose (PRD 7.5)
CALL_PURPOSES = {"verification": "verification", "documents": "verification", "shipper_ar": "collections",
                 "controller": "approvals", "cfo": "cash_brief"}
