import assert from "node:assert/strict";
import test from "node:test";
import { emptySnapshot, type Row, type Snapshot } from "./data";
import {
  attentionAudience,
  buildAttention,
  buildCases,
  buildFinance,
  buildOpsMetrics,
  buildTimeline,
  buildToolCalls,
  buildWorkloads,
  invoiceContractDelta,
  diffSnapshots,
  fmtAgo,
  fmtDuration,
  fmtMoney,
  intakeArriving,
  needsPerson,
  taskView,
  waitNote,
  type AttentionItem,
} from "./ops";

const NOW = Date.parse("2026-10-24T12:00:00.000Z");
const at = (offsetS: number) => new Date(NOW + offsetS * 1000).toISOString();
const snap = (patch: Partial<Snapshot> = {}): Snapshot => ({ ...emptySnapshot(), ...patch });
const task = (o: Row): Row => ({
  task_id: "T1",
  case_id: "C1",
  parent_task_id: "",
  agent: "carrier_ap",
  skill: "process_carrier_invoice",
  a2a_state: "working",
  started_at: at(0),
  updated_at: at(0),
  ...o,
});

test("taskView parses evidence_refs from arrays and JSON strings", () => {
  assert.deepEqual(taskView(task({ evidence_refs: ["a", "b"] })).evidenceRefs, ["a", "b"]);
  assert.deepEqual(taskView(task({ evidence_refs: '["x","y"]' })).evidenceRefs, ["x", "y"]);
  assert.deepEqual(taskView(task({ evidence_refs: "not json" })).evidenceRefs, []);
  assert.deepEqual(taskView(task({})).evidenceRefs, []);
});

test("taskView computes terminal and endedAt", () => {
  const done = taskView(task({ a2a_state: "completed", updated_at: at(30) }));
  assert.equal(done.terminal, true);
  assert.equal(done.endedAt, NOW + 30_000);
  const working = taskView(task({ updated_at: at(30) }));
  assert.equal(working.terminal, false);
  assert.equal(working.endedAt, 0);
  assert.equal(taskView(task({ a2a_state: "failed" })).terminal, true);
  assert.equal(taskView(task({ a2a_state: "input-required" })).terminal, false);
});

test("buildCases groups tasks per case in start order with agents in route order", () => {
  const s = snap({
    case: [{ case_id: "C1", title: "One", status: "open" }, { case_id: "C2", title: "[Synthetic] Two" }],
    task: [
      task({ task_id: "T3", agent: "risk", started_at: at(20) }),
      task({ task_id: "T1", agent: "carrier_ap", started_at: at(0), a2a_state: "completed" }),
      task({ task_id: "T2", agent: "risk", started_at: at(10) }),
      task({ task_id: "T4", case_id: "C2", agent: "cfo" }),
    ],
  });
  const cases = buildCases(s);
  const c1 = cases.get("C1")!;
  assert.deepEqual(c1.tasks.map((t) => t.taskId), ["T1", "T2", "T3"]);
  assert.deepEqual(c1.agents, ["carrier_ap", "risk"]);
  assert.equal(c1.activeTasks, 2);
  assert.equal(cases.get("C2")!.synthetic, true);
  assert.equal(c1.synthetic, false);
});

test("buildCases derives every attention kind", () => {
  const s = snap({
    case: [{ case_id: "C1" }],
    task: [
      task({ task_id: "T1", a2a_state: "failed" }),
      task({ task_id: "T2", a2a_state: "input-required" }),
    ],
    approval: [{ id: "A1", case_id: "C1", decision: "pending" }],
    flag: [{ id: "F1", case_id: "C1", status: "open" }],
    review: [{ id: "R1", case_id: "C1", verdict: "issue" }],
    hold: [{ id: "H1", case_id: "C1", status: "active" }],
    call: [{ id: "K1", case_id: "C1", status: "no_answer" }],
  });
  assert.deepEqual(buildCases(s).get("C1")!.attention, [
    "approval_pending",
    "task_failed",
    "input_required",
    "flag_open",
    "review_issue",
    "hold_active",
    "call_no_answer",
  ]);
  const clean = snap({
    case: [{ case_id: "C1" }],
    approval: [{ id: "A1", case_id: "C1", decision: "approved" }],
    flag: [{ id: "F1", case_id: "C1", status: "closed" }],
    review: [{ id: "R1", case_id: "C1", verdict: "pass" }],
    hold: [{ id: "H1", case_id: "C1", status: "released" }],
  });
  assert.deepEqual(buildCases(clean).get("C1")!.attention, []);
  // A task parked with no business status may be waiting on anyone: it needs you.
  assert.deepEqual(buildCases(s).get("C1")!.needsYou, ["approval_pending", "task_failed", "input_required", "flag_open", "review_issue", "call_no_answer"]);
  assert.deepEqual(buildCases(s).get("C1")!.agentsHandling, ["hold_active"]);
});

/* ----------------------- who acts: person or agents ----------------------- */

test("attentionAudience: every kind but a hold needs a person", () => {
  const person: AttentionItem["kind"][] = ["approval_pending", "task_failed", "policy_deny", "flag_open", "review_issue", "call_no_answer", "stale_agent"];
  for (const kind of person) assert.equal(attentionAudience(kind), "person", kind);
  assert.equal(attentionAudience("hold_active"), "agents");
  // Kinds other than input_required ignore the task's status.
  assert.equal(attentionAudience("task_failed", "awaiting_other_agent"), "person");
});

test("attentionAudience: input_required follows the business status", () => {
  const table: [string, "person" | "agents"][] = [
    ["awaiting_other_agent", "agents"],
    ["awaiting_call", "agents"],
    ["awaiting_external_verification", "agents"],
    ["awaiting_invoice", "agents"],
    ["awaiting_human_approval", "person"],
    ["awaiting_human", "person"],
    ["awaiting_approval", "person"],
    // Unknown, empty and non-waiting statuses fall back to a person, so nothing is hidden.
    ["awaiting_something_new", "person"],
    ["investigating", "person"],
    ["", "person"],
  ];
  for (const [status, audience] of table) assert.equal(attentionAudience("input_required", status, "input-required"), audience, status);
  // auth-required asks for credentials: a person, whatever the status says.
  assert.equal(attentionAudience("input_required", "awaiting_other_agent", "auth-required"), "person");
});

test("waitNote words each wait plainly", () => {
  assert.equal(waitNote("awaiting_other_agent", ["risk"]), "Waiting on Scout");
  assert.equal(waitNote("awaiting_other_agent", ["risk", "documents"]), "Waiting on Scout and Paper");
  assert.equal(waitNote("awaiting_other_agent", ["risk", "documents", "controller", "cfo"]), "Waiting on Scout, Paper and 2 more");
  assert.equal(waitNote("awaiting_other_agent", []), "Waiting on another agent");
  assert.equal(waitNote("awaiting_other_agent", ["risk"], (id) => id.toUpperCase()), "Waiting on RISK");
  assert.equal(waitNote("awaiting_call"), "Waiting on a call");
  assert.equal(waitNote("awaiting_external_verification"), "Waiting on outside documents");
  assert.equal(waitNote("awaiting_invoice"), "Waiting on the carrier invoice");
});

test("buildAttention splits items by audience and names who a parent waits on", () => {
  const s = snap({
    case: [{ case_id: "C1", title: "Case one" }],
    task: [
      // Parent parked on a running child.
      task({ task_id: "P1", a2a_state: "input-required", business_status: "awaiting_other_agent", updated_at: at(-10) }),
      task({ task_id: "K1", parent_task_id: "P1", agent: "risk", skill: "audit_accessorials", a2a_state: "working" }),
      // Parent whose delegation is recorded but whose child has no task row yet.
      task({ task_id: "P2", agent: "shipper_ar", skill: "chase_overdue", a2a_state: "input-required", business_status: "awaiting_other_agent", updated_at: at(-20) }),
      // Parent with nothing in the snapshot to say who it waits on.
      task({ task_id: "P3", a2a_state: "input-required", business_status: "awaiting_other_agent", updated_at: at(-30) }),
      // A task parked on a person.
      task({ task_id: "H1", agent: "controller", skill: "review_hold", a2a_state: "input-required", business_status: "awaiting_human", updated_at: at(-5) }),
      task({ task_id: "V1", agent: "voice_desk", skill: "place_call", a2a_state: "input-required", business_status: "awaiting_call", updated_at: at(-40) }),
    ],
    delegation_edge: [{ id: "e1", case_id: "C1", task_id: "K9", parent_task_id: "P2", from_agent: "shipper_ar", to_agent: "voice_desk", ts: at(-21) }],
    hold: [{ id: "HD1", case_id: "C1", invoice_id: "INV-1", status: "active", placed_by: "controller", reason_code: "UNSUPPORTED_ACCESSORIAL", amount: 300, placed_at: at(-2) }],
  });
  const items = buildAttention(s, buildCases(s), NOW);
  const byId = new Map(items.map((i) => [i.id, i]));
  assert.deepEqual(items.filter(needsPerson).map((i) => i.id), ["input:H1"]);
  assert.equal(byId.get("input:H1")!.title, "review hold needs input");
  assert.equal(byId.get("input:H1")!.detail, "awaiting human");

  const p1 = byId.get("input:P1")!;
  assert.equal(p1.audience, "agents");
  assert.deepEqual(p1.waitingOn, ["risk"]);
  assert.equal(p1.title, "process carrier invoice");
  assert.equal(p1.detail, "Waiting on Scout");
  assert.deepEqual(byId.get("input:P2")!.waitingOn, ["voice_desk"]);
  assert.equal(byId.get("input:P2")!.detail, "Waiting on Ringo");
  assert.equal(byId.get("input:P3")!.detail, "Waiting on another agent");
  assert.equal(byId.get("input:V1")!.detail, "Waiting on a call");

  const hold = byId.get("hold:HD1")!;
  assert.equal(hold.audience, "agents");
  assert.equal(hold.title, "Hold on INV-1");
  assert.equal(hold.detail, "Held by Stamp · unsupported accessorial");
});

test("buildAttention: a parent whose children all finished names the agent it delegated to last", () => {
  const s = snap({
    case: [{ case_id: "C1" }],
    task: [
      task({ task_id: "P1", a2a_state: "input-required", business_status: "awaiting_other_agent" }),
      task({ task_id: "K1", parent_task_id: "P1", agent: "risk", a2a_state: "completed", started_at: at(1) }),
      task({ task_id: "K2", parent_task_id: "P1", agent: "documents", a2a_state: "failed", started_at: at(2) }),
    ],
    delegation_edge: [
      { id: "e1", task_id: "K1", parent_task_id: "P1", to_agent: "risk", ts: at(1) },
      { id: "e2", task_id: "K2", parent_task_id: "P1", to_agent: "documents", ts: at(2) },
    ],
  });
  const parent = buildAttention(s, buildCases(s), NOW).find((i) => i.id === "input:P1")!;
  assert.deepEqual(parent.waitingOn, ["documents"]);
});

test("cases count as needing you only for items a person must act on", () => {
  const s = snap({
    case: [
      { case_id: "AGENTS", status: "in_progress" },
      { case_id: "PERSON", status: "in_progress" },
      { case_id: "BOTH", status: "in_progress" },
      { case_id: "DONE", status: "resolved" },
    ],
    task: [
      task({ task_id: "A1", case_id: "AGENTS", a2a_state: "input-required", business_status: "awaiting_other_agent" }),
      task({ task_id: "P1", case_id: "PERSON", a2a_state: "input-required", business_status: "awaiting_human_approval" }),
      task({ task_id: "B1", case_id: "BOTH", a2a_state: "input-required", business_status: "awaiting_call" }),
      task({ task_id: "B2", case_id: "BOTH", a2a_state: "input-required", business_status: "something_unknown" }),
      task({ task_id: "D1", case_id: "DONE", a2a_state: "input-required", business_status: "awaiting_human" }),
    ],
    hold: [{ id: "H1", case_id: "AGENTS", status: "active" }],
  });
  const cases = buildCases(s);
  assert.deepEqual(cases.get("AGENTS")!.needsYou, []);
  assert.deepEqual(cases.get("AGENTS")!.agentsHandling, ["input_required", "hold_active"]);
  assert.deepEqual(cases.get("PERSON")!.needsYou, ["input_required"]);
  assert.deepEqual(cases.get("PERSON")!.agentsHandling, []);
  assert.deepEqual(cases.get("BOTH")!.needsYou, ["input_required"]);
  assert.deepEqual(cases.get("BOTH")!.agentsHandling, ["input_required"]);
  // Resolved cases never count.
  assert.equal(buildOpsMetrics(s, cases, NOW).casesNeedingYou, 2);
});

test("buildTimeline orders events by time and maps audit kinds", () => {
  const s = snap({
    audit_event: [
      {
        id: "a1",
        case_id: "C1",
        task_id: "T1",
        agent: "risk",
        kind: "tool_call",
        ts: at(5),
        summary: JSON.stringify({ tool_name: "detention_calc", status: "completed", duration_ms: 900 }),
      },
      { id: "a2", case_id: "C1", agent: "carrier_ap", kind: "record_delegation", ts: at(3), summary: "carrier → risk" },
      { id: "a3", case_id: "C1", agent: "controller", kind: "place_hold", ts: at(6), summary: "hold" },
      { id: "a4", case_id: "C1", agent: "controller", kind: "policy_deny", ts: at(7), reason_code: "P-003", summary: "denied" },
      { id: "a5", case_id: "C1", kind: "upsert_task", ts: at(8) },
      { id: "a6", case_id: "C1", kind: "agent_heartbeat", ts: at(9) },
      { id: "a7", case_id: "C1", kind: "record_doc_read", ts: at(10) },
      { id: "a8", case_id: "C2", kind: "place_hold", ts: at(11) },
    ],
    delegation_edge: [{ id: "e1", case_id: "C1", from_agent: "carrier_ap", to_agent: "risk", skill: "investigate_charge", ts: at(2) }],
    dispatch: [{ event_id: "d1", case_id: "C1", agent: "carrier_ap", ts: at(1), reason: "classified" }],
  });
  const events = buildTimeline(s, "C1");
  assert.deepEqual(events.map((e) => e.id), ["dispatch:d1", "edge:e1", "audit:a2", "audit:a1", "audit:a3", "audit:a4"]);
  const byId = Object.fromEntries(events.map((e) => [e.id, e]));
  assert.equal(byId["audit:a1"].kind, "tool_call");
  assert.equal(byId["audit:a1"].title, "detention_calc · completed");
  assert.equal(byId["audit:a1"].detail, "900 ms");
  assert.equal(byId["edge:e1"].kind, "delegation");
  assert.equal(byId["edge:e1"].from, "carrier_ap");
  assert.equal(byId["edge:e1"].to, "risk");
  assert.equal(byId["audit:a3"].kind, "hold");
  assert.equal(byId["audit:a4"].kind, "policy");
  assert.ok(!events.some((e) => ["audit:a5", "audit:a6", "audit:a7", "audit:a8"].includes(e.id)));
  assert.equal(buildTimeline(s).length, events.length + 1); // all cases includes C2 hold
});

test("buildTimeline emits task start/end and waiting events", () => {
  const s = snap({
    task: [
      task({ task_id: "T1", a2a_state: "completed", started_at: at(0), updated_at: at(10) }),
      task({ task_id: "T2", a2a_state: "failed", started_at: at(1), updated_at: at(11) }),
      task({ task_id: "T3", a2a_state: "input-required", started_at: at(2), updated_at: at(12), business_status: "awaiting_human" }),
    ],
  });
  const kinds = buildTimeline(s).map((e) => `${e.taskId}:${e.kind}`);
  assert.ok(kinds.includes("T1:task_completed"));
  assert.ok(kinds.includes("T2:task_failed"));
  assert.ok(kinds.includes("T3:task_waiting"));
});

test("buildToolCalls merges started and completed events per tool_call_id", () => {
  const call = (id: string, ts: string, status: string, extra: object = {}) => ({
    id,
    kind: "tool_call",
    task_id: "T1",
    agent: "risk",
    ts,
    reason_code: `tool_${status}`,
    summary: JSON.stringify({ tool_call_id: "tc1", tool_name: "detention_calc", status, ...extra }),
  });
  const s = snap({
    audit_event: [
      call("b", at(4), "completed", { result: { ok: true }, duration_ms: 1500 }),
      call("a", at(2), "started", { arguments: { q: 1 } }),
      { ...call("c", at(3), "started"), task_id: "T9", summary: JSON.stringify({ tool_call_id: "tc2", tool_name: "x", status: "started" }) },
    ],
  });
  const all = buildToolCalls(s);
  assert.equal(all.length, 2);
  const merged = all.find((c) => c.id === "tc1")!;
  assert.equal(merged.status, "completed");
  assert.equal(merged.durationMs, 1500);
  assert.equal(merged.startedAt, NOW + 2000);
  assert.equal(merged.endedAt, NOW + 4000);
  assert.deepEqual(merged.arguments, { q: 1 });
  assert.deepEqual(merged.result, { ok: true });
  assert.equal(all.find((c) => c.id === "tc2")!.endedAt, 0);
  assert.deepEqual(buildToolCalls(s, "T1").map((c) => c.id), ["tc1"]);
});

test("buildWorkloads marks stale after 60 s and counts active/queued from tasks", () => {
  const s = snap({
    agent: [
      { name: "carrier_ap", last_seen: at(-30), status: "ok" },
      { name: "risk", last_seen: at(-61), status: "ok" },
      { name: "cfo", last_seen: at(-1), status: "error" },
    ],
    task: [
      task({ task_id: "T1", agent: "carrier_ap" }),
      task({ task_id: "T2", agent: "carrier_ap" }),
      task({ task_id: "T3", agent: "carrier_ap", a2a_state: "input-required", case_id: "C2" }),
      task({ task_id: "T4", agent: "carrier_ap", a2a_state: "completed" }),
    ],
  });
  const w = buildWorkloads(s, NOW);
  assert.equal(w.carrier_ap.status, "working");
  assert.equal(w.carrier_ap.active, 2);
  assert.equal(w.carrier_ap.queued, 1);
  assert.deepEqual(w.carrier_ap.activeCases, ["C1"]);
  assert.equal(w.risk.status, "stale");
  assert.equal(w.cfo.status, "error");
  assert.equal(w.controller.status, "offline");
  assert.equal(Object.keys(w).length, 9);
  assert.equal(buildWorkloads(snap({ agent: [{ name: "risk", last_seen: at(-60) }] }), NOW).risk.status, "idle");
});

const beat = (status: string, o: Row = {}): Row => ({ name: "carrier_ap", last_seen: at(-5), status, ...o });
const workload = (agent: Row | null, tasks: Row[]) =>
  buildWorkloads(snap({ agent: agent ? [agent] : [], task: tasks }), NOW).carrier_ap;

test("buildWorkloads: busy heartbeat with a fresh working task is working", () => {
  const w = workload(beat("busy"), [task({ updated_at: at(-10) })]);
  assert.equal(w.status, "working");
  assert.equal(w.active, 1);
  assert.equal(w.stalled, 0);
  assert.deepEqual(w.activeTasks.map((t) => t.taskId), ["T1"]);
});

test("buildWorkloads: idle heartbeat with orphaned working rows is idle and stalled", () => {
  const w = workload(beat("online", { active: 24 }), [
    task({ task_id: "T1", updated_at: at(-20 * 60) }),
    task({ task_id: "T2", case_id: "C2", updated_at: at(-60 * 60) }),
  ]);
  assert.equal(w.status, "idle");
  assert.equal(w.active, 0);
  assert.notEqual(w.active, 24);
  assert.equal(w.stalled, 2);
  assert.deepEqual(w.activeCases, []);
  assert.deepEqual(w.activeTasks, []);
});

test("buildWorkloads: idle heartbeat lag at task start still counts a recent row as live", () => {
  const w = workload(beat("online"), [task({ updated_at: at(-30) })]);
  assert.equal(w.status, "working");
  assert.equal(w.active, 1);
  assert.equal(w.stalled, 0);
  assert.deepEqual(w.activeCases, ["C1"]);
});

test("buildWorkloads: busy heartbeat extends liveness to 15 min but not beyond", () => {
  const w = workload(beat("busy"), [
    task({ task_id: "T1", updated_at: at(-5 * 60) }),
    task({ task_id: "T2", updated_at: at(-60 * 60) }),
  ]);
  assert.deepEqual(w.activeTasks.map((t) => t.taskId), ["T1"]);
  assert.equal(w.active, 1);
  assert.equal(w.stalled, 1);
  assert.equal(w.status, "working");
  // the same 5-minute-quiet row is stalled once the heartbeat is idle
  const idle = workload(beat("online"), [task({ updated_at: at(-5 * 60) })]);
  assert.equal(idle.stalled, 1);
  assert.equal(idle.active, 0);
});

test("buildWorkloads: busy heartbeat with no task rows is working with one active", () => {
  const w = workload(beat("busy"), []);
  assert.equal(w.status, "working");
  assert.equal(w.active, 1);
  assert.equal(w.stalled, 0);
  assert.deepEqual(w.activeTasks, []);
});

test("buildWorkloads: stale heartbeat is stale and its working rows are stalled; error beats busy", () => {
  const stale = workload(beat("busy", { last_seen: at(-61) }), [task({ updated_at: at(-10 * 60) })]);
  assert.equal(stale.status, "stale");
  assert.equal(stale.stalled, 1);
  assert.equal(stale.active, 0);
  assert.equal(workload(beat("error"), [task({ updated_at: at(-5) })]).status, "error");
  assert.equal(workload(null, []).status, "offline");
});

test("buildWorkloads: a row without updated_at ages from started_at", () => {
  const old = workload(beat("online"), [task({ updated_at: "", started_at: at(-10 * 60) })]);
  assert.equal(old.stalled, 1);
  assert.equal(old.active, 0);
  const fresh = workload(beat("online"), [task({ updated_at: "", started_at: at(-20) })]);
  assert.equal(fresh.stalled, 0);
  assert.equal(fresh.active, 1);
});

test("intakeArriving is true only for cases opened within 30 s", () => {
  const cases = (...offsets: number[]) =>
    buildCases(snap({ case: offsets.map((o, i) => ({ case_id: `C${i}`, status: "open", opened_at: at(o) })) })).values();
  assert.equal(intakeArriving(cases(-300, -600, -3600), NOW), false);
  assert.equal(intakeArriving(cases(-300, -10), NOW), true);
  assert.equal(intakeArriving(cases(-30), NOW), true);
  assert.equal(intakeArriving(cases(-31), NOW), false);
  assert.equal(intakeArriving([], NOW), false);
});

test("buildFinance computes balances from ledger lines", () => {
  const entry = (id: string, offsetDays: number, lines: object[]) => ({
    id,
    posted_at: new Date(NOW - offsetDays * 86_400_000).toISOString(),
    lines,
  });
  const s = snap({
    ledger_entry: [
      entry("1", 2, [
        { account: "accounts_receivable", debit: 1000, credit: 0 },
        { account: "freight_revenue", debit: 0, credit: 1000 },
      ]),
      entry("2", 2, [
        { account: "freight_expense", debit: 800, credit: 0 },
        { account: "accounts_payable", debit: 0, credit: 800 },
      ]),
      { ...entry("3", 1, []), lines: JSON.stringify([
        { account: "cash", debit: 500, credit: 0 },
        { account: "accounts_receivable", debit: 0, credit: 500 },
      ]) },
      entry("4", 1, [{ account: "mystery", debit: 99, credit: 0 }]),
    ],
    invoice: [
      { id: "I1", status: "open", amount: 1000, paid_amount: 200, issued_at: at(-40 * 86_400) },
      { id: "I2", status: "open", amount: 300, paid_amount: 0, issued_at: at(-86_400) },
      { id: "I3", status: "paid", amount: 700, paid_amount: 700, issued_at: at(-40 * 86_400) },
      { id: "I4", status: "held", amount: 400, paid_amount: 0, issued_at: at(-86_400) },
    ],
    hold: [
      { id: "H1", status: "active", invoice_id: "I2", amount: 300, released_amount: 100 },
      { id: "H2", status: "released", invoice_id: "I1", amount: 50, released_amount: 50 },
    ],
    payment_proposal: [
      { id: "P1", status: "proposed", amount: 100 },
      { id: "P2", status: "awaiting_approval", amount: 50 },
      { id: "P3", status: "paid", amount: 999 },
    ],
    cash_forecast: [
      { id: "F1", period: "2026-11-01", amount: 100, confidence: 0.8 },
      { id: "F2", period: "2026-11-01", amount: -40, confidence: 0.6 },
      { id: "F3", period: "2026-10-25", amount: 10, confidence: 0.5 },
    ],
  });
  const f = buildFinance(s, NOW);
  assert.equal(f.cash, 500);
  assert.equal(f.ar, 500);
  assert.equal(f.ap, 800);
  assert.equal(f.revenue30d, 1000);
  assert.equal(f.revenueMtd, 1000);
  assert.equal(f.expense30d, 800);
  assert.equal(f.grossMarginPct, 20);
  assert.deepEqual(f.outstandingInvoices, { count: 3, amount: 800 + 300 + 400 } /* open includes held */);
  assert.deepEqual(f.overdueInvoices, { count: 1, amount: 800 });
  assert.equal(f.heldInvoices.count, 2); // I2 (hold) + I4 (status)
  assert.equal(f.heldInvoices.amount, 200);
  assert.deepEqual(f.paymentsProposed, { count: 2, amount: 150 });
  assert.deepEqual(f.forecast.map((p) => p.period), ["2026-10-25", "2026-11-01"]);
  const nov = f.forecast[1];
  assert.equal(nov.inflow, 100);
  assert.equal(nov.outflow, 40);
  assert.equal(nov.net, 60);
  assert.ok(Math.abs(nov.confidence - 0.7) < 1e-9);
  for (const series of Object.values(f.trends)) assert.equal(series.length, 31);
  assert.equal(f.trends.cash.at(-1)!.value, 500);
  assert.equal(f.trends.cash[0].value, 0);
  assert.equal(f.trends.revenue.reduce((a, p) => a + p.value, 0), 1000);
  assert.equal(f.trends.ap.at(-1)!.value, 800);
});

test("buildFinance on an empty snapshot is all zeros", () => {
  const f = buildFinance(snap(), NOW);
  assert.equal(f.cash + f.ap + f.ar + f.revenue30d, 0);
  assert.equal(f.grossMarginPct, 0);
  assert.equal(f.trends.cash.length, 31);
});

test("buildAttention puts critical approvals first and sorts by severity then recency", () => {
  const s = snap({
    case: [{ case_id: "C1", title: "Case one", exposure_usd: 1234 }],
    approval: [
      { id: "A1", case_id: "C1", decision: "pending", amount: 100, action: "propose_payment", requested_at: at(-1) },
      { id: "A2", case_id: "C1", decision: "pending", amount: 6000, action: "release_hold", requested_at: at(-100) },
      { id: "A3", case_id: "C1", decision: "approved", amount: 9000 },
    ],
    task: [task({ task_id: "T1", a2a_state: "failed", updated_at: at(-5) })],
    hold: [{ id: "H1", case_id: "C1", status: "active", amount: 50, released_amount: 10, placed_at: at(-1) }],
  });
  const items = buildAttention(s, buildCases(s), NOW);
  assert.deepEqual(items.map((i) => i.id), ["approval:A2", "approval:A1", "failed:T1", "hold:H1"]);
  assert.deepEqual(items.map((i) => i.audience), ["person", "person", "person", "agents"]);
  assert.deepEqual(items.map((i) => i.severity), ["critical", "high", "high", "low"]);
  assert.equal(items[3].amount, 40);
  assert.equal(items[0].detail, "Case one");
});

test("buildAttention drops review issues once their case resolves", () => {
  const s = snap({
    case: [
      { case_id: "OPEN", status: "open" },
      { case_id: "DONE", status: "resolved" },
    ],
    review: [
      { id: "R1", case_id: "OPEN", verdict: "issue", ts: at(-1) },
      { id: "R2", case_id: "DONE", verdict: "issue", ts: at(-1) },
    ],
  });
  assert.deepEqual(buildAttention(s, buildCases(s), NOW).map((i) => i.id), ["review:R1"]);
});

test("diffSnapshots emits exactly the new events and nothing when unchanged", () => {
  const base = snap({
    case: [{ case_id: "C1", opened_at: at(0) }],
    task: [task({ task_id: "T1" }), task({ task_id: "T2", a2a_state: "working" })],
    approval: [{ id: "A1", case_id: "C1", decision: "pending", amount: 10, requested_at: at(0) }],
    call: [{ id: "K1", case_id: "C1", status: "ringing", started_at: at(0) }],
  });
  assert.deepEqual(diffSnapshots(base, base), []);
  const next = snap({
    case: [...base.case, { case_id: "C2", opened_at: at(5) }],
    dispatch: [{ event_id: "d1", case_id: "C2", task_id: "T9", agent: "risk", skill: "screen_bank_change", ts: at(5) }],
    task: [
      task({ task_id: "T1", a2a_state: "completed", updated_at: at(8), outcome: "approved" }),
      task({ task_id: "T2" }),
      task({ task_id: "T9", case_id: "C2", agent: "risk" }),
    ],
    delegation_edge: [{ id: "e1", case_id: "C1", task_id: "T2", from_agent: "carrier_ap", to_agent: "risk", skill: "s", ts: at(6) }],
    audit_event: [
      { id: "a1", kind: "tool_call", case_id: "C1", task_id: "T1", agent: "risk", ts: at(7), summary: JSON.stringify({ tool_name: "t", status: "started" }) },
      { id: "a2", kind: "upsert_task", case_id: "C1", ts: at(7) },
    ],
    approval: [{ ...base.approval[0], decision: "approved", decided_at: at(9) }],
    call: [{ ...base.call[0], status: "completed", ended_at: at(9) }],
  });
  const types = diffSnapshots(base, next).map((e) => e.type);
  assert.deepEqual(
    [...types].sort(),
    ["approval", "call", "case_opened", "delegation", "dispatch", "task_finished", "task_started", "tool_call"].sort(),
  );
  const fin = diffSnapshots(base, next).find((e) => e.type === "task_finished") as { state: string; outcome: string };
  assert.equal(fin.state, "completed");
  assert.equal(fin.outcome, "approved");
  const appr = diffSnapshots(base, next).find((e) => e.type === "approval") as { decision: string };
  assert.equal(appr.decision, "approved");
  // null prev treats everything as new
  assert.ok(diffSnapshots(null, base).some((e) => e.type === "case_opened"));
});

test("fmtMoney / fmtDuration / fmtAgo edge cases", () => {
  assert.equal(fmtMoney(1234.5), "$1,235");
  assert.equal(fmtMoney(0), "$0");
  assert.equal(fmtMoney(-50), "-$50");
  assert.equal(fmtMoney(1_500_000, true), "$1.5M");
  assert.equal(fmtDuration(0), "—");
  assert.equal(fmtDuration(NaN), "—");
  assert.equal(fmtDuration(Infinity), "—");
  assert.equal(fmtDuration(45), "45s");
  assert.equal(fmtDuration(125), "2m 5s");
  assert.equal(fmtDuration(3_660), "1h 1m");
  assert.equal(fmtAgo(NOW, NOW), "just now");
  assert.equal(fmtAgo(NOW + 10_000, NOW), "just now"); // future clamps
  assert.equal(fmtAgo(NOW - 30_000, NOW), "30s ago");
  assert.equal(fmtAgo(NOW - 5 * 60_000, NOW), "5m ago");
  assert.equal(fmtAgo(NOW - 3 * 3_600_000, NOW), "3h ago");
  assert.equal(fmtAgo(NOW - 2 * 86_400_000, NOW), "2d ago");
});

test("diffSnapshots: live doc reads count only alongside their audit row", () => {
  const old = (n: number) => ({ id: `DR-old-${n}`, task_id: "T1", agent: "documents", doc_ref: `doc:old-${n}` });
  const base = snap({ task: [task({ task_id: "T1" })] });
  // A task update pulls its old reads into the window: history, not news.
  const replay = snap({ task: [task({ task_id: "T1", updated_at: at(30) })], doc_read: [old(1), old(2)] });
  assert.deepEqual(diffSnapshots(base, replay).filter((e) => e.type === "doc_read"), []);
  // A fresh read arrives with its record_doc_read audit row, possibly beside old ones.
  const fresh = snap({
    task: replay.task,
    doc_read: [old(1), old(2), { id: "DR-new", task_id: "T1", agent: "documents", doc_ref: "doc:new" }],
    audit_event: [{ id: "a9", kind: "record_doc_read", case_id: "C1", task_id: "T1", ts: at(31) }],
  });
  const reads = diffSnapshots(base, fresh).filter((e) => e.type === "doc_read") as { docRef: string; at: number }[];
  assert.deepEqual(reads.map((e) => e.docRef), ["doc:new"]);
  assert.equal(reads[0].at, Date.parse(at(31)));
  // Simulated reads carry their own timestamp and need no audit row.
  const sim = snap({ doc_read: [{ id: "DR-s", task_id: "T1", doc_ref: "doc:s", ts: at(4) }] });
  assert.equal(diffSnapshots(base, sim).filter((e) => e.type === "doc_read").length, 1);
});


/* ---------------------------- imported history ---------------------------- */

const HIST = "HIST-LD-0915-3FA2C1";

test("imported cases are flagged, need nothing and stay out of agent metrics", () => {
  const s = snap({
    case: [
      { case_id: HIST, load_id: "LD-0915-3FA2C1", title: "Mesa invoice on LD-0915-3FA2C1", status: "resolved", opened_at: at(-30 * 86_400), resolved_at: at(-29 * 86_400) },
      { case_id: "CASE-1", title: "Live", status: "resolved", opened_at: at(-120), resolved_at: at(-60) },
    ],
    task: [task({ task_id: "T1", case_id: "CASE-1", a2a_state: "completed" })],
    invoice: [{ id: "INV-H", case_id: HIST, carrier_id: "C-1", amount: 1700, paid_amount: 0, issued_at: at(-30 * 86_400), status: "open" }],
  });
  const cases = buildCases(s);
  assert.equal(cases.get(HIST)!.imported, true);
  assert.equal(cases.get("CASE-1")!.imported, false);
  assert.deepEqual(cases.get(HIST)!.attention, []);
  assert.deepEqual(cases.get(HIST)!.tasks, []);
  const m = buildOpsMetrics(s, cases, NOW);
  assert.equal(m.casesTotal, 2);
  assert.equal(m.casesResolved, 2);
  assert.equal(m.casesNeedingYou, 0);
  assert.equal(m.medianResolutionS, 60, "only the live case's resolution time counts");
  assert.equal(m.minutesSaved, 18);
  assert.equal(m.automationRate, 1);
  assert.deepEqual(buildAttention(s, cases, NOW), []);
});

test("an imported case landing is not a new-case event", () => {
  const next = snap({ case: [{ case_id: HIST, opened_at: at(-30 * 86_400) }, { case_id: "CASE-2", opened_at: at(0) }] });
  const opened = diffSnapshots(snap(), next).filter((e) => e.type === "case_opened").map((e) => e.caseId);
  assert.deepEqual(opened, ["CASE-2"]);
});

test("buildFinance counts accessorial revenue and expense with freight", () => {
  const s = snap({
    ledger_entry: [
      { id: "B", posted_at: at(-3600), lines: [{ account: "accounts_receivable", debit: 1250, credit: 0 }, { account: "freight_revenue", debit: 0, credit: 1000 }, { account: "accessorial_revenue", debit: 0, credit: 250 }] },
      { id: "C", posted_at: at(-3500), lines: [{ account: "freight_expense", debit: 850, credit: 0 }, { account: "accessorial_expense", debit: 200, credit: 0 }, { account: "accounts_payable", debit: 0, credit: 1050 }] },
    ],
  });
  const f = buildFinance(s, NOW);
  assert.equal(f.revenue30d, 1250);
  assert.equal(f.expense30d, 1050);
  assert.equal(f.ar, 1250);
  assert.equal(f.ap, 1050);
  assert.equal(f.since, null);
});

test("contract delta is the bill against its contract, not the open balance", () => {
  // Partly paid: $1,000 of a $2,150 bill paid against a $2,000 contract is still $150 over.
  assert.equal(invoiceContractDelta({ amount: 2150, paid_amount: 1000, contract_amount: 2000 }), 150);
  assert.equal(invoiceContractDelta({ amount: "1900.10", contract_amount: "2000" }), -99.9);
  assert.equal(invoiceContractDelta({ amount: 2150, contract_amount: 0 }), 0);
  assert.equal(invoiceContractDelta({ amount: 2150 }), 0);
});
