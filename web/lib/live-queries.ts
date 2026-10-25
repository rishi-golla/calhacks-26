/**
 * Which rows the dashboard keeps live.
 *
 * Subscribing to `SELECT *` on every public table made the browser hold every
 * audit row, change set and cell change ever written (about 135 MB of row data
 * at 2,200 cases in the load test). Here each table gets a policy instead:
 *
 * - `full`: small tables, or tables an aggregate needs in full (cases, tasks,
 *   invoices, ledger, holds, approvals, flags, agents, config...).
 * - `window`: append-only history, kept for the last N minutes only. The
 *   cutoff is an ISO-8601 literal; every timestamp column in this schema is a
 *   UTC `...Z` string, so string order is time order.
 * - `none`: tables no view in this profile reads.
 *
 * A case page adds per-case queries (`caseQueries`) so its trace still shows
 * the case's whole history, whatever its age.
 *
 * SpacetimeDB 2.x subscriptions accept single-table `WHERE` filters (with
 * `AND`/`OR`) and semijoins on indexed columns; not `ORDER BY` or `LIMIT`.
 */
import { TABLES, normalize, type Row, type Snapshot, type TableName } from "./data";

/** `app`: the FreightLine pages (`/`, `/agents`, `/cases`). `ops`: the legacy `/ops` dashboard. */
export type LiveProfile = "app" | "ops";

export type TablePolicy =
  | { kind: "full" }
  | { kind: "none" }
  | { kind: "window"; sql: (cutoff: string) => string };

export const DEFAULT_WINDOW_MINUTES = 15;

/** Quote a string literal for SpacetimeDB SQL. */
export const sqlString = (value: string) => `'${value.replaceAll("'", "''")}'`;

/** Case ids are generated (`CASE-LD-1025-401BBC52`); anything else is refused before it reaches SQL. */
export const isSafeCaseId = (value: string) => /^[A-Za-z0-9_.:-]{1,160}$/.test(value);

const all = (table: TableName) => `SELECT * FROM "${table}"`;
const since = (table: TableName, column: string, extra = "") => (cutoff: string) =>
  `SELECT * FROM "${table}" WHERE "${column}" > ${sqlString(cutoff)}${extra}`;

/** Windowed tables, shared by both profiles. */
const AUDIT: TablePolicy = {
  kind: "window",
  // Policy denials stay in full: the attention queue lists every one on an open case.
  sql: since("audit_event", "ts", ` OR "kind" = 'policy_deny'`),
};
/** `doc_read` has no timestamp or case column; follow its task instead. */
const DOC_READ: TablePolicy = {
  kind: "window",
  sql: (cutoff) =>
    `SELECT "doc_read".* FROM "doc_read" JOIN "task" ON "doc_read"."task_id" = "task"."task_id" WHERE "task"."updated_at" > ${sqlString(cutoff)}`,
};

/** Read only by the legacy /ops views (audit workspace, outbox, learning, harness). */
const OPS_ONLY = new Set<TableName>(["change_set", "cell_change", "routing_outbox", "llm_call", "memory_revision"]);

const OPS_WINDOWED: Partial<Record<TableName, TablePolicy>> = {
  change_set: { kind: "window", sql: since("change_set", "ts") },
  cell_change: {
    kind: "window",
    sql: (cutoff) =>
      `SELECT "cell_change".* FROM "cell_change" JOIN "change_set" ON "cell_change"."change_set_id" = "change_set"."id" WHERE "change_set"."ts" > ${sqlString(cutoff)}`,
  },
  // Undelivered work stays whatever its age: the human inbox looks it up.
  routing_outbox: { kind: "window", sql: since("routing_outbox", "updated_at", ` OR "status" <> 'done'`) },
  llm_call: { kind: "window", sql: since("llm_call", "ts") },
};

export function tablePolicy(profile: LiveProfile, table: TableName): TablePolicy {
  if (table === "audit_event") return AUDIT;
  if (table === "doc_read") return DOC_READ;
  if (profile === "app" && OPS_ONLY.has(table)) return { kind: "none" };
  return (profile === "ops" && OPS_WINDOWED[table]) || { kind: "full" };
}

/**
 * `NEXT_PUBLIC_LIVE_WINDOW_MINUTES`: minutes of append-only history to keep.
 * Unset or invalid means the default; `0` turns the window off (every
 * windowed table is subscribed in full, as before).
 */
export function liveWindowMinutes(raw: string | undefined = process.env.NEXT_PUBLIC_LIVE_WINDOW_MINUTES): number {
  if (raw === undefined || raw.trim() === "") return DEFAULT_WINDOW_MINUTES;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : DEFAULT_WINDOW_MINUTES;
}

/** How often the window slides forward: a fifth of it, between one and ten minutes. */
export const windowStepMs = (minutes: number) => Math.min(10 * 60_000, Math.max(60_000, (minutes * 60_000) / 5));

/**
 * The cutoff literal for a window ending at `now`, rounded down to the slide
 * step so every call within one step yields the same query (and so the same
 * subscription). Null when the window is off.
 *
 * It is `YYYY-MM-DDTHH:MM:SS` with no fraction or zone: a stored
 * `...T02:21:00.000001Z` must sort after a `...T02:21:00` cutoff, which a
 * `.000Z` suffix would break ('0' < 'Z').
 */
export function windowCutoff(now: number, minutes: number): string | null {
  if (!(minutes > 0)) return null;
  const step = windowStepMs(minutes);
  return new Date(Math.floor((now - minutes * 60_000) / step) * step).toISOString().slice(0, 19);
}

export type PlannedQuery = { table: TableName; sql: string };
export type SubscriptionPlan = {
  /** Queries that never change while connected. */
  base: PlannedQuery[];
  /** Queries that depend on the cutoff and are re-issued as the window slides. */
  windowed: PlannedQuery[];
  /** Every table that has rows in this plan. */
  tables: TableName[];
};

export function subscriptionPlan(profile: LiveProfile, cutoff: string | null): SubscriptionPlan {
  const plan: SubscriptionPlan = { base: [], windowed: [], tables: [] };
  for (const table of TABLES) {
    const policy = tablePolicy(profile, table);
    if (policy.kind === "none") continue;
    plan.tables.push(table);
    if (policy.kind === "window" && cutoff) plan.windowed.push({ table, sql: policy.sql(cutoff) });
    else plan.base.push({ table, sql: all(table) });
  }
  return plan;
}

/**
 * Extra queries for one case's page: the windowed tables, unbounded in time
 * but restricted to the case. Empty when the window is off (everything is
 * already subscribed) or the id is not a safe literal.
 */
export function caseQueries(profile: LiveProfile, caseId: string, cutoff: string | null): PlannedQuery[] {
  if (!cutoff || !isSafeCaseId(caseId)) return [];
  const id = sqlString(caseId);
  const out: PlannedQuery[] = [];
  for (const table of TABLES) {
    if (tablePolicy(profile, table).kind !== "window") continue;
    if (table === "doc_read")
      out.push({
        table,
        sql: `SELECT "doc_read".* FROM "doc_read" JOIN "task" ON "doc_read"."task_id" = "task"."task_id" WHERE "task"."case_id" = ${id}`,
      });
    else if (table === "cell_change")
      out.push({
        table,
        sql: `SELECT "cell_change".* FROM "cell_change" JOIN "change_set" ON "cell_change"."change_set_id" = "change_set"."id" WHERE "change_set"."case_id" = ${id}`,
      });
    // llm_call rows carry no case.
    else if (table !== "llm_call") out.push({ table, sql: `SELECT * FROM "${table}" WHERE "case_id" = ${id}` });
  }
  return out;
}

/** `/cases/<id>` → the case id, else "". */
export function caseIdFromPath(pathname: string | null | undefined): string {
  const match = /^\/cases\/([^/?#]+)\/?$/.exec(pathname ?? "");
  if (!match) return "";
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return "";
  }
}

export const isLiveProfile = (value: unknown): value is LiveProfile => value === "app" || value === "ops";

/**
 * Snapshot rebuilds for the live cache. Only tables marked dirty get a new
 * array, and each SDK row object is normalized once, so unchanged tables keep
 * their array identity and unchanged rows their object identity. The
 * selectors' per-row caches (`taskView`) and `diffSnapshots` rely on both;
 * rebuilding every table on every change was most of the dashboard's lag.
 */
export function snapshotBuilder(read: (table: TableName) => Iterable<unknown>) {
  const normalized = new WeakMap<object, Row>();
  const toRow = (row: unknown): Row => {
    if (!row || typeof row !== "object") return normalize(row) as Row;
    let out = normalized.get(row);
    if (!out) {
      out = normalize(row) as Row;
      normalized.set(row, out);
    }
    return out;
  };
  return (current: Snapshot, dirty: Iterable<TableName>): Snapshot => {
    const next = { ...current };
    for (const table of dirty) next[table] = Array.from(read(table), toRow);
    return next;
  };
}
