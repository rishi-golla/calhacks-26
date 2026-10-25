/**
 * "Start fresh": hide activity before a shared epoch, without deleting it.
 *
 * The `view_epoch` table holds one row (`id = "current"`) whose `since` is the
 * epoch; no row, or a module that has no such table, means no epoch. Every
 * derived view reads the snapshot scoped here, so pages filter nothing
 * themselves:
 *
 * - A case is in scope when it opened at or after `since`, or when it is
 *   carried over: opened earlier but active across the epoch (see
 *   `carriedOver`), so a case is not lost mid-run.
 * - A row that belongs to a known case follows its case. A carried-over case
 *   keeps its whole history, so its trace and its totals stay coherent.
 * - A row with no known case is in scope by its own timestamp.
 * - Ledger entries go by their own `posted_at` only, whatever their case:
 *   finance totals every posting since the epoch.
 *
 * Reference and state tables (agents, config, companies, metrics, memory)
 * are never scoped. The case page reads the unscoped snapshot, so a
 * direct link to an earlier case still opens.
 */
import { str, timestamp, type Row, type Snapshot, type TableName } from "./data";
import { taskView, type LiveEvent } from "./ops";

export type ViewEpoch = {
  /** Epoch in ms. */
  since: number;
  setBy: string;
  setAt: number;
};

/** The shared epoch, or null when none is set (or the table is absent). */
export function readViewEpoch(snapshot: Snapshot): ViewEpoch | null {
  const rows = snapshot.view_epoch ?? [];
  const row = rows.find((r) => str(r.id, "") === "current") ?? rows[0];
  if (!row) return null;
  const since = timestamp(row.since);
  return since > 0 ? { since, setBy: str(row.set_by, ""), setAt: timestamp(row.set_at) } : null;
}

/**
 * Work quieter than this before the epoch was not in flight at it: a task row
 * left `submitted` or `working` by a worker that never picked it up, or a hold
 * parked on an idle case, does not carry its case over.
 */
export const CARRY_GRACE_MS = 5 * 60_000;

/** Timestamp columns that date a row of each scoped table. */
const ROW_TIME: Partial<Record<TableName, string[]>> = {
  task: ["started_at", "updated_at"],
  dispatch: ["ts"],
  delegation_edge: ["ts"],
  review: ["ts"],
  approval: ["requested_at", "decided_at"],
  flag: ["ts"],
  hold: ["placed_at", "released_at"],
  payment_proposal: ["proposed_at"],
  invoice: ["issued_at"],
  ledger_entry: ["posted_at"],
  audit_event: ["ts"],
  call: ["started_at", "ended_at"],
  doc_read: ["ts"],
  cash_forecast: ["updated_at"],
};
export const SCOPED_TABLES = Object.keys(ROW_TIME) as TableName[];

const rowTime = (table: TableName, row: Row) => {
  let at = 0;
  for (const field of ROW_TIME[table] ?? []) at = Math.max(at, timestamp(row[field]));
  return at;
};

export type ViewScope = {
  since: number;
  /** Case ids in scope. */
  cases: Set<string>;
  /** Of those, the ones opened before `since`. */
  carried: Set<string>;
  /** Every case id in the snapshot, in scope or not. */
  known: Set<string>;
  /** task id → case id, for rows that only name their task (live `doc_read`). */
  taskCase: Map<string, string>;
};

const isPending = (a: Row) => !a.decision || a.decision === "pending";

/**
 * Case ids opened before `since` that were still active across it:
 * - a task of the case started or changed at or after `since`;
 * - a non-terminal task changed within `CARRY_GRACE_MS` before `since`;
 * - a `queued` dispatch (routed, first hop not delivered yet) from within
 *   `CARRY_GRACE_MS` before `since`, or later, whose task does not exist yet.
 *   `triage` and `not_routed` intake rows never get a task and carry nothing;
 * - a pending approval that had not expired by `since`;
 * - an open flag;
 * - an active hold placed within `CARRY_GRACE_MS` before `since`, or later.
 */
export function carriedOver(snapshot: Snapshot, since: number, candidates: Set<string>): Set<string> {
  const out = new Set<string>();
  const add = (caseId: unknown) => {
    const id = str(caseId, "");
    if (candidates.has(id)) out.add(id);
  };
  const recent = since - CARRY_GRACE_MS;
  const tasks = new Set<string>();
  for (const row of snapshot.task) {
    const t = taskView(row);
    tasks.add(t.taskId);
    const last = Math.max(t.startedAt, t.updatedAt);
    if (last >= since || (!t.terminal && last >= recent)) add(t.caseId);
  }
  for (const d of snapshot.dispatch)
    if (d.status === "queued" && timestamp(d.ts) >= recent && !tasks.has(str(d.task_id, ""))) add(d.case_id);
  for (const a of snapshot.approval)
    if (isPending(a)) {
      const expires = timestamp(a.expires_at);
      if (!expires || expires >= since) add(a.case_id);
    }
  for (const f of snapshot.flag) if (["open", "reviewing"].includes(str(f.status, ""))) add(f.case_id);
  for (const h of snapshot.hold) if (["active", "held"].includes(str(h.status, "")) && timestamp(h.placed_at) >= recent) add(h.case_id);
  return out;
}

export function viewScope(snapshot: Snapshot, since: number): ViewScope {
  const known = new Set<string>();
  const cases = new Set<string>();
  const earlier = new Set<string>();
  for (const row of snapshot.case) {
    const id = str(row.case_id, "");
    known.add(id);
    if (timestamp(row.opened_at) >= since) cases.add(id);
    else earlier.add(id);
  }
  const carried = carriedOver(snapshot, since, earlier);
  for (const id of carried) cases.add(id);
  const taskCase = new Map<string, string>();
  for (const row of snapshot.task) taskCase.set(str(row.task_id, ""), str(row.case_id, ""));
  return { since, cases, carried, known, taskCase };
}

/** The case a row belongs to, when the snapshot knows it; else "". */
function caseOf(row: Row, scope: ViewScope): string {
  const direct = str(row.case_id, "");
  if (direct && scope.known.has(direct)) return direct;
  const viaTask = scope.taskCase.get(str(row.task_id, ""));
  return viaTask && scope.known.has(viaTask) ? viaTask : "";
}

export function rowInScope(table: TableName, row: Row, scope: ViewScope): boolean {
  if (!(table in ROW_TIME)) return true;
  if (table === "ledger_entry") return timestamp(row.posted_at) >= scope.since;
  const caseId = caseOf(row, scope);
  if (caseId) return scope.cases.has(caseId);
  // A live doc read has neither a case nor a timestamp; it is dated by its task.
  if (table === "doc_read" && !rowTime(table, row)) return false;
  return rowTime(table, row) >= scope.since;
}

const sameRows = (a: Row[] | undefined, b: Row[]) => !!a && a.length === b.length && a.every((row, i) => row === b[i]);

/**
 * The snapshot with every scoped table filtered to `scope`. A table that
 * loses no rows keeps the input array, and one whose result matches
 * `previous` keeps that array, so per-table memos and snapshot diffs see
 * stable identities.
 */
export function scopeSnapshot(snapshot: Snapshot, scope: ViewScope, previous?: Snapshot | null): Snapshot {
  const next = { ...snapshot };
  for (const table of SCOPED_TABLES) {
    const rows = snapshot[table] ?? [];
    const kept = rows.filter((row) => rowInScope(table, row, scope));
    next[table] = kept.length === rows.length ? rows : sameRows(previous?.[table], kept) ? previous![table] : kept;
  }
  next.case = snapshot.case.filter((row) => scope.cases.has(str(row.case_id, "")));
  if (next.case.length === snapshot.case.length) next.case = snapshot.case;
  else if (sameRows(previous?.case, next.case)) next.case = previous!.case;
  return next;
}

/** Live events in scope: by their case when it is known, else by their time. */
export function scopeEvents(events: LiveEvent[], scope: ViewScope): LiveEvent[] {
  return events.filter((e) => (e.caseId && scope.known.has(e.caseId) ? scope.cases.has(e.caseId) : e.at >= scope.since));
}

/* --------------------------- viewer preference --------------------------- */

/** localStorage key: "Show earlier activity" for one epoch, this viewer only. */
export const SHOW_EARLIER_KEY = "freightline.show-earlier.v1";

/**
 * The stored value names the epoch it was chosen for, so a later Start fresh
 * applies to this viewer again.
 */
export const showEarlierValue = (epoch: ViewEpoch) => String(epoch.since);
export const showEarlierApplies = (stored: string | null | undefined, epoch: ViewEpoch | null) =>
  !!epoch && stored === showEarlierValue(epoch);

/** "05:58", or "Oct 24, 05:58" when the epoch is not today. */
export function fmtSince(since: number, now: number): string {
  const at = new Date(since);
  const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  return at.toDateString() === new Date(now).toDateString()
    ? time
    : `${at.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
}
