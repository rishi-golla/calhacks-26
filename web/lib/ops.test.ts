import assert from "node:assert/strict";
import test from "node:test";
import { emptySnapshot, type Row, type Snapshot } from "./data";
import {
  buildAttention,
  buildCases,
  buildFinance,
  buildTimeline,
  buildToolCalls,
  buildWorkloads,
  diffSnapshots,
  fmtAgo,
  fmtDuration,
  fmtMoney,
  taskView,
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
  assert.deepEqual(items.map((i) => i.severity), ["critical", "high", "high", "low"]);
  assert.equal(items[3].amount, 40);
  assert.equal(items[0].detail, "Case one");
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
