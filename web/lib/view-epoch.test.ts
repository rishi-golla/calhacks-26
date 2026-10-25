import assert from "node:assert/strict";
import test from "node:test";
import { SCOPED_TABLES, CARRY_GRACE_MS, carriedOver, fmtSince, readViewEpoch, rowInScope, scopeEvents, scopeSnapshot, showEarlierApplies, showEarlierValue, viewScope } from "./view-epoch";
import { emptySnapshot, type Row, type Snapshot } from "./data";
import { DemoSim } from "./demo-sim";
import { buildFinance, buildWorkloads, type LiveEvent } from "./ops";

const NOW = Date.parse("2026-10-24T12:00:00.000Z");
const at = (offsetS: number) => new Date(NOW + offsetS * 1000).toISOString();
const snap = (patch: Partial<Snapshot> = {}): Snapshot => ({ ...emptySnapshot(), ...patch });
const SINCE = NOW;
const GRACE_S = CARRY_GRACE_MS / 1000;
const DAY = 86_400;

const kase = (id: string, openedOffsetS: number): Row => ({ case_id: id, title: id, opened_at: at(openedOffsetS) });
const task = (o: Row): Row => ({
  task_id: "T1",
  case_id: "C1",
  agent: "carrier_ap",
  skill: "process_carrier_invoice",
  a2a_state: "working",
  started_at: at(-DAY),
  updated_at: at(-DAY),
  ...o,
});
const epoch = (since: unknown, extra: Row = {}): Row => ({ id: "current", since, set_by: "tester", set_at: at(0), ...extra });
const scoped = (s: Snapshot, since = SINCE) => scopeSnapshot(s, viewScope(s, since));

/* ------------------------------ readViewEpoch ----------------------------- */

test("readViewEpoch: no rows or missing table is null", () => {
  assert.equal(readViewEpoch(snap()), null);
  assert.equal(readViewEpoch({ ...emptySnapshot(), view_epoch: undefined } as unknown as Snapshot), null);
});

test("readViewEpoch: prefers the current row and parses ISO strings", () => {
  const e = readViewEpoch(snap({ view_epoch: [epoch(at(-100), { id: "old" }), epoch(at(-50))] }));
  assert.deepEqual(e, { since: NOW - 50_000, setBy: "tester", setAt: NOW });
});

test("readViewEpoch: falls back to the first row when none is current", () => {
  const e = readViewEpoch(snap({ view_epoch: [epoch(at(-10), { id: "other" })] }));
  assert.equal(e?.since, NOW - 10_000);
});

test("readViewEpoch: parses microsecond numbers and rejects junk", () => {
  assert.equal(readViewEpoch(snap({ view_epoch: [epoch(NOW * 1000)] }))?.since, NOW);
  assert.equal(readViewEpoch(snap({ view_epoch: [epoch("garbage")] })), null);
  assert.equal(readViewEpoch(snap({ view_epoch: [epoch(undefined)] })), null);
});

/* ------------------------------ since filtering --------------------------- */

test("cases opened at or after since are visible, earlier idle ones hidden", () => {
  const s = snap({ case: [kase("NEW", 0), kase("LATER", 60), kase("OLD", -DAY)] });
  const out = scoped(s);
  assert.deepEqual(out.case.map((c) => c.case_id), ["NEW", "LATER"]);
});

test("case-linked rows follow their case across every scoped table", () => {
  const linked = (extra: Row = {}): Row => ({ id: "x", case_id: "NEW", ...extra });
  const orphan = (extra: Row = {}): Row => ({ id: "y", case_id: "OLD", ...extra });
  const s = snap({
    case: [kase("NEW", 10), kase("OLD", -DAY)],
    task: [task({ task_id: "TN", case_id: "NEW", a2a_state: "completed", started_at: at(20), updated_at: at(30) }), task({ task_id: "TO", case_id: "OLD", a2a_state: "completed" })],
    dispatch: [linked({ ts: at(-DAY) }), orphan({ ts: at(100) })],
    delegation_edge: [linked({ ts: at(-DAY) }), orphan({ ts: at(100) })],
    review: [linked({ ts: at(-DAY) }), orphan({ ts: at(100) })],
    approval: [linked({ decision: "approved", requested_at: at(-DAY) }), orphan({ decision: "approved", requested_at: at(100) })],
    flag: [linked({ status: "closed", ts: at(-DAY) }), orphan({ status: "closed", ts: at(100) })],
    hold: [linked({ status: "released", placed_at: at(-DAY) }), orphan({ status: "released", placed_at: at(100) })],
    payment_proposal: [linked({ proposed_at: at(-DAY) }), orphan({ proposed_at: at(100) })],
    invoice: [linked({ issued_at: at(-30 * DAY) }), orphan({ issued_at: at(100) })],
    call: [linked({ started_at: at(-DAY) }), orphan({ started_at: at(100) })],
    audit_event: [linked({ ts: at(-DAY) }), orphan({ ts: at(100) })],
    cash_forecast: [linked({ updated_at: at(-DAY) }), orphan({ updated_at: at(100) })],
  });
  const out = scoped(s);
  assert.deepEqual(out.task.map((t) => t.task_id), ["TN"]);
  for (const table of ["dispatch", "delegation_edge", "review", "approval", "flag", "hold", "payment_proposal", "invoice", "call", "audit_event", "cash_forecast"] as const)
    assert.deepEqual(out[table].map((r) => r.id), ["x"], table);
});

test("rows with no known case go by their own timestamp", () => {
  const s = snap({
    audit_event: [
      { id: "before", ts: at(-10) },
      { id: "after", ts: at(10) },
      { id: "edge", ts: at(0) },
      { id: "unknown-case-before", case_id: "GHOST", ts: at(-10) },
      { id: "unknown-case-after", case_id: "GHOST", ts: at(10) },
    ],
  });
  assert.deepEqual(scoped(s).audit_event.map((r) => r.id), ["after", "edge", "unknown-case-after"]);
});

test("live doc_read follows its task's case; unknown task with no ts is hidden", () => {
  const s = snap({
    case: [kase("NEW", 5), kase("OLD", -DAY)],
    task: [task({ task_id: "TN", case_id: "NEW", updated_at: at(5), started_at: at(5) }), task({ task_id: "TO", case_id: "OLD", a2a_state: "completed" })],
    doc_read: [
      { id: "d1", task_id: "TN" },
      { id: "d2", task_id: "TO" },
      { id: "d3", task_id: "NOPE" },
      { id: "d4", ts: at(10) },
    ],
  });
  assert.deepEqual(scoped(s).doc_read.map((r) => r.id), ["d1", "d4"]);
});

test("ledger_entry needs posted_at >= since even in a visible case", () => {
  const s = snap({
    case: [kase("CARRY", -DAY), kase("NEW", 5)],
    task: [task({ task_id: "TC", case_id: "CARRY", updated_at: at(-10) })],
    ledger_entry: [
      { id: "carried-pre", case_id: "CARRY", posted_at: at(-DAY) },
      { id: "carried-post", case_id: "CARRY", posted_at: at(10) },
      { id: "new-pre", case_id: "NEW", posted_at: at(-1) },
      { id: "new-post", case_id: "NEW", posted_at: at(10) },
      { id: "caseless-pre", posted_at: at(-DAY) },
      { id: "caseless-post", posted_at: at(10) },
    ],
  });
  const scope = viewScope(s, SINCE);
  assert.ok(scope.carried.has("CARRY"));
  assert.deepEqual(scopeSnapshot(s, scope).ledger_entry.map((r) => r.id), ["carried-post", "new-post", "caseless-post"]);
});

test("unscoped tables keep the same array identity", () => {
  const s = snap({
    case: [kase("OLD", -DAY)],
    agent: [{ name: "risk" }],
    agent_config: [{ agent: "risk" }],
    metric: [{ name: "m" }],
    company: [{ id: "co" }],
    view_epoch: [epoch(at(0))],
  });
  const out = scoped(s);
  for (const table of ["agent", "agent_config", "metric", "company", "view_epoch"] as const) assert.equal(out[table], s[table], table);
  assert.ok(!SCOPED_TABLES.includes("agent" as never));
});

test("identity: no-loss tables return the input array; equal results reuse previous", () => {
  const s = snap({
    case: [kase("NEW", 5), kase("OLD", -DAY)],
    audit_event: [
      { id: "a", ts: at(10) },
      { id: "b", ts: at(-10) },
    ],
    dispatch: [{ id: "d", case_id: "NEW", ts: at(10) }],
  });
  const first = scoped(s);
  assert.equal(first.dispatch, s.dispatch); // lost nothing
  assert.notEqual(first.audit_event, s.audit_event);
  assert.notEqual(first.case, s.case);
  const second = scopeSnapshot(s, viewScope(s, SINCE), first);
  assert.equal(second.audit_event, first.audit_event);
  assert.equal(second.case, first.case);
  // A change in the result breaks reuse.
  const s2 = { ...s, audit_event: [...s.audit_event, { id: "c", ts: at(20) }] };
  const third = scopeSnapshot(s2, viewScope(s2, SINCE), first);
  assert.notEqual(third.audit_event, first.audit_event);
  assert.equal(third.audit_event.length, 2);
});

test("rowInScope passes unscoped tables through", () => {
  const scope = viewScope(snap(), SINCE);
  assert.equal(rowInScope("agent", {}, scope), true);
});

/* -------------------------------- carry-over ------------------------------ */

const carriedBy = (patch: Partial<Snapshot>): boolean => {
  const s = snap({ case: [kase("C1", -DAY)], ...patch });
  return viewScope(s, SINCE).carried.has("C1");
};

test("carried: task activity around the epoch", () => {
  assert.equal(carriedBy({ task: [task({ updated_at: at(-GRACE_S + 5) })] }), true, "non-terminal within grace");
  assert.equal(carriedBy({ task: [task({ a2a_state: "submitted", updated_at: at(-GRACE_S) })] }), true, "exactly at grace edge");
  assert.equal(carriedBy({ task: [task({ started_at: at(10), updated_at: at(10) })] }), true, "started after since");
  assert.equal(carriedBy({ task: [task({ a2a_state: "completed", started_at: at(-DAY), updated_at: at(30) })] }), true, "finished after since");
});

test("carried: pending approvals, open flags, recent holds", () => {
  assert.equal(carriedBy({ approval: [{ id: "A", case_id: "C1", decision: "pending" }] }), true, "no expiry");
  assert.equal(carriedBy({ approval: [{ id: "A", case_id: "C1", decision: "pending", expires_at: at(0) }] }), true, "expires at since");
  assert.equal(carriedBy({ approval: [{ id: "A", case_id: "C1", decision: "pending", expires_at: at(600) }] }), true, "expires later");
  assert.equal(carriedBy({ approval: [{ id: "A", case_id: "C1" }] }), true, "missing decision is pending");
  assert.equal(carriedBy({ flag: [{ id: "F", case_id: "C1", status: "open" }] }), true);
  assert.equal(carriedBy({ flag: [{ id: "F", case_id: "C1", status: "reviewing" }] }), true);
  assert.equal(carriedBy({ hold: [{ id: "H", case_id: "C1", status: "active", placed_at: at(-GRACE_S + 5) }] }), true, "within grace");
  assert.equal(carriedBy({ hold: [{ id: "H", case_id: "C1", status: "held", placed_at: at(30) }] }), true, "placed after since");
});

test("not carried: abandoned, finished, expired, decided, closed, stale or released", () => {
  assert.equal(carriedBy({ task: [task({ a2a_state: "submitted", started_at: at(-3 * DAY), updated_at: at(-3 * DAY) })] }), false, "abandoned");
  assert.equal(carriedBy({ task: [task({ a2a_state: "working", updated_at: at(-GRACE_S - 1) })] }), false, "quiet past grace");
  assert.equal(carriedBy({ task: [task({ a2a_state: "completed", updated_at: at(-10) })] }), false, "terminal before since");
  assert.equal(carriedBy({ task: [task({ a2a_state: "failed", updated_at: at(-10) })] }), false);
  assert.equal(carriedBy({ approval: [{ id: "A", case_id: "C1", decision: "pending", expires_at: at(-10) }] }), false, "expired");
  assert.equal(carriedBy({ approval: [{ id: "A", case_id: "C1", decision: "approved" }] }), false, "decided");
  assert.equal(carriedBy({ flag: [{ id: "F", case_id: "C1", status: "resolved" }] }), false);
  assert.equal(carriedBy({ flag: [{ id: "F", case_id: "C1", status: "closed" }] }), false);
  assert.equal(carriedBy({ hold: [{ id: "H", case_id: "C1", status: "active", placed_at: at(-GRACE_S - 60) }] }), false, "stale hold");
  assert.equal(carriedBy({ hold: [{ id: "H", case_id: "C1", status: "released", placed_at: at(30) }] }), false, "released");
});

test("carriedOver only returns candidate cases", () => {
  const s = snap({ case: [kase("C1", -DAY), kase("C2", -DAY)], flag: [{ id: "F", case_id: "C2", status: "open" }, { id: "G", case_id: "OTHER", status: "open" }] });
  assert.deepEqual([...carriedOver(s, SINCE, new Set(["C1", "C2"]))], ["C2"]);
});

test("a carried-over case keeps its whole history", () => {
  const s = snap({
    case: [kase("C1", -DAY)],
    task: [
      task({ task_id: "TOLD", a2a_state: "completed", started_at: at(-DAY), updated_at: at(-DAY + 60) }),
      task({ task_id: "TLIVE", updated_at: at(-5) }),
    ],
    audit_event: [{ id: "a", case_id: "C1", ts: at(-DAY) }],
  });
  const out = scoped(s);
  assert.deepEqual(out.task.map((t) => t.task_id), ["TOLD", "TLIVE"]);
  assert.deepEqual(out.audit_event.map((r) => r.id), ["a"]);
  assert.deepEqual(out.case.map((c) => c.case_id), ["C1"]);
});

/* -------------------------------- scopeEvents ----------------------------- */

test("scopeEvents keys on the case when known, else on time", () => {
  const s = snap({ case: [kase("NEW", 5), kase("OLD", -DAY)] });
  const scope = viewScope(s, SINCE);
  const ev = (caseId: string, offsetS: number): LiveEvent => ({ type: "case_opened", at: NOW + offsetS * 1000, caseId, agent: "intake" });
  const events = [ev("NEW", -9999), ev("OLD", 9999), ev("GHOST", -10), ev("GHOST", 10), ev("", 10), ev("", -10)];
  assert.deepEqual(
    scopeEvents(events, scope).map((e) => [e.caseId, e.at - NOW]),
    [["NEW", -9999_000], ["GHOST", 10_000], ["", 10_000]],
  );
});

/* --------------------------- workloads / finance -------------------------- */

test("buildWorkloads with since counts only tasks finished since, ignoring the heartbeat", () => {
  const s = snap({
    agent: [{ name: "carrier_ap", status: "idle", last_seen: at(0), done: 500, errors: 40 }],
    task: [
      task({ task_id: "a", a2a_state: "completed", updated_at: at(10) }),
      task({ task_id: "b", a2a_state: "completed", updated_at: at(-10) }),
      task({ task_id: "c", a2a_state: "completed", updated_at: at(0) }),
      task({ task_id: "d", a2a_state: "failed", updated_at: at(5) }),
      task({ task_id: "e", a2a_state: "failed", updated_at: at(-5) }),
      task({ task_id: "f", agent: "risk", a2a_state: "completed", updated_at: at(5) }),
    ],
  });
  const w = buildWorkloads(s, NOW, { since: SINCE });
  assert.equal(w.carrier_ap.done, 2);
  assert.equal(w.carrier_ap.errors, 1);
  assert.equal(w.risk.done, 1);
  assert.equal(w.carrier_ap.done + w.risk.done, 3);
});

test("buildWorkloads without since still uses the heartbeat", () => {
  const s = snap({
    agent: [{ name: "carrier_ap", status: "idle", last_seen: at(0), done: 500, errors: 40 }],
    task: [task({ a2a_state: "completed", updated_at: at(10) })],
  });
  const w = buildWorkloads(s, NOW);
  assert.equal(w.carrier_ap.done, 500);
  assert.equal(w.carrier_ap.errors, 40);
});

test("buildFinance ledgerOnly ignores the cash_position metric", () => {
  const s = snap({
    metric: [{ name: "cash_position", value: 9_999_999 }],
    ledger_entry: [{ id: "L", posted_at: at(-60), lines: [{ account: "cash", debit: 1000, credit: 0 }, { account: "freight_revenue", debit: 0, credit: 1000 }] }],
  });
  assert.equal(buildFinance(s, NOW).cash, 9_999_999);
  assert.equal(buildFinance(s, NOW, { ledgerOnly: true }).cash, 1000);
});

/* ----------------------------------- sim ---------------------------------- */

const makeSim = () => new DemoSim({ seed: 1, now: NOW });

test("sim: setViewEpoch sets, overrides and clears the epoch", () => {
  const sim = makeSim();
  assert.equal(readViewEpoch(sim.snapshot()), null);
  sim.setViewEpoch();
  const e = readViewEpoch(sim.snapshot());
  assert.ok(e);
  assert.ok(Math.abs(e.since - NOW) < 60_000, `since ${e.since} vs ${NOW}`);
  sim.setViewEpoch({ since: NOW - 123_000 });
  assert.equal(readViewEpoch(sim.snapshot())?.since, NOW - 123_000);
  sim.setViewEpoch({ clear: true });
  assert.equal(readViewEpoch(sim.snapshot()), null);
});

test("sim: right after Start fresh every visible case is new or carried; ledger history hidden", () => {
  const sim = makeSim();
  sim.setViewEpoch();
  const s = sim.snapshot();
  const since = readViewEpoch(s)!.since;
  const scope = viewScope(s, since);
  const out = scopeSnapshot(s, scope);
  assert.ok(s.case.length > 0);
  for (const c of out.case) {
    const id = String(c.case_id);
    assert.ok(Date.parse(String(c.opened_at)) >= since || scope.carried.has(id), `${id} neither new nor carried`);
  }
  assert.ok(s.ledger_entry.length > 0);
  assert.equal(out.ledger_entry.length, 0);
});

/* ------------------------------ viewer helpers ---------------------------- */

test("an empty view_epoch means no scoping", () => {
  assert.equal(readViewEpoch(snap({ view_epoch: [] })), null);
});

test("showEarlierApplies is tied to the epoch it was chosen for", () => {
  const e = { since: NOW, setBy: "x", setAt: NOW };
  assert.equal(showEarlierApplies(showEarlierValue(e), e), true);
  assert.equal(showEarlierApplies(showEarlierValue({ ...e, since: NOW + 1 }), e), false);
  assert.equal(showEarlierApplies(null, e), false);
  assert.equal(showEarlierApplies(undefined, e), false);
  assert.equal(showEarlierApplies(showEarlierValue(e), null), false);
});

test("fmtSince: time only today, month and day otherwise", () => {
  const same = fmtSince(NOW - 60_000, NOW);
  assert.match(same, /^\d{2}:\d{2}$/);
  const other = fmtSince(NOW - 3 * 86_400_000, NOW);
  assert.match(other, /^.+, \d{2}:\d{2}$/);
  assert.match(other, /[A-Za-z]/);
});
