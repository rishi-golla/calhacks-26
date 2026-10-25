/**
 * Derived, typed view-model over a `Snapshot`.
 *
 * Every page (finance home, agent graph, case explorer) reads through these
 * selectors so live SpacetimeDB data and the client simulation render the same
 * way. Selectors are pure; wrap them in `useMemo` keyed on the snapshot tables
 * they touch.
 */
import { AGENT_IDS, isAgentId, type AgentId } from "./agents";
import { json, num, str, timestamp, type Row, type Snapshot } from "./data";

/* ------------------------------ tasks ------------------------------ */

export type A2AState =
  | "submitted"
  | "working"
  | "input-required"
  | "auth-required"
  | "completed"
  | "failed"
  | "canceled"
  | "rejected"
  | "unknown";

export const TERMINAL_STATES = new Set<A2AState>(["completed", "failed", "canceled", "rejected"]);

export type TaskView = {
  taskId: string;
  caseId: string;
  parentTaskId: string;
  agent: AgentId | string;
  skill: string;
  displayId: string;
  state: A2AState;
  businessStatus: string;
  outcome: string;
  confidence: number;
  model: string;
  startedAt: number;
  updatedAt: number;
  /** Finished time when terminal, else 0. */
  endedAt: number;
  depth: number;
  evidenceRefs: string[];
  policyIds: string[];
  artifact: Record<string, unknown> | null;
  terminal: boolean;
  row: Row;
};

const asStringArray = (value: unknown): string[] =>
  Array.isArray(value) ? value.map((v) => str(v, "")).filter(Boolean) : typeof value === "string" && value ? json<string[]>(value, []) : [];

/**
 * Rows are immutable objects (the sim replaces a row object on every change and
 * the live provider rebuilds rows on refresh), so a per-object cache makes
 * repeated selector passes over tens of thousands of tasks nearly free.
 */
const taskViewCache = new WeakMap<Row, TaskView>();
export function taskView(row: Row): TaskView {
  const cached = taskViewCache.get(row);
  if (cached) return cached;
  const view = buildTaskView(row);
  taskViewCache.set(row, view);
  return view;
}
function buildTaskView(row: Row): TaskView {
  const state = (str(row.a2a_state, "unknown") as A2AState) ?? "unknown";
  const terminal = TERMINAL_STATES.has(state);
  const updatedAt = timestamp(row.updated_at);
  return {
    taskId: str(row.task_id, ""),
    caseId: str(row.case_id, ""),
    parentTaskId: str(row.parent_task_id, ""),
    agent: str(row.agent, ""),
    skill: str(row.skill, ""),
    displayId: str(row.display_id, str(row.task_id, "")),
    state,
    businessStatus: str(row.business_status, ""),
    outcome: str(row.outcome, ""),
    confidence: num(row.confidence),
    model: str(row.model_used, ""),
    startedAt: timestamp(row.started_at),
    updatedAt,
    endedAt: terminal ? updatedAt : 0,
    depth: num(row.depth),
    evidenceRefs: asStringArray(row.evidence_refs),
    policyIds: asStringArray(row.policy_ids),
    artifact: json<Record<string, unknown> | null>(row.artifact_json, null),
    terminal,
    row,
  };
}

/* ------------------------------ cases ------------------------------ */

export type CaseStatus = "open" | "in_progress" | "resolved" | "human_owned" | string;

export type CaseView = {
  caseId: string;
  loadId: string;
  title: string;
  status: CaseStatus;
  priority: string;
  exposureUsd: number;
  openedAt: number;
  resolvedAt: number;
  /** Agents that have run a task on the case, in first-seen order. */
  agents: AgentId[];
  tasks: TaskView[];
  activeTasks: number;
  /** Something needs a person: pending approval, input-required, failed task, open flag. */
  attention: AttentionKind[];
  synthetic: boolean;
  scenario: string;
  row: Row;
};

export type AttentionKind =
  | "approval_pending"
  | "task_failed"
  | "input_required"
  | "flag_open"
  | "review_issue"
  | "hold_active"
  | "call_no_answer";

export const isSyntheticCase = (row: Row) => /^\[(Live )?synthetic/i.test(str(row.title, ""));

export function buildCases(snapshot: Snapshot): Map<string, CaseView> {
  const tasksByCase = new Map<string, TaskView[]>();
  for (const row of snapshot.task) {
    const t = taskView(row);
    const list = tasksByCase.get(t.caseId);
    if (list) list.push(t);
    else tasksByCase.set(t.caseId, [t]);
  }
  const pendingApprovals = new Set(
    snapshot.approval.filter((a) => !a.decision || a.decision === "pending").map((a) => str(a.case_id, "")),
  );
  const openFlags = new Set(snapshot.flag.filter((f) => ["open", "reviewing"].includes(str(f.status, ""))).map((f) => str(f.case_id, "")));
  const issueReviews = new Set(snapshot.review.filter((r) => r.verdict === "issue").map((r) => str(r.case_id, "")));
  const activeHolds = new Set(snapshot.hold.filter((h) => ["active", "held"].includes(str(h.status, ""))).map((h) => str(h.case_id, "")));
  const noAnswer = new Set(snapshot.call.filter((c) => c.status === "no_answer").map((c) => str(c.case_id, "")));
  const cases = new Map<string, CaseView>();
  for (const row of snapshot.case) {
    const caseId = str(row.case_id, "");
    const tasks = (tasksByCase.get(caseId) ?? []).sort((a, b) => a.startedAt - b.startedAt);
    const agents: AgentId[] = [];
    for (const t of tasks) if (isAgentId(t.agent) && !agents.includes(t.agent)) agents.push(t.agent);
    const attention: AttentionKind[] = [];
    if (pendingApprovals.has(caseId)) attention.push("approval_pending");
    if (tasks.some((t) => t.state === "failed")) attention.push("task_failed");
    if (tasks.some((t) => t.state === "input-required" || t.state === "auth-required")) attention.push("input_required");
    if (openFlags.has(caseId)) attention.push("flag_open");
    if (issueReviews.has(caseId)) attention.push("review_issue");
    if (activeHolds.has(caseId)) attention.push("hold_active");
    if (noAnswer.has(caseId)) attention.push("call_no_answer");
    cases.set(caseId, {
      caseId,
      loadId: str(row.load_id, ""),
      title: str(row.title, caseId),
      status: str(row.status, "open"),
      priority: str(row.priority, "P3"),
      exposureUsd: num(row.exposure_usd),
      openedAt: timestamp(row.opened_at),
      resolvedAt: timestamp(row.resolved_at),
      agents,
      tasks,
      activeTasks: tasks.filter((t) => !t.terminal).length,
      attention,
      synthetic: isSyntheticCase(row),
      scenario: str(row.scenario, ""),
      row,
    });
  }
  return cases;
}

/* --------------------------- case timeline --------------------------- */

export type ToolCallView = {
  id: string;
  taskId: string;
  agent: string;
  name: string;
  status: "started" | "waiting" | "completed" | "failed" | "blocked" | string;
  arguments: unknown;
  result: unknown;
  durationMs: number;
  startedAt: number;
  endedAt: number;
  model: string;
};

export type TimelineEvent = {
  id: string;
  at: number;
  caseId: string;
  taskId: string;
  agent: string;
  kind:
    | "dispatch"
    | "task_started"
    | "task_completed"
    | "task_failed"
    | "task_waiting"
    | "delegation"
    | "tool_call"
    | "doc_read"
    | "hold"
    | "payment"
    | "approval"
    | "journal"
    | "call"
    | "review"
    | "policy"
    | "audit";
  title: string;
  detail: string;
  policyIds: string[];
  evidenceRefs: string[];
  /** For delegation events. */
  from?: string;
  to?: string;
  row: Row;
};

const EVENT_KIND: Record<string, TimelineEvent["kind"]> = {
  record_delegation: "delegation",
  tool_call: "tool_call",
  record_doc_read: "doc_read",
  place_hold: "hold",
  release_hold: "hold",
  propose_payment: "payment",
  request_approval: "approval",
  decide_approval: "approval",
  post_journal_entry: "journal",
  request_journal_entry: "journal",
  upsert_call: "call",
  add_review: "review",
  policy_deny: "policy",
};

/** Chronological, deduplicated event list for one case (or all cases when caseId is empty). */
export function buildTimeline(snapshot: Snapshot, caseId = ""): TimelineEvent[] {
  const events: TimelineEvent[] = [];
  const match = (row: Row) => !caseId || row.case_id === caseId;
  for (const row of snapshot.dispatch)
    if (match(row))
      events.push({
        id: `dispatch:${str(row.event_id)}`,
        at: timestamp(row.ts),
        caseId: str(row.case_id, ""),
        taskId: str(row.task_id, ""),
        agent: "intake",
        kind: "dispatch",
        title: `Routed to ${str(row.agent)}`,
        detail: str(row.reason, ""),
        policyIds: [],
        evidenceRefs: [],
        from: "intake",
        to: str(row.agent, ""),
        row,
      });
  for (const row of snapshot.delegation_edge)
    if (match(row))
      events.push({
        id: `edge:${str(row.id)}`,
        at: timestamp(row.ts),
        caseId: str(row.case_id, ""),
        taskId: str(row.task_id, ""),
        agent: str(row.from_agent, ""),
        kind: "delegation",
        title: `${str(row.from_agent)} → ${str(row.to_agent)}`,
        detail: str(row.skill, ""),
        policyIds: [],
        evidenceRefs: [],
        from: str(row.from_agent, ""),
        to: str(row.to_agent, ""),
        row,
      });
  for (const row of snapshot.task)
    if (match(row)) {
      const t = taskView(row);
      events.push({
        id: `task-start:${t.taskId}`,
        at: t.startedAt,
        caseId: t.caseId,
        taskId: t.taskId,
        agent: t.agent,
        kind: "task_started",
        title: `${t.skill.replaceAll("_", " ")}`,
        detail: t.displayId,
        policyIds: [],
        evidenceRefs: [],
        row,
      });
      if (t.terminal)
        events.push({
          id: `task-end:${t.taskId}`,
          at: t.endedAt,
          caseId: t.caseId,
          taskId: t.taskId,
          agent: t.agent,
          kind: t.state === "failed" ? "task_failed" : "task_completed",
          title: t.outcome || t.state,
          detail: str(t.artifact?.summary, ""),
          policyIds: t.policyIds,
          evidenceRefs: t.evidenceRefs,
          row,
        });
      else if (t.state === "input-required" || t.state === "auth-required")
        events.push({
          id: `task-wait:${t.taskId}`,
          at: t.updatedAt,
          caseId: t.caseId,
          taskId: t.taskId,
          agent: t.agent,
          kind: "task_waiting",
          title: t.businessStatus.replaceAll("_", " ") || "waiting",
          detail: str(t.artifact?.summary, ""),
          policyIds: t.policyIds,
          evidenceRefs: t.evidenceRefs,
          row,
        });
    }
  for (const row of snapshot.doc_read)
    if (match(row))
      events.push({
        id: `doc:${str(row.id)}`,
        at: timestamp(row.ts) || 0,
        caseId: str(row.case_id, ""),
        taskId: str(row.task_id, ""),
        agent: str(row.agent, ""),
        kind: "doc_read",
        title: `Read ${str(row.doc_ref)}`,
        detail: str(row.span, ""),
        policyIds: [],
        evidenceRefs: [str(row.doc_ref, "")],
        row,
      });
  for (const row of snapshot.audit_event)
    if (match(row)) {
      const kindRaw = str(row.kind, "");
      if (kindRaw === "upsert_task" || kindRaw === "agent_heartbeat" || kindRaw === "record_doc_read") continue;
      const kind = EVENT_KIND[kindRaw] ?? "audit";
      let title = str(row.summary, kindRaw.replaceAll("_", " "));
      let detail = str(row.reason_code, "");
      if (kind === "tool_call") {
        const call = json<Record<string, unknown>>(row.summary, {});
        title = `${str(call.tool_name, "tool")} · ${str(call.status, "")}`;
        detail = call.duration_ms ? `${num(call.duration_ms)} ms` : "";
      }
      events.push({
        id: `audit:${str(row.id)}`,
        at: timestamp(row.ts),
        caseId: str(row.case_id, ""),
        taskId: str(row.task_id, ""),
        agent: str(row.agent, ""),
        kind,
        title,
        detail,
        policyIds: asStringArray(row.policy_ids),
        evidenceRefs: asStringArray(row.evidence_refs),
        row,
      });
    }
  return events.filter((e) => e.at > 0).sort((a, b) => a.at - b.at || a.id.localeCompare(b.id));
}

/** Group `tool_call` audit events into one view per call id. */
export function buildToolCalls(snapshot: Snapshot, taskId?: string): ToolCallView[] {
  const calls = new Map<string, ToolCallView>();
  for (const row of snapshot.audit_event) {
    if (row.kind !== "tool_call") continue;
    if (taskId && row.task_id !== taskId) continue;
    const body = json<Record<string, unknown>>(row.summary, {});
    const id = str(body.tool_call_id, str(row.id));
    const at = timestamp(row.ts);
    const status = str(body.status, str(row.reason_code, "").replace(/^tool_/, ""));
    const prev = calls.get(id);
    const view: ToolCallView = {
      id,
      taskId: str(row.task_id, ""),
      agent: str(row.agent, ""),
      name: str(body.tool_name, "tool"),
      status,
      arguments: body.arguments ?? prev?.arguments,
      result: body.result ?? prev?.result,
      durationMs: num(body.duration_ms) || prev?.durationMs || 0,
      startedAt: prev ? Math.min(prev.startedAt, at) : at,
      endedAt: ["completed", "failed", "blocked"].includes(status) ? at : (prev?.endedAt ?? 0),
      model: str(body.model, str(row.model_used, "")),
    };
    if (prev && at < prev.startedAt) view.status = prev.status;
    calls.set(id, view);
  }
  return [...calls.values()].sort((a, b) => a.startedAt - b.startedAt);
}

/* ---------------------------- agent workload ---------------------------- */

export type AgentWorkload = {
  id: AgentId;
  status: "working" | "idle" | "offline" | "stale" | "error";
  active: number;
  queued: number;
  /** `working` task rows nothing is advancing: orphaned by a crash or a lost projection. */
  stalled: number;
  done: number;
  errors: number;
  p50Ms: number;
  lastSeen: number;
  /** Case ids currently being worked by this agent. */
  activeCases: string[];
  activeTasks: TaskView[];
  model: string;
  enabled: boolean;
};

/** Heartbeats land every 15 s; past this the agent's row is stale. */
const HEARTBEAT_STALE_MS = 60_000;
/** A `working` row this quiet while the heartbeat says idle has been abandoned. */
export const TASK_STALL_MS = 120_000;
/** Past this a `working` row is abandoned whatever the heartbeat says. */
export const TASK_ABANDON_MS = 15 * 60_000;

/**
 * Status comes from the agent's own heartbeat, not from task rows: a row can
 * be left `working` forever when a worker dies, while the heartbeat reports
 * whether the service holds a live task lease right now.
 *
 * With `since` (a "Start fresh" epoch), done and error counts come from tasks
 * that finished at or after it, not from the heartbeat's all-time totals.
 */
export function buildWorkloads(snapshot: Snapshot, now: number, { since }: { since?: number } = {}): Record<AgentId, AgentWorkload> {
  const byAgent = new Map<string, TaskView[]>();
  const doneSince = new Map<string, number>();
  const failedSince = new Map<string, number>();
  for (const row of snapshot.task) {
    const t = taskView(row);
    if (since !== undefined && t.terminal && t.endedAt >= since) {
      if (t.state === "completed") doneSince.set(t.agent, (doneSince.get(t.agent) ?? 0) + 1);
      // The heartbeat counts rejected tasks as errors too.
      else if (t.state === "failed" || t.state === "rejected") failedSince.set(t.agent, (failedSince.get(t.agent) ?? 0) + 1);
    }
    if (t.terminal) continue;
    const list = byAgent.get(t.agent);
    if (list) list.push(t);
    else byAgent.set(t.agent, [t]);
  }
  const out = {} as Record<AgentId, AgentWorkload>;
  for (const id of AGENT_IDS) {
    const record = snapshot.agent.find((r) => r.name === id);
    const config = snapshot.agent_config.find((r) => r.agent === id);
    const workingTasks = (byAgent.get(id) ?? []).filter((t) => t.state === "working");
    const queuedTasks = (byAgent.get(id) ?? []).filter((t) => t.state !== "working");
    const lastSeen = record ? timestamp(record.last_seen) : 0;
    const stale = !record || now - lastSeen > HEARTBEAT_STALE_MS;
    const busy = !stale && record.status === "busy";
    const activeTasks = workingTasks.filter((t) => {
      const quiet = now - (t.updatedAt || t.startedAt);
      return quiet <= TASK_STALL_MS || (busy && quiet <= TASK_ABANDON_MS);
    });
    const status: AgentWorkload["status"] = !record
      ? "offline"
      : stale
        ? "stale"
        : record.status === "error"
          ? "error"
          : busy || activeTasks.length > 0
            ? "working"
            : "idle";
    out[id] = {
      id,
      status,
      // The heartbeat's own `active` column counts orphaned rows too, so count
      // live rows here; a busy heartbeat means at least one leased task.
      active: Math.max(activeTasks.length, busy ? 1 : 0),
      queued: queuedTasks.length || (record ? num(record.queued) : 0),
      stalled: workingTasks.length - activeTasks.length,
      done:
        since !== undefined
          ? (doneSince.get(id) ?? 0)
          : record
            ? num(record.done)
            : snapshot.task.filter((r) => r.agent === id && r.a2a_state === "completed").length,
      errors: since !== undefined ? (failedSince.get(id) ?? 0) : record ? num(record.errors) : 0,
      p50Ms: record ? num(record.p50_ms) : 0,
      lastSeen,
      activeCases: [...new Set(activeTasks.map((t) => t.caseId))],
      activeTasks,
      model: str(config?.model, "auto"),
      enabled: config ? config.enabled !== false : true,
    };
  }
  return out;
}

/** Intake animates for this long after a case arrives. */
export const INTAKE_ARRIVAL_MS = 30_000;

/** Intake is working while cases arrive; a backlog of open cases is not activity. */
export function intakeArriving(cases: Iterable<CaseView>, now: number): boolean {
  for (const c of cases) if (c.openedAt > 0 && now - c.openedAt <= INTAKE_ARRIVAL_MS) return true;
  return false;
}

/* --------------------------- operations metrics --------------------------- */

export type OpsMetrics = {
  casesTotal: number;
  casesActive: number;
  casesResolved: number;
  casesResolvedToday: number;
  casesNeedingAttention: number;
  tasksActive: number;
  tasksCompleted: number;
  tasksFailed: number;
  handoffs: number;
  toolCalls: number;
  callsPlaced: number;
  approvalsPending: number;
  holdsActive: number;
  automationRate: number;
  medianResolutionS: number;
  /** Cases resolved per hour over the last hour. */
  throughputPerHour: number;
  /** Rough estimate: 18 minutes of clerk time per resolved case. */
  minutesSaved: number;
  agentsOnline: number;
};

export function buildOpsMetrics(snapshot: Snapshot, cases: Map<string, CaseView>, now: number): OpsMetrics {
  const list = [...cases.values()];
  const resolved = list.filter((c) => c.status === "resolved");
  const dayStart = new Date(now).setHours(0, 0, 0, 0);
  const durations = resolved.filter((c) => c.resolvedAt && c.openedAt).map((c) => (c.resolvedAt - c.openedAt) / 1000).sort((a, b) => a - b);
  const lastHour = resolved.filter((c) => now - c.resolvedAt < 3_600_000).length;
  // Count task states straight off the rows: no parsing needed here.
  let tasksActive = 0;
  let tasksCompleted = 0;
  let tasksFailed = 0;
  for (const row of snapshot.task) {
    const state = row.a2a_state;
    if (state === "completed") tasksCompleted++;
    else if (state === "failed") tasksFailed++;
    else if (state !== "canceled" && state !== "rejected") tasksActive++;
  }
  let toolCalls = 0;
  for (const e of snapshot.audit_event) if (e.kind === "tool_call" && e.reason_code === "tool_completed") toolCalls++;
  const approvalsPending = snapshot.approval.filter((a) => !a.decision || a.decision === "pending").length;
  // Automation rate over a rolling window (last 20 minutes, or the whole
  // history while it is still short) so it reflects current behaviour rather than
  // freezing once hundreds of cases have accumulated. A case "needed a human"
  // when it raised an approval, a flag, or a task parked on human input.
  const windowStart = now - 20 * 60_000;
  const recent = list.filter((c) => c.openedAt >= windowStart);
  const sample = recent.length >= 20 ? recent : list;
  const sampleIds = new Set(sample.map((c) => c.caseId));
  const touched = new Set<string>();
  for (const a of snapshot.approval) if (sampleIds.has(str(a.case_id))) touched.add(str(a.case_id));
  for (const f of snapshot.flag) if (sampleIds.has(str(f.case_id))) touched.add(str(f.case_id));
  for (const c of sample) if (c.tasks.some((t) => t.state === "input-required" || t.state === "auth-required")) touched.add(c.caseId);
  const agentsOnline = snapshot.agent.filter((a) => now - timestamp(a.last_seen) <= 60_000).length;
  return {
    casesTotal: list.length,
    casesActive: list.filter((c) => c.status !== "resolved").length,
    casesResolved: resolved.length,
    casesResolvedToday: resolved.filter((c) => c.resolvedAt >= dayStart).length,
    casesNeedingAttention: list.filter((c) => c.attention.length > 0 && c.status !== "resolved").length,
    tasksActive,
    tasksCompleted,
    tasksFailed,
    handoffs: snapshot.delegation_edge.length,
    toolCalls,
    callsPlaced: snapshot.call.length,
    approvalsPending,
    holdsActive: snapshot.hold.filter((h) => ["active", "held"].includes(str(h.status))).length,
    automationRate: sample.length ? 1 - touched.size / sample.length : 0,
    medianResolutionS: durations.length ? durations[Math.floor(durations.length / 2)] : 0,
    throughputPerHour: lastHour,
    minutesSaved: resolved.length * 18,
    agentsOnline,
  };
}

/* ----------------------------- finance ----------------------------- */

export type LedgerLine = { account: string; debit: number; credit: number };
export type TrendPoint = { t: number; value: number };

export type FinanceMetrics = {
  cash: number;
  ap: number;
  ar: number;
  revenueMtd: number;
  revenue30d: number;
  expense30d: number;
  grossMarginPct: number;
  /** Carrier invoices still open (unpaid, not rejected). */
  outstandingInvoices: { count: number; amount: number };
  /** Invoices with an active hold. */
  heldInvoices: { count: number; amount: number };
  /** Open invoices issued more than 30 days ago. */
  overdueInvoices: { count: number; amount: number };
  paymentsProposed: { count: number; amount: number };
  forecast: { period: string; inflow: number; outflow: number; net: number; confidence: number }[];
  /** Daily series over the last 30 days. */
  trends: { cash: TrendPoint[]; revenue: TrendPoint[]; ap: TrendPoint[]; ar: TrendPoint[] };
};

const ACCOUNT_ALIASES: Record<string, string> = {
  cash: "cash",
  bank: "cash",
  accounts_receivable: "ar",
  ar: "ar",
  receivables: "ar",
  accounts_payable: "ap",
  ap: "ap",
  payables: "ap",
  freight_revenue: "revenue",
  revenue: "revenue",
  sales: "revenue",
  freight_expense: "expense",
  carrier_expense: "expense",
  expense: "expense",
  cost_of_revenue: "expense",
};

/**
 * `since` (a "Start fresh" epoch, with rows already scoped to it): cash comes
 * from the ledger rows given, not the all-time `cash_position` metric; the
 * 30-day totals run from `since` instead, and month-to-date from the later of
 * the month start and `since`, matching the "since" labels on Home.
 */
/** Journal lines arrive as objects over the live socket but as positional tuples from the SQL snapshot. */
function ledgerLines(raw: unknown): LedgerLine[] {
  const list = Array.isArray(raw) ? raw : json<unknown[]>(raw, []);
  return list.map((line) =>
    Array.isArray(line) ? ({ account: line[0], debit: line[1], credit: line[2] } as LedgerLine) : (line as LedgerLine),
  );
}

export function buildFinance(snapshot: Snapshot, now: number, { since }: { since?: number } = {}): FinanceMetrics {
  const day = 86_400_000;
  const entries = snapshot.ledger_entry
    .map((row) => ({ at: timestamp(row.posted_at), lines: ledgerLines(row.lines) }))
    // Future-dated postings are excluded so the headline agrees with the chart's last point.
    .filter((e) => e.at > 0 && e.at <= now)
    .sort((a, b) => a.at - b.at);
  // Running balances by canonical account; assets grow with debits, liabilities/revenue with credits.
  const delta = (line: LedgerLine) => {
    const acct = ACCOUNT_ALIASES[str(line.account).toLowerCase()];
    if (!acct) return null;
    const d = num(line.debit) - num(line.credit);
    return { acct, amount: acct === "cash" || acct === "ar" || acct === "expense" ? d : -d };
  };
  const bal = { cash: 0, ar: 0, ap: 0, revenue: 0, expense: 0 };
  const start = now - 30 * day;
  const totalsFrom = since ?? start;
  const buckets = new Map<number, typeof bal>();
  let revenue30d = 0;
  let expense30d = 0;
  let revenueMtd = 0;
  const monthStart = Math.max(since ?? 0, new Date(now).setDate(1));
  for (const e of entries) {
    for (const line of e.lines) {
      const d = delta(line);
      if (!d) continue;
      bal[d.acct as keyof typeof bal] += d.amount;
      if (d.acct === "revenue" && e.at >= totalsFrom) revenue30d += d.amount;
      if (d.acct === "expense" && e.at >= totalsFrom) expense30d += d.amount;
      if (d.acct === "revenue" && e.at >= monthStart) revenueMtd += d.amount;
    }
    if (e.at >= start) {
      const key = Math.floor(e.at / day) * day;
      buckets.set(key, { ...bal });
    }
  }
  const series = (acct: keyof typeof bal): TrendPoint[] => {
    const points: TrendPoint[] = [];
    let last = 0;
    // Seed with the balance before the window.
    for (const e of entries) {
      if (e.at >= start) break;
      for (const line of e.lines) {
        const d = delta(line);
        if (d?.acct === acct) last += d.amount;
      }
    }
    for (let i = 30; i >= 0; i--) {
      const key = Math.floor((now - i * day) / day) * day;
      const b = buckets.get(key);
      if (b) last = b[acct];
      points.push({ t: key, value: last });
    }
    return points;
  };
  // Revenue trend is daily revenue, not cumulative.
  const revenueDaily: TrendPoint[] = [];
  for (let i = 30; i >= 0; i--) {
    const key = Math.floor((now - i * day) / day) * day;
    let v = 0;
    for (const e of entries)
      if (e.at >= key && e.at < key + day)
        for (const line of e.lines) {
          const d = delta(line);
          if (d?.acct === "revenue") v += d.amount;
        }
    revenueDaily.push({ t: key, value: v });
  }
  const invoices = snapshot.invoice;
  const open = invoices.filter((i) => ["open", "held"].includes(str(i.status, "open")));
  const heldIds = new Set(snapshot.hold.filter((h) => ["active", "held"].includes(str(h.status))).map((h) => str(h.invoice_id)));
  const held = invoices.filter((i) => heldIds.has(str(i.id)) || i.status === "held");
  const overdue = open.filter((i) => now - timestamp(i.issued_at) > 30 * day);
  const sum = (rows: Row[], field = "amount") => rows.reduce((s, r) => s + num(r[field]) - (field === "amount" ? num(r.paid_amount) : 0), 0);
  const proposals = snapshot.payment_proposal.filter((p) => ["proposed", "awaiting_approval"].includes(str(p.status)));
  // Forecast grouped by period.
  const byPeriod = new Map<string, { inflow: number; outflow: number; conf: number[] }>();
  for (const f of snapshot.cash_forecast) {
    const key = str(f.period, "");
    const slot = byPeriod.get(key) ?? { inflow: 0, outflow: 0, conf: [] };
    const amt = num(f.amount);
    if (amt >= 0) slot.inflow += amt;
    else slot.outflow += -amt;
    slot.conf.push(num(f.confidence));
    byPeriod.set(key, slot);
  }
  const forecast = [...byPeriod.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([period, v]) => ({ period, inflow: v.inflow, outflow: v.outflow, net: v.inflow - v.outflow, confidence: v.conf.length ? v.conf.reduce((s, c) => s + c, 0) / v.conf.length : 0 }));
  const metric = (name: string) => snapshot.metric.find((m) => m.name === name);
  const cashMetric = since !== undefined ? undefined : (metric("cash_position") ?? metric("cash_position_usd") ?? metric("cash_balance"));
  return {
    cash: cashMetric ? num(cashMetric.value) : bal.cash,
    ap: bal.ap,
    ar: bal.ar,
    revenueMtd,
    revenue30d,
    expense30d,
    grossMarginPct: revenue30d ? ((revenue30d - expense30d) / revenue30d) * 100 : 0,
    outstandingInvoices: { count: open.length, amount: sum(open) },
    heldInvoices: { count: held.length, amount: snapshot.hold.filter((h) => ["active", "held"].includes(str(h.status))).reduce((s, h) => s + Math.max(0, num(h.amount) - num(h.released_amount)), 0) },
    overdueInvoices: { count: overdue.length, amount: sum(overdue) },
    paymentsProposed: { count: proposals.length, amount: proposals.reduce((s, p) => s + num(p.amount), 0) },
    forecast,
    trends: { cash: series("cash"), revenue: revenueDaily, ap: series("ap"), ar: series("ar") },
  };
}

/* --------------------------- attention queue --------------------------- */

export type AttentionItem = {
  id: string;
  kind: AttentionKind | "policy_deny" | "stale_agent";
  severity: "critical" | "high" | "medium" | "low";
  at: number;
  caseId: string;
  taskId: string;
  agent: string;
  title: string;
  detail: string;
  amount: number;
  row: Row;
};

const APPROVAL_TITLE: Record<string, string> = {
  propose_payment: "Release carrier payment",
  release_hold: "Release hold",
  post_journal_entry: "Post journal entry",
  request_journal_entry: "Post journal entry",
  set_credit_limit: "Set credit limit",
};

export function buildAttention(snapshot: Snapshot, cases: Map<string, CaseView>, now: number): AttentionItem[] {
  const items: AttentionItem[] = [];
  for (const a of snapshot.approval)
    if (!a.decision || a.decision === "pending")
      items.push({
        id: `approval:${str(a.id)}`,
        kind: "approval_pending",
        severity: num(a.amount) > 5_000 ? "critical" : "high",
        at: timestamp(a.requested_at) || timestamp(a.expires_at) - 3_600_000 || now,
        caseId: str(a.case_id, ""),
        taskId: str(a.task_id, ""),
        agent: str(a.requested_by, "controller"),
        title: APPROVAL_TITLE[str(a.action, "")] ?? str(a.action, "Approval").replaceAll("_", " "),
        detail: cases.get(str(a.case_id))?.title ?? str(a.target_id, ""),
        amount: num(a.amount),
        row: a,
      });
  // Failures and holds on cases that have since resolved are history, not work.
  const openCase = (caseId: string) => {
    const c = cases.get(caseId);
    return !c || c.status !== "resolved";
  };
  for (const row of snapshot.task) {
    const t = taskView(row);
    if (t.state === "failed" && openCase(t.caseId))
      items.push({ id: `failed:${t.taskId}`, kind: "task_failed", severity: "high", at: t.updatedAt, caseId: t.caseId, taskId: t.taskId, agent: t.agent, title: `${t.skill.replaceAll("_", " ")} failed`, detail: str(t.artifact?.summary, cases.get(t.caseId)?.title ?? ""), amount: cases.get(t.caseId)?.exposureUsd ?? 0, row });
    else if ((t.state === "input-required" || t.state === "auth-required") && !snapshot.approval.some((a) => a.task_id === t.taskId && (!a.decision || a.decision === "pending")))
      items.push({ id: `input:${t.taskId}`, kind: "input_required", severity: "medium", at: t.updatedAt, caseId: t.caseId, taskId: t.taskId, agent: t.agent, title: `${t.skill.replaceAll("_", " ")} needs input`, detail: t.businessStatus.replaceAll("_", " "), amount: cases.get(t.caseId)?.exposureUsd ?? 0, row });
  }
  for (const f of snapshot.flag)
    if (["open", "reviewing"].includes(str(f.status)))
      items.push({ id: `flag:${str(f.id)}`, kind: "flag_open", severity: "medium", at: timestamp(f.ts), caseId: str(f.case_id, ""), taskId: "", agent: "human", title: `Flag: ${str(f.kind)}`, detail: str(f.note, ""), amount: 0, row: f });
  for (const r of snapshot.review)
    if (r.verdict === "issue" && now - timestamp(r.ts) < 6 * 3_600_000)
      items.push({ id: `review:${str(r.id)}`, kind: "review_issue", severity: "medium", at: timestamp(r.ts), caseId: str(r.case_id, ""), taskId: str(r.target_task_id, ""), agent: "validator", title: "Quality review found an issue", detail: cases.get(str(r.case_id))?.title ?? "", amount: cases.get(str(r.case_id))?.exposureUsd ?? 0, row: r });
  for (const e of snapshot.audit_event)
    if (e.kind === "policy_deny" && openCase(str(e.case_id, "")))
      items.push({ id: `policy:${str(e.id)}`, kind: "policy_deny", severity: "high", at: timestamp(e.ts), caseId: str(e.case_id, ""), taskId: str(e.task_id, ""), agent: str(e.agent, ""), title: `Policy ${str(e.reason_code)} blocked an action`, detail: str(e.summary, ""), amount: cases.get(str(e.case_id))?.exposureUsd ?? 0, row: e });
  for (const h of snapshot.hold)
    if (["active", "held"].includes(str(h.status)) && openCase(str(h.case_id, "")))
      items.push({ id: `hold:${str(h.id)}`, kind: "hold_active", severity: "low", at: timestamp(h.placed_at), caseId: str(h.case_id, ""), taskId: "", agent: str(h.placed_by, "controller"), title: `Hold on ${str(h.invoice_id)}`, detail: str(h.reason_code, "").replaceAll("_", " ").toLowerCase(), amount: Math.max(0, num(h.amount) - num(h.released_amount)), row: h });
  for (const c of snapshot.call)
    if (c.status === "no_answer" && openCase(str(c.case_id, "")))
      items.push({ id: `call:${str(c.id)}`, kind: "call_no_answer", severity: "low", at: timestamp(c.ended_at) || timestamp(c.started_at), caseId: str(c.case_id, ""), taskId: str(c.task_id, ""), agent: "voice_desk", title: "Call not answered", detail: cases.get(str(c.case_id))?.title ?? "", amount: 0, row: c });
  const order = { critical: 0, high: 1, medium: 2, low: 3 };
  return items.sort((a, b) => order[a.severity] - order[b.severity] || b.at - a.at);
}

/* ------------------------------ events ------------------------------ */

/** A delta between two snapshots: what just happened, for animation. */
export type LiveEvent =
  | { type: "case_opened"; at: number; caseId: string; agent: string }
  | { type: "task_started"; at: number; caseId: string; taskId: string; agent: string; skill: string }
  | { type: "task_finished"; at: number; caseId: string; taskId: string; agent: string; state: A2AState; outcome: string }
  | { type: "delegation"; at: number; caseId: string; taskId: string; from: string; to: string; skill: string }
  | { type: "dispatch"; at: number; caseId: string; taskId: string; to: string; skill: string }
  | { type: "tool_call"; at: number; caseId: string; taskId: string; agent: string; name: string; status: string }
  | { type: "doc_read"; at: number; caseId: string; taskId: string; agent: string; docRef: string }
  | { type: "hold"; at: number; caseId: string; agent: string; amount: number; status: string }
  | { type: "approval"; at: number; caseId: string; agent: string; amount: number; decision: string }
  | { type: "call"; at: number; caseId: string; taskId: string; status: string }
  | { type: "review"; at: number; caseId: string; verdict: string }
  | { type: "journal"; at: number; caseId: string; amount: number; memo: string };

export function diffSnapshots(prev: Snapshot | null, next: Snapshot): LiveEvent[] {
  const events: LiveEvent[] = [];
  const index = (rows: Row[], key: string) => new Map(rows.map((r) => [str(r[key]), r]));
  // Both sources append rows in order and the sim replaces an unchanged table
  // with the same array, so we can skip identical tables outright and only
  // index the overlapping prefix when a table merely grew.
  const changed = (table: keyof Snapshot) => !prev || prev[table] !== next[table];
  const appendedOnly = (table: keyof Snapshot) => {
    const p = prev?.[table] ?? [];
    const n = next[table];
    if (p.length > n.length) return false;
    for (let i = 0; i < p.length; i += Math.max(1, Math.floor(p.length / 16))) if (p[i] !== n[i]) return false;
    return p.length === 0 || p[p.length - 1] === n[p.length - 1];
  };
  /** Rows of `table` that are new or changed vs prev. */
  const delta = (table: keyof Snapshot, key: string): { row: Row; before?: Row }[] => {
    if (!changed(table)) return [];
    const p = prev?.[table] ?? [];
    const n = next[table];
    if (appendedOnly(table)) return n.slice(p.length).map((row) => ({ row }));
    const pIndex = index(p, key);
    const out: { row: Row; before?: Row }[] = [];
    for (const row of n) {
      const before = pIndex.get(str(row[key]));
      if (before !== row) out.push({ row, before });
    }
    return out;
  };
  for (const { row: c, before } of delta("case", "case_id"))
    if (!before) events.push({ type: "case_opened", at: timestamp(c.opened_at), caseId: str(c.case_id), agent: "intake" });
  for (const { row: d, before } of delta("dispatch", "event_id"))
    if (!before) events.push({ type: "dispatch", at: timestamp(d.ts), caseId: str(d.case_id), taskId: str(d.task_id), to: str(d.agent), skill: str(d.skill) });
  for (const { row, before } of delta("task", "task_id")) {
    const t = taskView(row);
    if (!before) {
      if (t.state === "working") events.push({ type: "task_started", at: t.startedAt, caseId: t.caseId, taskId: t.taskId, agent: t.agent, skill: t.skill });
    } else if (before.a2a_state !== row.a2a_state) {
      if (t.state === "working") events.push({ type: "task_started", at: t.startedAt, caseId: t.caseId, taskId: t.taskId, agent: t.agent, skill: t.skill });
      if (t.terminal || t.state === "input-required") events.push({ type: "task_finished", at: t.updatedAt, caseId: t.caseId, taskId: t.taskId, agent: t.agent, state: t.state, outcome: t.outcome });
    }
  }
  for (const { row: e, before } of delta("delegation_edge", "id"))
    if (!before) events.push({ type: "delegation", at: timestamp(e.ts), caseId: str(e.case_id), taskId: str(e.task_id), from: str(e.from_agent), to: str(e.to_agent), skill: str(e.skill) });
  const audits = delta("audit_event", "id");
  for (const { row: a, before } of audits) {
    if (before) continue;
    if (a.kind === "tool_call") {
      const body = json<Record<string, unknown>>(a.summary, {});
      events.push({ type: "tool_call", at: timestamp(a.ts), caseId: str(a.case_id), taskId: str(a.task_id), agent: str(a.agent), name: str(body.tool_name, "tool"), status: str(body.status, "") });
    } else if (a.kind === "place_hold" || a.kind === "release_hold") events.push({ type: "hold", at: timestamp(a.ts), caseId: str(a.case_id), agent: str(a.agent), amount: 0, status: a.kind === "place_hold" ? "placed" : "released" });
    else if (a.kind === "post_journal_entry") events.push({ type: "journal", at: timestamp(a.ts), caseId: str(a.case_id), amount: 0, memo: str(a.summary) });
  }
  // Live doc reads carry no timestamp, and a live row can reach the cache long
  // after it was written: the dashboard keeps doc reads whose task changed
  // recently, so a task update brings its old reads in. A live read is news
  // only alongside the `record_doc_read` audit row its write produced, one
  // read per audit row; simulated reads carry their own `ts`.
  const docAudits = new Map<string, number[]>();
  for (const { row: a, before } of audits)
    if (!before && a.kind === "record_doc_read") {
      const list = docAudits.get(str(a.task_id, "")) ?? [];
      list.push(timestamp(a.ts) || Date.now());
      docAudits.set(str(a.task_id, ""), list);
    }
  const newReads = delta("doc_read", "id").filter(({ before }) => !before);
  // Rows arrive oldest first: when a task's old reads and a fresh one land
  // together, match audit rows to the newest reads.
  const reads: LiveEvent[] = [];
  for (let i = newReads.length - 1; i >= 0; i--) {
    const d = newReads[i].row;
    let at = timestamp(d.ts);
    if (!at) {
      const list = docAudits.get(str(d.task_id, ""));
      if (!list?.length) continue;
      at = list.pop()!;
    }
    reads.push({ type: "doc_read", at, caseId: str(d.case_id), taskId: str(d.task_id), agent: str(d.agent), docRef: str(d.doc_ref) });
  }
  events.push(...reads.reverse());
  for (const { row: a, before } of delta("approval", "id")) {
    if (!before) events.push({ type: "approval", at: timestamp(a.requested_at) || Date.now(), caseId: str(a.case_id), agent: str(a.requested_by), amount: num(a.amount), decision: "pending" });
    else if (before.decision !== a.decision) events.push({ type: "approval", at: timestamp(a.decided_at) || Date.now(), caseId: str(a.case_id), agent: "human", amount: num(a.amount), decision: str(a.decision) });
  }
  for (const { row: c, before } of delta("call", "id"))
    if (!before || before.status !== c.status) events.push({ type: "call", at: timestamp(c.ended_at) || timestamp(c.started_at) || Date.now(), caseId: str(c.case_id), taskId: str(c.task_id), status: str(c.status) });
  for (const { row: r, before } of delta("review", "id"))
    if (!before) events.push({ type: "review", at: timestamp(r.ts), caseId: str(r.case_id), verdict: str(r.verdict) });
  return events;
}

/* ------------------------------ helpers ------------------------------ */

export const fmtMoney = (value: number, compact = false) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: compact ? 1 : 0,
    notation: compact ? "compact" : "standard",
  }).format(value);

export const fmtDuration = (seconds: number) => {
  if (!seconds || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3_600)}h ${Math.round((seconds % 3_600) / 60)}m`;
};

export const fmtAgo = (at: number, now: number) => {
  const s = Math.max(0, (now - at) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3_600) return `${Math.round(s / 60)}m ago`;
  if (s < 86_400) return `${Math.round(s / 3_600)}h ago`;
  return `${Math.round(s / 86_400)}d ago`;
};

export const pct = (value: number, digits = 0) => `${(value * 100).toFixed(digits)}%`;
