/**
 * Canonical agent roster for the FreightLine UI.
 *
 * Service IDs match `common/config.py`, the SpacetimeDB policy allowlist and
 * `task.agent` / `audit_event.agent` values. Display names are the brokerage
 * job titles from PRD §8. Colors are the mascot body colors (see
 * `public/mascots/`), so a node, a badge and a character always agree.
 */
export type AgentId =
  | "carrier_ap"
  | "shipper_ar"
  | "cfo"
  | "risk"
  | "controller"
  | "verification"
  | "voice_desk"
  | "validator"
  | "documents";

/** Pseudo-nodes that appear on the graph but are not A2A services. */
export type NodeId = AgentId | "intake" | "human";

export type AgentMeta = {
  id: AgentId;
  name: string;
  /** Two-letter chip used where there is no room for the mascot. */
  short: string;
  /** Friendly nickname used in speech bubbles and playful copy. */
  nick: string;
  role: string;
  /** Mascot body color (hex). */
  color: string;
  /** Darker companion for borders / text on light surfaces. */
  colorDeep: string;
  port: number;
  skills: string[];
  /** Agents this agent may delegate to (PRD §8, `common/config.py`). */
  delegatesTo: AgentId[];
  /** Frontline specialists face the business; support specialists serve them. */
  tier: "frontline" | "support";
  /** Mascot image set under /public/mascots/<id>/ */
  mascot: string;
};

export const AGENT_META: Record<AgentId, AgentMeta> = {
  carrier_ap: {
    id: "carrier_ap",
    name: "Carrier Settlement",
    short: "CS",
    nick: "Haul",
    role: "Carrier invoices & payment readiness",
    color: "#2F6BFF",
    colorDeep: "#1C47B8",
    port: 9101,
    skills: ["process_carrier_invoice", "propose_payment", "answer_payment_status"],
    delegatesTo: ["risk", "controller", "verification", "documents"],
    tier: "frontline",
    mascot: "carrier_ap",
  },
  shipper_ar: {
    id: "shipper_ar",
    name: "Shipper Billing & Collections",
    short: "AR",
    nick: "Ledger",
    role: "Billing packets, short-pay & collections",
    color: "#A86B3C",
    colorDeep: "#7A4A26",
    port: 9102,
    skills: ["reconcile_customer_payment", "chase_overdue", "set_credit_limit"],
    delegatesTo: ["controller", "verification", "voice_desk", "cfo", "documents"],
    tier: "frontline",
    mascot: "shipper_ar",
  },
  cfo: {
    id: "cfo",
    name: "Load Profitability",
    short: "LP",
    nick: "Margin",
    role: "Load margin & cash impact",
    color: "#FFC627",
    colorDeep: "#B88A0C",
    port: 9103,
    skills: ["assess_financial_impact", "cash_forecast"],
    delegatesTo: ["shipper_ar", "carrier_ap", "controller"],
    tier: "frontline",
    mascot: "cfo",
  },
  risk: {
    id: "risk",
    name: "Accessorial Audit",
    short: "AA",
    nick: "Scout",
    role: "Detention, receipts & extra charges",
    color: "#F0338F",
    colorDeep: "#B01E66",
    port: 9104,
    skills: ["investigate_charge", "screen_bank_change", "screen_counterparty"],
    delegatesTo: ["verification", "controller", "documents"],
    tier: "frontline",
    mascot: "risk",
  },
  controller: {
    id: "controller",
    name: "Settlement Approvals",
    short: "CT",
    nick: "Stamp",
    role: "Holds, reconciliation & journal posting",
    color: "#5A6B85",
    colorDeep: "#3B475C",
    port: 9105,
    skills: ["review_hold", "check_already_paid", "reconcile", "approve_entry"],
    delegatesTo: ["cfo", "verification", "voice_desk", "documents"],
    tier: "support",
    mascot: "controller",
  },
  verification: {
    id: "verification",
    name: "Carrier Trust & Verification",
    short: "CV",
    nick: "Shield",
    role: "Carrier identity & payment verification",
    color: "#8B5CF6",
    colorDeep: "#5F38C2",
    port: 9106,
    skills: ["verify_claim", "verify_bank_change", "verify_dispute"],
    delegatesTo: ["voice_desk"],
    tier: "frontline",
    mascot: "verification",
  },
  voice_desk: {
    id: "voice_desk",
    name: "Dispatcher Outreach",
    short: "VD",
    nick: "Ringo",
    role: "Demo-routed phone conversations",
    color: "#FF7A1A",
    colorDeep: "#C25306",
    port: 9107,
    skills: ["place_call", "handle_inbound_call"],
    delegatesTo: [],
    tier: "support",
    mascot: "voice_desk",
  },
  validator: {
    id: "validator",
    name: "Settlement Quality Review",
    short: "VA",
    nick: "Checks",
    role: "Review, friction & continuous learning",
    color: "#17B3A6",
    colorDeep: "#0E7F76",
    port: 9108,
    skills: ["review_output", "review_flag"],
    delegatesTo: [],
    tier: "support",
    mascot: "validator",
  },
  documents: {
    id: "documents",
    name: "POD & Document Chase",
    short: "DC",
    nick: "Paper",
    role: "Delivery paperwork & missing receipts",
    color: "#F0405A",
    colorDeep: "#B4243A",
    port: 9109,
    skills: ["collect_documents", "check_document_packet", "receive_document"],
    delegatesTo: ["voice_desk"],
    tier: "frontline",
    mascot: "documents",
  },
};

export const AGENT_IDS = Object.keys(AGENT_META) as AgentId[];
export const AGENT_LIST = AGENT_IDS.map((id) => AGENT_META[id]);

/** Non-service nodes shown on graphs. */
export const NODE_META: Record<
  "intake" | "human",
  { id: "intake" | "human"; name: string; short: string; nick: string; role: string; color: string; colorDeep: string; mascot: string }
> = {
  intake: {
    id: "intake",
    name: "Intake & Routing",
    short: "IN",
    nick: "Greeter",
    role: "Classifies new events and dispatches the first hop",
    color: "#1FB37A",
    colorDeep: "#117A52",
    mascot: "intake",
  },
  human: {
    id: "human",
    name: "Human controller",
    short: "HC",
    nick: "You",
    role: "Approves, rejects and flags",
    color: "#E8E6E1",
    colorDeep: "#8A8781",
    mascot: "human",
  },
};

export const isAgentId = (value: unknown): value is AgentId =>
  typeof value === "string" && value in AGENT_META;

export const agentMeta = (id: unknown): AgentMeta | undefined =>
  isAgentId(id) ? AGENT_META[id] : undefined;

export const agentLabel = (id: unknown): string =>
  agentMeta(id)?.name ??
  (id === "intake" ? NODE_META.intake.name : id === "human" ? NODE_META.human.name : String(id ?? "unknown"));

export const agentColor = (id: unknown): string =>
  agentMeta(id)?.color ??
  (id === "intake" ? NODE_META.intake.color : id === "human" ? NODE_META.human.color : "#9AA3B2");

/** Default mascot image for a node id and state. */
export const mascotSrc = (
  id: NodeId | string,
  state: "idle" | "working" | "alert" = "idle",
  /** 320 px companion for small renders (~70 KB instead of ~620 KB). */
  small = false,
) => `/mascots/${id}/${state}${small ? "@320" : ""}.png`;

/** Human-readable skill names. */
export const SKILL_LABEL: Record<string, string> = {
  process_carrier_invoice: "Process carrier invoice",
  propose_payment: "Propose payment",
  answer_payment_status: "Answer payment status",
  reconcile_customer_payment: "Reconcile customer payment",
  chase_overdue: "Chase overdue invoice",
  set_credit_limit: "Recommend credit limit",
  assess_financial_impact: "Assess financial impact",
  cash_forecast: "Update cash forecast",
  investigate_charge: "Investigate charge",
  screen_bank_change: "Screen bank change",
  screen_counterparty: "Screen counterparty",
  review_hold: "Review hold",
  check_already_paid: "Check already paid",
  reconcile: "Reconcile",
  approve_entry: "Approve journal entry",
  verify_claim: "Verify claim",
  verify_bank_change: "Verify bank change",
  verify_dispute: "Verify dispute",
  place_call: "Place call",
  handle_inbound_call: "Handle inbound call",
  review_output: "Review output",
  review_flag: "Review flag",
  collect_documents: "Collect documents",
  check_document_packet: "Check document packet",
  receive_document: "Receive document",
};
export const skillLabel = (skill: unknown) =>
  SKILL_LABEL[String(skill)] ?? String(skill ?? "task").replaceAll("_", " ");
