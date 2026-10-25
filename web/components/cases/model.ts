/**
 * Case-scoped selectors for the explorer and the execution trace.
 *
 * Everything here is derived from rows that actually exist for the case:
 * dispatch, task, delegation_edge, audit_event (tool calls), doc_read,
 * approval, review. Nothing is predetermined, so two cases of the same
 * scenario can produce different graphs, exactly as in production.
 */
import { emptySnapshot, json, num, str, timestamp, type Row, type Snapshot } from "@/lib/data";
import {
  buildTimeline,
  buildToolCalls,
  taskView,
  type CaseView,
  type TaskView,
  type TimelineEvent,
  type ToolCallView,
} from "@/lib/ops";

/* ------------------------------ helpers ------------------------------ */

export const clock = (ms: number) =>
  ms ? new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "—";

export const shortDur = (ms: number) => {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1_000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  if (s < 3_600) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(s / 3_600)}h ${String(Math.round((s % 3_600) / 60)).padStart(2, "0")}m`;
};

/** mm:ss (or h:mm:ss) for running timers and the scrubber readout. */
export const timer = (ms: number) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
};

/** Mask every digit of a phone number except the last two. */
export const maskPhone = (value: string) => {
  let digits = value.replace(/\D/g, "").length;
  return value.replace(/\d/g, (d) => (--digits < 2 ? d : "•"));
};

/**
 * When an approval was requested. The live `approval` table has no
 * `requested_at`; fall back to the matching request_approval audit row, then
 * to the one-hour expiry window.
 */
export function approvalRequestedAt(a: Row, audit: Row[]): number {
  const direct = timestamp(a.requested_at);
  if (direct) return direct;
  const row = audit.find((e) => e.kind === "request_approval" && e.task_id === a.task_id);
  return (row ? timestamp(row.ts) : 0) || (timestamp(a.expires_at) ? timestamp(a.expires_at) - 3_600_000 : 0);
}

export type CaseKind = "payable" | "receivable" | "bank_change" | "case";

const SCENARIO_KIND: Record<string, CaseKind> = {
  clean_invoice: "payable",
  detention_unsupported: "payable",
  lumper_no_receipt: "payable",
  missing_pod: "payable",
  duplicate_invoice: "payable",
  rate_mismatch: "payable",
  bank_change: "bank_change",
  shipper_short_pay: "receivable",
  overdue_collections: "receivable",
  customer_payment: "receivable",
};
const SKILL_KIND: Record<string, CaseKind> = {
  process_carrier_invoice: "payable",
  propose_payment: "payable",
  answer_payment_status: "payable",
  investigate_charge: "payable",
  screen_bank_change: "bank_change",
  verify_bank_change: "bank_change",
  reconcile_customer_payment: "receivable",
  chase_overdue: "receivable",
  set_credit_limit: "receivable",
};

/** Business kind of a case: from the scenario when known, else from the first task's skill. */
export function caseKind(c: CaseView): CaseKind {
  if (c.scenario && SCENARIO_KIND[c.scenario]) return SCENARIO_KIND[c.scenario];
  if (/-BANK$/i.test(c.caseId)) return "bank_change";
  if (/-AR$/i.test(c.caseId)) return "receivable";
  const root = c.tasks.find((t) => !t.parentTaskId) ?? c.tasks[0];
  return (root && SKILL_KIND[root.skill]) || (c.tasks.length ? "payable" : "case");
}
export const KIND_LABEL: Record<CaseKind, string> = { payable: "Payable", receivable: "Receivable", bank_change: "Bank change", case: "Case" };

/* ---------------------------- case slice ---------------------------- */

/**
 * The rows that belong to one case. `doc_read` has no `case_id` column in the
 * live schema, so it is joined through the case's task ids; a `case_id` and a
 * best-effort `ts` are stamped on so the shared timeline selector sees them.
 */
export function sliceCase(snapshot: Snapshot, caseId: string): Snapshot {
  const out = emptySnapshot();
  const byCase = (rows: Row[]) => rows.filter((r) => r.case_id === caseId);
  out.case = byCase(snapshot.case);
  out.task = byCase(snapshot.task);
  out.dispatch = byCase(snapshot.dispatch);
  out.delegation_edge = byCase(snapshot.delegation_edge);
  out.audit_event = byCase(snapshot.audit_event);
  out.hold = byCase(snapshot.hold);
  out.payment_proposal = byCase(snapshot.payment_proposal);
  out.approval = byCase(snapshot.approval);
  out.ledger_entry = byCase(snapshot.ledger_entry);
  out.call = byCase(snapshot.call);
  out.review = byCase(snapshot.review);
  out.flag = byCase(snapshot.flag);
  out.cash_forecast = byCase(snapshot.cash_forecast);
  out.invoice = byCase(snapshot.invoice);
  const tasks = new Map(out.task.map((r) => [str(r.task_id, ""), r]));
  // Live doc reads carry no timestamp: borrow the k-th record_doc_read audit row of the same task, else the task start.
  const docAudits = new Map<string, number[]>();
  for (const a of out.audit_event)
    if (a.kind === "record_doc_read") {
      const list = docAudits.get(str(a.task_id, "")) ?? [];
      list.push(timestamp(a.ts));
      docAudits.set(str(a.task_id, ""), list);
    }
  for (const list of docAudits.values()) list.sort((a, b) => a - b);
  const seen = new Map<string, number>();
  out.doc_read = snapshot.doc_read
    .filter((r) => r.case_id === caseId || tasks.has(str(r.task_id, "")))
    .sort((a, b) => timestamp(a.ts) - timestamp(b.ts) || str(a.id).localeCompare(str(b.id)))
    .map((r) => {
      const taskId = str(r.task_id, "");
      const k = seen.get(taskId) ?? 0;
      seen.set(taskId, k + 1);
      const ts = timestamp(r.ts) || docAudits.get(taskId)?.[k] || timestamp(tasks.get(taskId)?.started_at) || 0;
      return { ...r, case_id: caseId, ts: ts ? new Date(ts).toISOString() : "" };
    });
  return out;
}

/* ------------------------------ the trace ------------------------------ */

export type NodeStatus = "queued" | "working" | "waiting" | "completed" | "failed" | "canceled" | "approved" | "rejected" | "pending";

export type DocRead = {
  id: string;
  taskId: string;
  agent: string;
  docRef: string;
  page: number;
  span: string;
  extracted: unknown;
  at: number;
};

export type TraceNode = {
  id: string;
  kind: "intake" | "task" | "human";
  lane: string;
  start: number;
  /** Terminal time, 0 while open. */
  end: number;
  task?: TaskView;
  dispatch?: Row;
  approval?: Row;
  tools: ToolCallView[];
  docs: DocRead[];
  /** Human-approval windows that parked this task, [requested, decided || 0]. */
  waits: [number, number][];
  height: number;
};

/**
 * A shipment lifecycle event the router logged without routing (`not_routed`
 * dispatch rows: tendered, dispatched, delivered, GPS). Context, not a
 * decision: drawn as a tick on the Intake lane, never as a card.
 */
export type TraceMilestone = {
  id: string;
  /** Ingest event type, e.g. `load.delivered`; "" when the reason names none. */
  type: string;
  /** Short plain label for the tick, e.g. "Delivered". */
  label: string;
  at: number;
  /** Every time this tick stands for: one, or each GPS ping when pings are collapsed. */
  times: number[];
  row: Row;
};

export type TraceEdge = {
  id: string;
  kind: "dispatch" | "delegation" | "review" | "approval" | "decision" | "retry";
  from: string;
  to: string;
  /** Time the edge leaves its source. */
  at: number;
  skill: string;
  row?: Row;
};

export type SideEffect = {
  id: string;
  kind: "hold" | "payment" | "approval" | "journal" | "forecast";
  at: number;
  /** Time the status changed after `at` (release, decision), 0 if never. */
  changedAt: number;
  label: string;
  amount: number;
  status: string;
  /** Status before `changedAt`. */
  initialStatus: string;
  taskId: string;
  row: Row;
};

export type CaseTrace = {
  caseId: string;
  slice: Snapshot;
  nodes: TraceNode[];
  nodeById: Map<string, TraceNode>;
  edges: TraceEdge[];
  /** Lifecycle events logged without routing, chronological. */
  milestones: TraceMilestone[];
  timeline: TimelineEvent[];
  sideEffects: SideEffect[];
  /** Every audit row for a task, chronological (includes upsert_task rows the timeline hides). */
  auditByTask: Map<string, Row[]>;
  toolCount: number;
  first: number;
  last: number;
  /** Any task still open or approval still pending. */
  open: boolean;
};

/* ------------------------- lifecycle milestones ------------------------- */

const EVENT_LABEL: Record<string, string> = {
  "load.tendered": "Load tendered",
  "load.dispatched": "Load dispatched",
  "load.delivered": "Load delivered",
  "gps.ping": "GPS ping",
  "pod.received": "Proof of delivery received",
  "carrier_invoice.received": "Carrier invoice received",
  "shipper_payment.received": "Shipper payment received",
  "bank.event": "Bank event",
  "bank_change.requested": "Bank change requested",
  "voice.inbound.dispute": "Dispute call received",
  "email.received": "Email received",
  "scenario.injected": "Scenario injected",
};
const MILESTONE_LABEL: Record<string, string> = {
  "load.tendered": "Tendered",
  "load.dispatched": "Dispatched",
  "load.delivered": "Delivered",
};

/** Plain words for an ingest event type: `load.delivered` → "Load delivered". */
export function eventLabel(type: string): string {
  if (EVENT_LABEL[type]) return EVENT_LABEL[type];
  const words = type.replace(/[._]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Event";
}

/** Short tick label: "Tendered", "Dispatched", "Delivered", else the event's plain words. */
export const milestoneLabel = (type: string) => MILESTONE_LABEL[type] ?? (type ? eventLabel(type) : "Logged");

const NOT_ROUTED_REASON = /^(?:scenario -> )?([\w.]+) is not routed(?: \(7\.3\))?$/;
const TABLE_REASON = /^7\.3 table: (?:scenario -> )?([\w.]+)$/;

/** The event type a router reason names ("load.delivered is not routed (7.3)"), "" if none. */
export function dispatchEventType(reason: string): string {
  return (reason.match(NOT_ROUTED_REASON) ?? reason.match(TABLE_REASON))?.[1] ?? "";
}

/** A router reason in plain words: no PRD section numbers, no raw event types. */
export function dispatchReason(reason: string): string {
  const notRouted = reason.match(NOT_ROUTED_REASON);
  if (notRouted) return `${eventLabel(notRouted[1])} · logged`;
  const table = reason.match(TABLE_REASON);
  if (table) return eventLabel(table[1]);
  let out = reason.replace(/\s*\(7\.3\)/g, "").replace(/\b7\.3 table:\s*/g, "");
  for (const [type, label] of Object.entries(EVENT_LABEL)) out = out.replaceAll(type, label.toLowerCase());
  return out.trim();
}

/** A `not_routed` dispatch is a logged lifecycle event, not a routing decision. */
export const isMilestone = (d: Row) => str(d.status, "") === "not_routed";

/**
 * Splits dispatch rows into routing decisions (routed, triage, duplicate: drawn
 * as cards) and lifecycle milestones (drawn as ticks). GPS pings collapse into
 * one milestone at the first ping. `fallbackAt` stands in for a missing `ts`.
 */
export function splitDispatches(rows: Row[], fallbackAt = 0): { decisions: Row[]; milestones: TraceMilestone[] } {
  const decisions: Row[] = [];
  const milestones: TraceMilestone[] = [];
  let gps: TraceMilestone | undefined;
  for (const d of [...rows].sort((a, b) => timestamp(a.ts) - timestamp(b.ts))) {
    if (!isMilestone(d)) {
      decisions.push(d);
      continue;
    }
    const at = timestamp(d.ts) || fallbackAt;
    const type = dispatchEventType(str(d.reason, ""));
    if (type === "gps.ping" && gps) {
      gps.times.push(at);
      continue;
    }
    const m: TraceMilestone = { id: `milestone:${str(d.event_id)}`, type, label: milestoneLabel(type), at, times: [at], row: d };
    if (type === "gps.ping") gps = m;
    milestones.push(m);
  }
  return { decisions, milestones };
}

/** The tick's text at `cursor` (Infinity = live), or null before it happened. */
export function milestoneAt(m: TraceMilestone, cursor: number): string | null {
  const n = m.times.filter((t) => t <= cursor).length;
  if (!n) return null;
  return m.type === "gps.ping" ? `${n} GPS ping${n === 1 ? "" : "s"}` : m.label;
}

const CARD_W_TOOL_ROWS = 3;
export function taskCardHeight(tools: number) {
  const rows = Math.max(1, Math.min(tools, CARD_W_TOOL_ROWS) + (tools > CARD_W_TOOL_ROWS ? 1 : 0));
  return 58 + rows * 15;
}
export const SMALL_CARD_H = 68;

export function buildCaseTrace(snapshot: Snapshot, caseId: string): CaseTrace {
  const slice = sliceCase(snapshot, caseId);
  const caseRow = slice.case[0];
  const tasks = slice.task.map(taskView).sort((a, b) => a.startedAt - b.startedAt || a.taskId.localeCompare(b.taskId));
  const taskIds = new Set(tasks.map((t) => t.taskId));
  const calls = buildToolCalls(slice);
  const callsByTask = new Map<string, ToolCallView[]>();
  for (const c of calls) {
    const list = callsByTask.get(c.taskId) ?? [];
    list.push(c);
    callsByTask.set(c.taskId, list);
  }
  const docsByTask = new Map<string, DocRead[]>();
  for (const r of slice.doc_read) {
    const d: DocRead = {
      id: str(r.id, ""),
      taskId: str(r.task_id, ""),
      agent: str(r.agent, ""),
      docRef: str(r.doc_ref, ""),
      page: num(r.page),
      span: str(r.span, ""),
      extracted: json<unknown>(r.extracted_json, r.extracted_json ?? null),
      at: timestamp(r.ts),
    };
    const list = docsByTask.get(d.taskId) ?? [];
    list.push(d);
    docsByTask.set(d.taskId, list);
  }
  const approvalsByTask = new Map<string, Row[]>();
  for (const a of slice.approval) {
    const list = approvalsByTask.get(str(a.task_id, "")) ?? [];
    list.push(a);
    approvalsByTask.set(str(a.task_id, ""), list);
  }

  const nodes: TraceNode[] = [];
  const edges: TraceEdge[] = [];

  // Intake dispatch: routing decisions are the root of the graph; lifecycle events only get ticks.
  const { decisions, milestones } = splitDispatches(slice.dispatch, timestamp(caseRow?.opened_at));
  const dispatchByTask = new Map<string, string>();
  for (const d of decisions) {
    const id = `intake:${str(d.event_id)}`;
    const at = timestamp(d.ts) || timestamp(caseRow?.opened_at);
    nodes.push({ id, kind: "intake", lane: "intake", start: at, end: at, dispatch: d, tools: [], docs: [], waits: [], height: SMALL_CARD_H });
    const target = str(d.task_id, "");
    if (target) dispatchByTask.set(target, id);
  }

  for (const t of tasks) {
    const tools = callsByTask.get(t.taskId) ?? [];
    const waits = (approvalsByTask.get(t.taskId) ?? []).map(
      (a) => [approvalRequestedAt(a, slice.audit_event) || t.updatedAt, a.decision && a.decision !== "pending" ? timestamp(a.decided_at) : 0] as [number, number],
    );
    nodes.push({
      id: t.taskId,
      kind: "task",
      lane: t.agent || "unknown",
      start: t.startedAt,
      end: t.endedAt,
      task: t,
      tools,
      docs: (docsByTask.get(t.taskId) ?? []).sort((a, b) => a.at - b.at),
      waits,
      height: taskCardHeight(tools.length),
    });
  }

  // Human approvals.
  for (const a of slice.approval) {
    const requested = approvalRequestedAt(a, slice.audit_event);
    const decided = a.decision && a.decision !== "pending" ? timestamp(a.decided_at) : 0;
    const id = `human:${str(a.id)}`;
    nodes.push({ id, kind: "human", lane: "human", start: requested, end: decided, approval: a, tools: [], docs: [], waits: [], height: SMALL_CARD_H });
    const taskId = str(a.task_id, "");
    if (taskIds.has(taskId)) {
      edges.push({ id: `approval:${str(a.id)}`, kind: "approval", from: taskId, to: id, at: requested, skill: str(a.action, ""), row: a });
      if (decided) edges.push({ id: `decision:${str(a.id)}`, kind: "decision", from: id, to: taskId, at: decided, skill: str(a.decision, ""), row: a });
    }
  }

  // Edges: dispatch, delegations (from the edge table, else parent_task_id), reviews, retries.
  const incoming = new Set<string>();
  for (const [taskId, intakeId] of dispatchByTask)
    if (taskIds.has(taskId)) {
      const node = nodes.find((n) => n.id === intakeId)!;
      edges.push({ id: `dispatch:${intakeId}`, kind: "dispatch", from: intakeId, to: taskId, at: node.start, skill: str(node.dispatch?.skill, "") });
      incoming.add(taskId);
    }
  for (const e of slice.delegation_edge) {
    const child = str(e.task_id, "");
    const parent = str(e.parent_task_id, "");
    if (!taskIds.has(child) || !taskIds.has(parent) || incoming.has(child)) continue;
    edges.push({ id: `edge:${str(e.id)}`, kind: "delegation", from: parent, to: child, at: timestamp(e.ts), skill: str(e.skill, ""), row: e });
    incoming.add(child);
  }
  const taskById = new Map(tasks.map((t) => [t.taskId, t]));
  // Retries: a later task of the same agent, skill and parent that follows a failure.
  for (const t of tasks) {
    if (incoming.has(t.taskId)) continue;
    const failed = tasks.find(
      (f) => f.state === "failed" && f.agent === t.agent && f.skill === t.skill && f.parentTaskId === t.parentTaskId && f.taskId !== t.taskId && f.updatedAt <= t.startedAt,
    );
    if (failed) {
      edges.push({ id: `retry:${t.taskId}`, kind: "retry", from: failed.taskId, to: t.taskId, at: failed.updatedAt, skill: t.skill });
      incoming.add(t.taskId);
    }
  }
  for (const t of tasks) {
    if (incoming.has(t.taskId) || !t.parentTaskId || !taskById.has(t.parentTaskId)) continue;
    edges.push({ id: `parent:${t.taskId}`, kind: "delegation", from: t.parentTaskId, to: t.taskId, at: t.startedAt, skill: t.skill });
    incoming.add(t.taskId);
  }
  // Post-resolution quality review: dashed edge from the reviewed (financial) task to the validator.
  const roots = tasks.filter((t) => !t.parentTaskId && t.agent !== "validator");
  for (const t of tasks) {
    if (incoming.has(t.taskId) || t.agent !== "validator") continue;
    const review = slice.review.find((r) => str(r.id) === `REV-${t.taskId}`) ?? slice.review.find((r) => taskIds.has(str(r.target_task_id, "")));
    const target = review && taskIds.has(str(review.target_task_id, "")) ? str(review.target_task_id) : (roots[0]?.taskId ?? "");
    if (!target || target === t.taskId) continue;
    const src = taskById.get(target)!;
    edges.push({ id: `review:${t.taskId}`, kind: "review", from: target, to: t.taskId, at: Math.min(src.endedAt || src.startedAt, t.startedAt), skill: t.skill, row: review });
    incoming.add(t.taskId);
  }

  const timeline = buildTimeline(slice, caseId);
  const auditByTask = new Map<string, Row[]>();
  for (const a of [...slice.audit_event].sort((x, y) => timestamp(x.ts) - timestamp(y.ts) || num(x.sequence) - num(y.sequence))) {
    const key = str(a.task_id, "");
    const list = auditByTask.get(key) ?? [];
    list.push(a);
    auditByTask.set(key, list);
  }

  const sideEffects = buildSideEffects(slice);
  const times = [
    timestamp(caseRow?.opened_at),
    ...nodes.flatMap((n) => [n.start, n.end]),
    ...milestones.flatMap((m) => m.times),
    ...timeline.map((e) => e.at),
    ...sideEffects.flatMap((s) => [s.at, s.changedAt]),
  ].filter((t) => t > 0);
  const open = tasks.some((t) => !t.terminal) || slice.approval.some((a) => !a.decision || a.decision === "pending");
  return {
    caseId,
    slice,
    nodes,
    nodeById: new Map(nodes.map((n) => [n.id, n])),
    edges,
    milestones,
    timeline,
    sideEffects,
    auditByTask,
    toolCount: calls.length,
    first: times.length ? Math.min(...times) : 0,
    last: times.length ? Math.max(...times) : 0,
    open,
  };
}

function buildSideEffects(slice: Snapshot): SideEffect[] {
  const out: SideEffect[] = [];
  for (const h of slice.hold) {
    const released = timestamp(h.released_at);
    out.push({
      id: `hold:${str(h.id)}`,
      kind: "hold",
      at: timestamp(h.placed_at),
      changedAt: released,
      label: `Hold · ${str(h.invoice_id, "invoice")}`,
      amount: num(h.amount),
      status: str(h.status, "active"),
      initialStatus: "active",
      taskId: str(h.id).startsWith("HOLD-") ? str(h.id).slice(5) : "",
      row: h,
    });
  }
  for (const p of slice.payment_proposal)
    out.push({
      id: `payment:${str(p.id)}`,
      kind: "payment",
      at: timestamp(p.proposed_at),
      changedAt: 0,
      label: `Payment · ${str(p.invoice_id, "invoice")}`,
      amount: num(p.amount),
      status: str(p.status, "proposed"),
      initialStatus: str(p.status, "proposed"),
      taskId: str(p.id).startsWith("PP-") ? str(p.id).slice(3) : "",
      row: p,
    });
  for (const a of slice.approval) {
    const decided = a.decision && a.decision !== "pending";
    out.push({
      id: `approval:${str(a.id)}`,
      kind: "approval",
      at: approvalRequestedAt(a, slice.audit_event),
      changedAt: decided ? timestamp(a.decided_at) : 0,
      label: `Approval · ${str(a.action, "action").replaceAll("_", " ")}`,
      amount: num(a.amount),
      status: decided ? str(a.decision) : "pending",
      initialStatus: "pending",
      taskId: str(a.task_id, ""),
      row: a,
    });
  }
  for (const j of slice.ledger_entry) {
    const lines = (Array.isArray(j.lines) ? j.lines : json<Row[]>(j.lines, [])) as Row[];
    out.push({
      id: `journal:${str(j.id)}`,
      kind: "journal",
      at: timestamp(j.posted_at),
      changedAt: 0,
      label: str(j.memo, "Journal entry"),
      amount: lines.reduce((s, l) => s + num(l.debit), 0),
      status: "posted",
      initialStatus: "posted",
      taskId: "",
      row: j,
    });
  }
  for (const f of slice.cash_forecast)
    out.push({
      id: `forecast:${str(f.id)}`,
      kind: "forecast",
      at: timestamp(f.updated_at),
      changedAt: 0,
      label: `Forecast · ${str(f.period, "")}`,
      amount: num(f.amount),
      status: str(f.category, "forecast").replaceAll("_", " "),
      initialStatus: str(f.category, "forecast").replaceAll("_", " "),
      taskId: str(f.id).startsWith("CF-") ? str(f.id).slice(3) : "",
      row: f,
    });
  return out.filter((s) => s.at > 0).sort((a, b) => a.at - b.at);
}

/* --------------------------- state at a time --------------------------- */

/**
 * Status of a node at `cursor` (Infinity = live). Only timestamps are used, so
 * replay shows what was true at that moment, not the final state.
 */
export function nodeStatus(node: TraceNode, cursor: number): NodeStatus | null {
  if (node.start > cursor) return null;
  if (node.kind === "intake") return str(node.dispatch?.status, "accepted") === "rejected" ? "failed" : "completed";
  if (node.kind === "human") {
    if (node.end && node.end <= cursor) return str(node.approval?.decision) === "approved" ? "approved" : "rejected";
    return "pending";
  }
  const t = node.task!;
  if (node.end && node.end <= cursor) return t.state === "failed" ? "failed" : t.state === "canceled" || t.state === "rejected" ? "canceled" : "completed";
  if (node.waits.some(([from, to]) => from <= cursor && (!to || to > cursor))) return "waiting";
  if (cursor === Infinity) {
    if (t.state === "input-required" || t.state === "auth-required") return "waiting";
    if (t.state === "submitted") return "queued";
  }
  return "working";
}

export type ToolState = { call: ToolCallView; status: "running" | "completed" | "failed" | "blocked" };
export function toolsAt(node: TraceNode, cursor: number): ToolState[] {
  const out: ToolState[] = [];
  for (const call of node.tools) {
    if (call.startedAt > cursor) continue;
    const done = call.endedAt && call.endedAt <= cursor;
    out.push({ call, status: done ? (call.status === "failed" ? "failed" : call.status === "blocked" ? "blocked" : "completed") : "running" });
  }
  return out;
}

export function sideEffectAt(s: SideEffect, cursor: number): string | null {
  if (s.at > cursor) return null;
  if (s.changedAt && s.changedAt > cursor) return s.initialStatus;
  return s.status;
}

/* ------------------------------ layout ------------------------------ */

export const LAYOUT = {
  gutter: 70,
  top: 22,
  laneW: { intake: 188, human: 204, agent: 218 } as Record<string, number>,
  lineInset: 12,
  cardInset: 26,
  branch: 26,
  laneGap: 16,
};

/** Vertical room one milestone tick takes on the Intake lane, and the most time before it may add. */
export const MILESTONE_STEP = 18;

/** Compressed time → pixels: linear for 10 s, slower to 30 s, then a fixed spacer. */
const GAP_MS = 30_000;
function stretch(dt: number) {
  if (dt <= 0) return 0;
  if (dt <= 10_000) return (dt / 1000) * 7;
  return 70 + ((Math.min(dt, GAP_MS) - 10_000) / 1000) * 1.6;
}

export type Lane = { id: string; x: number; width: number; lineX: number; cardX: number; cardW: number; first: number };
export type Gap = { y0: number; y1: number; dt: number };
export type TraceLayout = {
  lanes: Lane[];
  laneById: Map<string, Lane>;
  /** y of each node's start dot. */
  nodeY: Map<string, number>;
  /** y of keyed anchors: `end:<node>`, `src:<edge>`, `<milestone id>`. */
  anchorY: Map<string, number>;
  yOf: (t: number) => number;
  gaps: Gap[];
  width: number;
  height: number;
};

/**
 * Git-graph layout rotated: lanes are agents (first appearance order), time
 * flows down. Every timestamp becomes an anchor; anchors are swept in order so
 * a node never overlaps the previous node of its lane and a branch always
 * leaves room to curve. Positions only depend on earlier anchors, so a live
 * case grows downward without reshuffling what is already on screen.
 * Milestones have no card: the time before one adds at most MILESTONE_STEP,
 * so a run of lifecycle events never pushes the first real card down.
 */
export function layoutTrace(trace: CaseTrace, tail: number): TraceLayout {
  const { nodes, edges, milestones } = trace;
  // Lanes in order of first appearance.
  const firstAt = new Map<string, number>();
  const starts = [...nodes.map((n) => ({ lane: n.lane, t: n.start })), ...milestones.map((m) => ({ lane: "intake", t: m.at }))];
  for (const s of starts.sort((a, b) => a.t - b.t)) if (!firstAt.has(s.lane)) firstAt.set(s.lane, s.t);
  const lanes: Lane[] = [];
  let x = LAYOUT.gutter;
  for (const [id, first] of firstAt) {
    const width = LAYOUT.laneW[id] ?? LAYOUT.laneW.agent;
    lanes.push({ id, x, width, lineX: x + LAYOUT.lineInset, cardX: x + LAYOUT.cardInset, cardW: width - LAYOUT.cardInset - LAYOUT.laneGap, first });
    x += width;
  }
  const laneById = new Map(lanes.map((l) => [l.id, l]));

  type Anchor = { t: number; key: string; order: number; node?: TraceNode; source?: string; milestone?: boolean };
  const anchors: Anchor[] = [];
  const sourceOf = new Map<string, string>();
  for (const e of edges) {
    const key = e.kind === "dispatch" ? `start:${e.from}` : e.kind === "review" || e.kind === "retry" ? `end:${e.from}` : `src:${e.id}`;
    if (key.startsWith("src:")) anchors.push({ t: e.at, key, order: 0 });
    if (!sourceOf.has(e.to) && e.kind !== "decision") sourceOf.set(e.to, key);
  }
  for (const n of nodes) {
    anchors.push({ t: n.start, key: `start:${n.id}`, order: 1, node: n, source: sourceOf.get(n.id) });
    if (n.end && n.kind !== "intake") anchors.push({ t: n.end, key: `end:${n.id}`, order: 0 });
    for (const c of n.tools) {
      anchors.push({ t: c.startedAt, key: `tc:${c.id}`, order: 0 });
      if (c.endedAt) anchors.push({ t: c.endedAt, key: `tce:${c.id}`, order: 0 });
    }
    for (const d of n.docs) anchors.push({ t: d.at, key: `doc:${d.id}`, order: 0 });
  }
  for (const m of milestones) anchors.push({ t: m.at, key: m.id, order: 1, milestone: true });
  if (tail) anchors.push({ t: tail, key: "tail", order: 2 });
  const valid = anchors.filter((a) => a.t > 0).sort((a, b) => a.t - b.t || a.order - b.order);

  const anchorY = new Map<string, number>();
  const nodeY = new Map<string, number>();
  const points: { t: number; y: number }[] = [];
  const gaps: Gap[] = [];
  const laneFree = new Map<string, number>();
  let y = LAYOUT.top;
  let prev = valid[0]?.t ?? 0;
  for (const a of valid) {
    const dt = a.t - prev;
    const y0 = y;
    y += a.milestone ? Math.min(stretch(dt), MILESTONE_STEP) : stretch(dt);
    if (dt > GAP_MS && !a.milestone) gaps.push({ y0, y1: y, dt });
    prev = a.t;
    if (a.milestone) {
      // A tick sits below whatever the Intake lane already shows, and keeps its own row free of the next card.
      y = Math.max(y, laneFree.get("intake") ?? 0);
      laneFree.set("intake", y + MILESTONE_STEP);
    } else if (a.node) {
      let min = laneFree.get(a.node.lane) ?? 0;
      const src = a.source ? anchorY.get(a.source) : undefined;
      if (src !== undefined) min = Math.max(min, src + LAYOUT.branch);
      if (y < min) y = min;
      laneFree.set(a.node.lane, y + a.node.height + 18);
      nodeY.set(a.node.id, y);
    }
    anchorY.set(a.key, y);
    points.push({ t: a.t, y });
  }

  const yOf = (t: number) => {
    if (!points.length) return LAYOUT.top;
    if (t <= points[0].t) return points[0].y;
    let lo = 0;
    let hi = points.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (points[mid].t <= t) lo = mid;
      else hi = mid - 1;
    }
    // Last point at or before t (the lowest one when several share a timestamp).
    const p = points[lo];
    const next = points[lo + 1];
    const dy = stretch(t - p.t);
    return next ? Math.min(p.y + dy, next.y) : p.y + dy;
  };

  let bottom = y;
  for (const n of nodes) bottom = Math.max(bottom, (nodeY.get(n.id) ?? 0) + n.height);
  return { lanes, laneById, nodeY, anchorY, yOf, gaps, width: x + 28, height: bottom + 56 };
}

/* ------------------------------ explorer ------------------------------ */

export function caseDuration(c: CaseView, now: number) {
  return (c.resolvedAt && c.status === "resolved" ? c.resolvedAt : now) - c.openedAt;
}
