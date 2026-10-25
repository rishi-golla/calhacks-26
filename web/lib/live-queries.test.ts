import assert from "node:assert/strict";
import test from "node:test";
import { TABLES, emptySnapshot } from "./data";
import {
  DEFAULT_WINDOW_MINUTES,
  caseIdFromPath,
  caseQueries,
  isSafeCaseId,
  liveWindowMinutes,
  snapshotBuilder,
  sqlString,
  subscriptionPlan,
  tablePolicy,
  windowCutoff,
  windowStepMs,
} from "./live-queries";

const NOW = Date.parse("2026-10-25T02:37:41.123Z");
const CUTOFF = "2026-10-25T02:21:00";

test("window minutes: default, explicit, off, and junk", () => {
  assert.equal(liveWindowMinutes(undefined), DEFAULT_WINDOW_MINUTES);
  assert.equal(liveWindowMinutes(""), DEFAULT_WINDOW_MINUTES);
  assert.equal(liveWindowMinutes("45"), 45);
  assert.equal(liveWindowMinutes("0"), 0);
  assert.equal(liveWindowMinutes("-5"), DEFAULT_WINDOW_MINUTES);
  assert.equal(liveWindowMinutes("soon"), DEFAULT_WINDOW_MINUTES);
});

test("cutoff is rounded down to the slide step and stable within a step", () => {
  assert.equal(windowStepMs(15), 3 * 60_000);
  assert.equal(windowStepMs(1), 60_000);
  assert.equal(windowStepMs(240), 10 * 60_000);
  // 02:37:41 - 15 min = 02:22:41, rounded down to a 3-minute step.
  assert.equal(windowCutoff(NOW, 15), CUTOFF);
  assert.equal(windowCutoff(NOW + 60_000, 15), CUTOFF);
  assert.equal(windowCutoff(NOW + 3 * 60_000, 15), "2026-10-25T02:24:00");
  // Never later than the true window start, never more than one step earlier.
  const cutoff = Date.parse(`${windowCutoff(NOW, 15)}Z`);
  assert.ok(cutoff <= NOW - 15 * 60_000 && cutoff > NOW - 18 * 60_000);
  assert.equal(windowCutoff(NOW, 0), null);
});

test("cutoff compares correctly against the schema's ISO timestamp strings", () => {
  const cutoff = windowCutoff(NOW, 15)!;
  assert.ok("2026-10-25T02:21:00.000001Z" > cutoff);
  assert.ok("2026-10-25T02:20:59.999999Z" < cutoff);
  assert.ok("2026-10-25T02:21:00Z" > cutoff);
  assert.ok("2026-10-25T02:21:00+00:00" > cutoff);
  assert.ok("" < cutoff, "unset timestamps fall outside the window");
});

test("app plan: aggregate tables full, history windowed, legacy-only tables skipped", () => {
  const plan = subscriptionPlan("app", CUTOFF);
  const base = new Map(plan.base.map((q) => [q.table, q.sql]));
  const windowed = new Map(plan.windowed.map((q) => [q.table, q.sql]));
  for (const table of ["case", "task", "invoice", "ledger_entry", "hold", "approval", "flag", "agent", "agent_config", "dispatch", "delegation_edge", "accounting_period"] as const)
    assert.equal(base.get(table), `SELECT * FROM "${table}"`, table);
  assert.deepEqual([...windowed.keys()].sort(), ["audit_event", "doc_read"]);
  assert.equal(
    windowed.get("audit_event"),
    `SELECT * FROM "audit_event" WHERE "ts" > '${CUTOFF}' OR "kind" = 'policy_deny'`,
  );
  assert.equal(
    windowed.get("doc_read"),
    `SELECT "doc_read".* FROM "doc_read" JOIN "task" ON "doc_read"."task_id" = "task"."task_id" WHERE "task"."updated_at" > '${CUTOFF}'`,
  );
  for (const table of ["change_set", "cell_change", "routing_outbox", "llm_call", "memory_revision"] as const) {
    assert.ok(!plan.tables.includes(table), table);
    assert.equal(tablePolicy("app", table).kind, "none");
  }
  // Every table is either subscribed once or deliberately skipped.
  assert.equal(plan.base.length + plan.windowed.length, plan.tables.length);
  assert.equal(new Set(plan.tables).size, plan.tables.length);
});

test("ops plan keeps every table, windowing the legacy history tables", () => {
  const plan = subscriptionPlan("ops", CUTOFF);
  assert.deepEqual([...plan.tables].sort(), [...TABLES].sort());
  const windowed = new Map(plan.windowed.map((q) => [q.table, q.sql]));
  assert.deepEqual([...windowed.keys()].sort(), ["audit_event", "cell_change", "change_set", "doc_read", "llm_call", "routing_outbox"]);
  assert.equal(windowed.get("change_set"), `SELECT * FROM "change_set" WHERE "ts" > '${CUTOFF}'`);
  assert.equal(windowed.get("routing_outbox"), `SELECT * FROM "routing_outbox" WHERE "updated_at" > '${CUTOFF}'`);
  assert.match(windowed.get("cell_change")!, /^SELECT "cell_change"\.\* FROM "cell_change" JOIN "change_set" ON .* WHERE "change_set"\."ts" > '/);
});

test("no window: every subscribed table is read in full, as before", () => {
  for (const profile of ["app", "ops"] as const) {
    const plan = subscriptionPlan(profile, null);
    assert.equal(plan.windowed.length, 0);
    for (const q of plan.base) assert.equal(q.sql, `SELECT * FROM "${q.table}"`);
  }
  assert.deepEqual(caseQueries("app", "CASE-1", null), []);
});

test("SQL queries carry no ORDER BY or LIMIT (subscriptions reject both)", () => {
  const all = [
    ...subscriptionPlan("app", CUTOFF).base,
    ...subscriptionPlan("ops", CUTOFF).windowed,
    ...caseQueries("ops", "CASE-1", CUTOFF),
  ];
  for (const q of all) assert.doesNotMatch(q.sql, /\b(ORDER BY|LIMIT)\b/i, q.sql);
});

test("case queries restore one case's windowed history", () => {
  const app = new Map(caseQueries("app", "CASE-LD-1025-401BBC52", CUTOFF).map((q) => [q.table, q.sql]));
  assert.deepEqual([...app.keys()].sort(), ["audit_event", "doc_read"]);
  assert.equal(app.get("audit_event"), `SELECT * FROM "audit_event" WHERE "case_id" = 'CASE-LD-1025-401BBC52'`);
  assert.equal(
    app.get("doc_read"),
    `SELECT "doc_read".* FROM "doc_read" JOIN "task" ON "doc_read"."task_id" = "task"."task_id" WHERE "task"."case_id" = 'CASE-LD-1025-401BBC52'`,
  );
  const ops = new Map(caseQueries("ops", "C-1", CUTOFF).map((q) => [q.table, q.sql]));
  assert.ok(!ops.has("llm_call"), "llm_call rows carry no case");
  assert.equal(ops.get("change_set"), `SELECT * FROM "change_set" WHERE "case_id" = 'C-1'`);
  assert.match(ops.get("cell_change")!, /WHERE "change_set"\."case_id" = 'C-1'$/);
});

test("unsafe case ids never reach SQL; literals are quoted", () => {
  assert.ok(isSafeCaseId("CASE-LD-1025-401BBC52"));
  for (const bad of ["", "x' OR '1'='1", "a b", "a;b", "x".repeat(161)]) {
    assert.ok(!isSafeCaseId(bad), bad);
    assert.deepEqual(caseQueries("app", bad, CUTOFF), []);
  }
  assert.equal(sqlString("it's"), "'it''s'");
});

test("case id from the route", () => {
  assert.equal(caseIdFromPath("/cases/CASE-LD-1025-401BBC52"), "CASE-LD-1025-401BBC52");
  assert.equal(caseIdFromPath("/cases/CASE%3A1/"), "CASE:1");
  assert.equal(caseIdFromPath("/cases"), "");
  assert.equal(caseIdFromPath("/cases/a/b"), "");
  assert.equal(caseIdFromPath("/agents"), "");
  assert.equal(caseIdFromPath(null), "");
  assert.equal(caseIdFromPath("/cases/%E0%A4%A"), "");
});

test("snapshot rebuilds touch only dirty tables and keep unchanged rows", () => {
  const audit = [{ id: "A1", ts: "t1" }];
  const tasks: object[] = [{ taskId: "T1", a2AState: "working", confidence: BigInt(1) }];
  const build = snapshotBuilder((table) => (table === "task" ? tasks : table === "audit_event" ? audit : []));
  const first = build(emptySnapshot(), ["task", "audit_event"]);
  assert.deepEqual(first.task, [{ task_id: "T1", a2a_state: "working", confidence: 1 }]);
  tasks.push({ taskId: "T2", a2AState: "submitted" });
  const second = build(first, ["task"]);
  assert.equal(second.audit_event, first.audit_event, "clean table keeps its array");
  assert.notEqual(second.task, first.task);
  assert.equal(second.task[0], first.task[0], "unchanged SDK row keeps its normalized object");
  assert.equal(second.task.length, 2);
  tasks[0] = { taskId: "T1", a2AState: "completed" };
  const third = build(second, ["task"]);
  assert.notEqual(third.task[0], second.task[0], "a replaced SDK row is normalized again");
  assert.equal(third.task[0].a2a_state, "completed");
  assert.equal(third.task[1], second.task[1]);
});
