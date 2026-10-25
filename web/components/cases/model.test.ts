import assert from "node:assert/strict";
import test from "node:test";
import { emptySnapshot, type Row, type Snapshot } from "@/lib/data";
import { buildCases } from "@/lib/ops";
import {
  buildCaseTrace,
  caseKind,
  dayClock,
  shortDur,
  dispatchEventType,
  dispatchReason,
  eventLabel,
  isMilestone,
  layoutTrace,
  milestoneAt,
  milestoneLabel,
  splitDispatches,
  LAYOUT,
  MILESTONE_STEP,
} from "./model";

const T0 = Date.parse("2026-10-24T12:00:00.000Z");
const at = (offsetS: number) => new Date(T0 + offsetS * 1000).toISOString();
const ms = (offsetS: number) => T0 + offsetS * 1000;
const snap = (patch: Partial<Snapshot> = {}): Snapshot => ({ ...emptySnapshot(), ...patch });

const dispatch = (o: Row): Row => ({
  event_id: "E1",
  case_id: "C1",
  skill: "",
  agent: "",
  task_id: "",
  status: "not_routed",
  reason: "",
  queue_ms: 0,
  policy_version: "v1",
  ts: at(0),
  ...o,
});
const notRouted = (id: string, type: string, offsetS: number, extra: Row = {}) =>
  dispatch({ event_id: id, reason: `${type} is not routed (7.3)`, ts: at(offsetS), ...extra });
const routed = (id: string, offsetS: number, extra: Row = {}) =>
  dispatch({
    event_id: id,
    status: "accepted",
    reason: "7.3 table: pod.received",
    agent: "carrier_ap",
    skill: "process_carrier_invoice",
    task_id: "T1",
    ts: at(offsetS),
    ...extra,
  });
const task = (o: Row = {}): Row => ({
  task_id: "T1",
  case_id: "C1",
  parent_task_id: "",
  agent: "carrier_ap",
  skill: "process_carrier_invoice",
  a2a_state: "working",
  started_at: at(44),
  updated_at: at(44),
  ...o,
});

/* ------------------------------ labels ------------------------------ */

test("eventLabel maps known types and humanizes unknown ones", () => {
  assert.equal(eventLabel("load.delivered"), "Load delivered");
  assert.equal(eventLabel("pod.received"), "Proof of delivery received");
  assert.equal(eventLabel("load.status"), "Load status");
  assert.equal(eventLabel(""), "Event");
});

test("milestoneLabel gives short lifecycle labels and falls back", () => {
  assert.equal(milestoneLabel("load.tendered"), "Tendered");
  assert.equal(milestoneLabel("load.dispatched"), "Dispatched");
  assert.equal(milestoneLabel("load.delivered"), "Delivered");
  assert.equal(milestoneLabel("pod.received"), "Proof of delivery received");
  assert.equal(milestoneLabel(""), "Logged");
});

test("dispatchEventType parses the type from every reason shape", () => {
  assert.equal(dispatchEventType("load.delivered is not routed (7.3)"), "load.delivered");
  assert.equal(dispatchEventType("scenario -> load.tendered is not routed (7.3)"), "load.tendered");
  assert.equal(dispatchEventType("7.3 table: pod.received"), "pod.received");
  assert.equal(dispatchEventType("7.3 table: scenario -> bank.event"), "bank.event");
  assert.equal(dispatchEventType("delivered by dispatch worker"), "");
  assert.equal(dispatchEventType(""), "");
});

test("dispatchReason is plain English with no 7.3 and no raw lifecycle types", () => {
  assert.equal(dispatchReason("load.delivered is not routed (7.3)"), "Load delivered · logged");
  assert.equal(dispatchReason("scenario -> load.tendered is not routed (7.3)"), "Load tendered · logged");
  assert.equal(dispatchReason("7.3 table: pod.received"), "Proof of delivery received");
  assert.equal(dispatchReason("delivered by dispatch worker"), "delivered by dispatch worker");
  for (const type of ["load.tendered", "load.dispatched", "load.delivered", "gps.ping"]) {
    for (const reason of [`${type} is not routed (7.3)`, `scenario -> ${type} is not routed (7.3)`, `7.3 table: ${type}`]) {
      const out = dispatchReason(reason);
      assert.ok(!out.includes("7.3"), out);
      assert.ok(!out.includes("load."), out);
      assert.ok(!out.includes("gps."), out);
    }
  }
});

test("dispatchReason strips (7.3) from free text and lower-cases known event types", () => {
  const out = dispatchReason("held: load.delivered arrived early (7.3)");
  assert.equal(out, "held: load delivered arrived early");
  assert.ok(!out.includes("7.3"));
});

/* ------------------------------ splitting ------------------------------ */

test("isMilestone is true only for not_routed", () => {
  assert.equal(isMilestone(dispatch({ status: "not_routed" })), true);
  for (const status of ["accepted", "triage", "duplicate"]) assert.equal(isMilestone(dispatch({ status })), false);
});

test("splitDispatches keeps decisions and turns not_routed rows into ordered milestones", () => {
  const rows = [
    routed("E5", 42),
    notRouted("E3", "load.delivered", 40),
    dispatch({ event_id: "E6", status: "triage", reason: "no agent; held for a person", ts: at(50) }),
    notRouted("E1", "load.tendered", 0),
    dispatch({ event_id: "E7", status: "duplicate", reason: "duplicate", ts: at(60) }),
    notRouted("E2", "load.dispatched", 2),
  ];
  const { decisions, milestones } = splitDispatches(rows);
  assert.deepEqual(decisions.map((d) => d.event_id), ["E5", "E6", "E7"]);
  assert.deepEqual(milestones.map((m) => m.id), ["milestone:E1", "milestone:E2", "milestone:E3"]);
  assert.deepEqual(milestones.map((m) => m.type), ["load.tendered", "load.dispatched", "load.delivered"]);
  assert.deepEqual(milestones.map((m) => m.label), ["Tendered", "Dispatched", "Delivered"]);
  assert.deepEqual(milestones.map((m) => m.at), [ms(0), ms(2), ms(40)]);
  assert.deepEqual(milestones[0].times, [ms(0)]);
  assert.equal(milestones[0].row.event_id, "E1");
});

test("splitDispatches parses the scenario -> prefix", () => {
  const { milestones } = splitDispatches([dispatch({ event_id: "E1", reason: "scenario -> load.tendered is not routed (7.3)" })]);
  assert.equal(milestones[0].type, "load.tendered");
  assert.equal(milestones[0].label, "Tendered");
});

test("splitDispatches collapses every GPS ping into one milestone at the first ping", () => {
  const rows = [
    notRouted("G2", "gps.ping", 20),
    notRouted("G1", "gps.ping", 10),
    notRouted("E1", "load.tendered", 0),
    notRouted("G3", "gps.ping", 30),
  ];
  const { milestones } = splitDispatches(rows);
  assert.equal(milestones.length, 2);
  const gps = milestones.find((m) => m.type === "gps.ping")!;
  assert.equal(gps.id, "milestone:G1");
  assert.equal(gps.at, ms(10));
  assert.deepEqual(gps.times, [ms(10), ms(20), ms(30)]);
});

test("splitDispatches uses fallbackAt for a missing ts", () => {
  const fallback = ms(-5);
  const { milestones } = splitDispatches([dispatch({ event_id: "E1", reason: "load.tendered is not routed (7.3)", ts: "" })], fallback);
  assert.equal(milestones[0].at, fallback);
  assert.deepEqual(milestones[0].times, [fallback]);
});

/* ------------------------------ replay ------------------------------ */

test("milestoneAt is null before, the label at/after, and counts GPS pings up to the cursor", () => {
  const { milestones } = splitDispatches([
    notRouted("E1", "load.delivered", 5),
    notRouted("G1", "gps.ping", 10),
    notRouted("G2", "gps.ping", 20),
    notRouted("G3", "gps.ping", 30),
  ]);
  const [delivered, gps] = milestones;
  assert.equal(milestoneAt(delivered, ms(4.999)), null);
  assert.equal(milestoneAt(delivered, ms(5)), "Delivered");
  assert.equal(milestoneAt(delivered, Infinity), "Delivered");
  assert.equal(milestoneAt(gps, ms(9)), null);
  assert.equal(milestoneAt(gps, ms(10)), "1 GPS ping");
  assert.equal(milestoneAt(gps, ms(25)), "2 GPS pings");
  assert.equal(milestoneAt(gps, Infinity), "3 GPS pings");
});

/* ------------------------------ trace ------------------------------ */

test("buildCaseTrace keeps only decisions as nodes and exposes milestones", () => {
  const s = snap({
    case: [{ case_id: "C1", opened_at: at(0) }],
    dispatch: [
      notRouted("E1", "load.tendered", 0),
      notRouted("E2", "load.dispatched", 2),
      notRouted("E3", "load.delivered", 40),
      routed("E4", 42),
    ],
    task: [task()],
  });
  const trace = buildCaseTrace(s, "C1");
  const intake = trace.nodes.filter((n) => n.kind === "intake");
  assert.deepEqual(intake.map((n) => n.id), ["intake:E4"]);
  assert.ok(!trace.nodes.some((n) => n.id.startsWith("intake:E1") || n.id.startsWith("intake:E2") || n.id.startsWith("intake:E3")));
  assert.deepEqual(trace.milestones.map((m) => m.label), ["Tendered", "Dispatched", "Delivered"]);
  const edge = trace.edges.find((e) => e.kind === "dispatch");
  assert.ok(edge);
  assert.equal(edge.from, "intake:E4");
  assert.equal(edge.to, "T1");
  assert.equal(edge.skill, "process_carrier_invoice");
  assert.equal(trace.first, ms(0));
  assert.ok(trace.last >= ms(44));
});

test("buildCaseTrace first/last include milestone times", () => {
  const s = snap({
    case: [{ case_id: "C1", opened_at: at(10) }],
    dispatch: [notRouted("E1", "load.tendered", -30), notRouted("E2", "load.delivered", 90)],
  });
  const trace = buildCaseTrace(s, "C1");
  assert.equal(trace.nodes.length, 0);
  assert.equal(trace.first, ms(-30));
  assert.equal(trace.last, ms(90));
});

test("buildCaseTrace falls back to the case open time for milestones without ts", () => {
  const s = snap({
    case: [{ case_id: "C1", opened_at: at(7) }],
    dispatch: [dispatch({ event_id: "E1", reason: "load.tendered is not routed (7.3)", ts: "" })],
  });
  assert.equal(buildCaseTrace(s, "C1").milestones[0].at, ms(7));
});

/* ------------------------------ layout ------------------------------ */

test("layoutTrace: milestones anchor on the Intake lane and the lane exists with only milestones", () => {
  const s = snap({
    case: [{ case_id: "C1", opened_at: at(0) }],
    dispatch: [notRouted("E1", "load.tendered", 0), notRouted("E2", "load.delivered", 5)],
  });
  const trace = buildCaseTrace(s, "C1");
  const layout = layoutTrace(trace, 0);
  assert.deepEqual(layout.lanes.map((l) => l.id), ["intake"]);
  assert.ok(layout.anchorY.has("milestone:E1"));
  assert.ok(layout.anchorY.has("milestone:E2"));
  assert.equal(layout.anchorY.get("milestone:E1"), LAYOUT.top);
});

test("layoutTrace: a long wait before a milestone adds at most MILESTONE_STEP and no gap", () => {
  const s = snap({
    case: [{ case_id: "C1", opened_at: at(0) }],
    dispatch: [notRouted("E1", "load.tendered", 0), notRouted("E2", "load.delivered", 600)],
  });
  const layout = layoutTrace(buildCaseTrace(s, "C1"), 0);
  const y1 = layout.anchorY.get("milestone:E1")!;
  const y2 = layout.anchorY.get("milestone:E2")!;
  assert.ok(y2 - y1 <= MILESTONE_STEP, `${y2 - y1}`);
  assert.equal(layout.gaps.length, 0);
});

test("layoutTrace: the replay head glides to a milestone row instead of parking on it early", () => {
  const s = snap({
    case: [{ case_id: "C1", opened_at: at(0) }],
    dispatch: [notRouted("E1", "load.tendered", 0), notRouted("E2", "load.delivered", 600)],
  });
  const layout = layoutTrace(buildCaseTrace(s, "C1"), 0);
  const y1 = layout.anchorY.get("milestone:E1")!;
  const y2 = layout.anchorY.get("milestone:E2")!;
  const mid = layout.yOf(ms(300));
  assert.ok(mid > y1 && mid < y2, `${y1} < ${mid} < ${y2}`);
  assert.ok(Math.abs(mid - (y1 + y2) / 2) < 0.01, `${mid}`);
  assert.equal(layout.yOf(ms(600)), y2);
});

test("layoutTrace: consecutive milestones are at least MILESTONE_STEP apart, even at the same instant", () => {
  const s = snap({
    case: [{ case_id: "C1", opened_at: at(0) }],
    dispatch: [notRouted("E1", "load.tendered", 0), notRouted("E2", "load.dispatched", 0), notRouted("E3", "load.delivered", 1)],
  });
  const layout = layoutTrace(buildCaseTrace(s, "C1"), 0);
  const ys = ["E1", "E2", "E3"].map((id) => layout.anchorY.get(`milestone:${id}`)!);
  for (let i = 1; i < ys.length; i++) assert.ok(ys[i] - ys[i - 1] >= MILESTONE_STEP, ys.join(","));
});

test("layoutTrace: a routed card after lifecycle milestones stays near the top with no gaps", () => {
  const s = snap({
    case: [{ case_id: "C1", opened_at: at(0) }],
    dispatch: [
      notRouted("E1", "load.tendered", 0),
      notRouted("E2", "load.dispatched", 2),
      notRouted("E3", "load.delivered", 40),
      routed("E4", 42),
    ],
    task: [task()],
  });
  const layout = layoutTrace(buildCaseTrace(s, "C1"), 0);
  const lastMilestoneY = layout.anchorY.get("milestone:E3")!;
  const cardY = layout.nodeY.get("intake:E4")!;
  assert.ok(cardY >= lastMilestoneY + MILESTONE_STEP, `${cardY} vs ${lastMilestoneY}`);
  assert.ok(cardY < 100, `routed card too low: ${cardY}`);
  assert.ok(cardY >= LAYOUT.top);
  assert.equal(layout.gaps.length, 0);
});


test("an imported case has no trace graph, keeps its ledger and reads as a payable", () => {
  const id = "HIST-LD-0915-3FA2C1";
  const s = snap({
    case: [{ case_id: id, load_id: "LD-0915-3FA2C1", title: "Mesa invoice on LD-0915-3FA2C1", status: "resolved", opened_at: at(-86_400), resolved_at: at(-3600) }],
    ledger_entry: [
      { id: "HIST-JE-LD-0915-3FA2C1-COST", case_id: id, posted_at: at(-3600), memo: "Carrier settlement: INV-1", lines: [{ account: "freight_expense", debit: 1700, credit: 0 }, { account: "accounts_payable", debit: 0, credit: 1700 }] },
    ],
  });
  const trace = buildCaseTrace(s, id);
  assert.equal(trace.nodes.length, 0);
  assert.equal(trace.edges.length, 0);
  assert.equal(trace.open, false);
  assert.deepEqual(trace.sideEffects.map((e) => [e.kind, e.label, e.amount]), [["journal", "Carrier settlement: INV-1", 1700]]);
  assert.equal(trace.first, ms(-86_400));
  const layout = layoutTrace(trace, 0);
  assert.equal(layout.lanes.length, 0);
  const cases = buildCases(s);
  assert.equal(caseKind(cases.get(id)!), "payable");
  const ar = buildCases(snap({ case: [{ case_id: `${id}-AR`, status: "resolved", opened_at: at(-10) }] }));
  assert.equal(caseKind(ar.get(`${id}-AR`)!), "receivable");
  const live = buildCases(snap({ case: [{ case_id: "CASE-X", status: "open", opened_at: at(-10) }] }));
  assert.equal(caseKind(live.get("CASE-X")!), "case");
});

test("long spans read in days, and an earlier day carries its date", () => {
  assert.equal(shortDur(90 * 60_000), "1h 30m");
  assert.equal(shortDur(47 * 3_600_000), "47h 00m");
  assert.equal(shortDur((20 * 24 + 12) * 3_600_000 + 19 * 60_000), "20d 12h");
  assert.ok(!dayClock(T0 - 60_000, T0).includes(","));
  assert.match(dayClock(T0 - 5 * 86_400_000, T0), /^\w+ \d+, /);
  assert.equal(dayClock(0, T0), "—");
});
