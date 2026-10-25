import assert from "node:assert/strict";
import test from "node:test";
import { AGENT_META, isAgentId, type AgentId } from "./agents";
import { TABLES, type Row, type Snapshot } from "./data";
import { DemoSim } from "./demo-sim";
import { buildWorkloads } from "./ops";
import { parseChecks, parseWorkings, recheckWorking } from "./workings";

const NOW = Date.parse("2026-10-24T12:00:00.000Z");
const make = (seed = 1337, extra: Partial<ConstructorParameters<typeof DemoSim>[0]> = {}) =>
  new DemoSim({ seed, historyMinutes: 30, ratePerMinute: 8, now: NOW, ...extra });
const TERMINAL = ["completed", "failed", "canceled", "rejected"];

/** Advance a sim through a few minutes so late-stage rows (reviews, retries) exist. */
function settled(seed: number): Snapshot {
  const sim = make(seed);
  for (let i = 1; i <= 120; i++) sim.tick(NOW + i * 5_000);
  return sim.snapshot();
}
const SEEDS = [1337, 7, 42];

test("snapshot has every table and is populated", () => {
  const s = make().snapshot();
  for (const name of TABLES) assert.ok(Array.isArray(s[name]), `missing ${name}`);
  for (const name of ["case", "task", "agent", "ledger_entry", "company", "audit_event", "dispatch", "cash_forecast"] as const)
    assert.ok(s[name].length > 0, `${name} empty`);
  assert.equal(s.agent.length, 9);
});

test("every task.agent is a known AgentId", () => {
  for (const seed of SEEDS)
    for (const t of settled(seed).task) assert.ok(isAgentId(t.agent), `unknown agent ${String(t.agent)}`);
});

test("delegation edges respect allowlist, depth <= 3 and no repeated ancestors", () => {
  for (const seed of SEEDS) {
    const s = settled(seed);
    const tasks = new Map(s.task.map((t) => [String(t.task_id), t]));
    assert.ok(s.delegation_edge.length > 0);
    for (const e of s.delegation_edge) {
      const from = e.from_agent as AgentId;
      const to = e.to_agent as AgentId;
      assert.ok(AGENT_META[from].delegatesTo.includes(to), `${from} -> ${to} not allowed`);
      assert.ok(Number(e.depth) <= 3, `depth ${String(e.depth)}`);
      // Walk up: the chain of ancestor agents must not contain the new agent.
      const chain: string[] = [];
      let cur: Row | undefined = tasks.get(String(e.parent_task_id));
      while (cur) {
        chain.push(String(cur.agent));
        cur = cur.parent_task_id ? tasks.get(String(cur.parent_task_id)) : undefined;
      }
      assert.ok(!chain.includes(to), `${to} repeats in ancestors [${chain}] (edge ${String(e.id)})`);
    }
  }
});

// KNOWN BUG (reported to the UI owner, modules under test not patched): a failed child is retried
// via startTask({...t, taskId}) in completeTask(), which keeps parent_task_id but never records a
// delegation_edge. Remove `todo` once the sim records an edge for retries.
test("child tasks always have a delegation_edge", {}, () => {
  for (const seed of SEEDS) {
    const s = settled(seed);
    const edgeTasks = new Set(s.delegation_edge.map((e) => String(e.task_id)));
    for (const t of s.task)
      if (t.parent_task_id) assert.ok(edgeTasks.has(String(t.task_id)), `task ${String(t.task_id)} has parent but no edge`);
  }
});

test("every non-retry child task has a delegation_edge", () => {
  for (const seed of SEEDS) {
    const s = settled(seed);
    const edgeTasks = new Set(s.delegation_edge.map((e) => String(e.task_id)));
    const retries = new Set(s.audit_event.filter((e) => e.reason_code === "TASK_RETRY").map((e) => String(e.task_id)));
    for (const t of s.task)
      if (t.parent_task_id && !retries.has(String(t.task_id)))
        assert.ok(edgeTasks.has(String(t.task_id)), `task ${String(t.task_id)} has parent but no edge`);
  }
});

// KNOWN BUG: when a delegated child fails, the parent resumes (failed is terminal) and closes the
// case while the 20-45 s retry is still scheduled; the retry then starts on an already-resolved case
// (e.g. seed 7: TASK-5158-4 controller working under resolved CASE-5158).
test("a resolved case has no non-terminal tasks except validator review", {}, () => {
  for (const seed of SEEDS) {
    const s = settled(seed);
    const resolved = new Set(s.case.filter((c) => c.status === "resolved").map((c) => String(c.case_id)));
    assert.ok(resolved.size > 0);
    for (const t of s.task) {
      if (!resolved.has(String(t.case_id)) || TERMINAL.includes(String(t.a2a_state))) continue;
      assert.equal(t.agent, "validator", `open ${String(t.agent)} task ${String(t.task_id)} on resolved case`);
      assert.equal(t.skill, "review_output");
    }
  }
});

test("validator review is created at most once per case", () => {
  for (const seed of SEEDS) {
    const s = settled(seed);
    const outbox = new Map<string, number>();
    for (const e of s.audit_event)
      if (e.reason_code === "REVIEW_OUTBOX") outbox.set(String(e.case_id), (outbox.get(String(e.case_id)) ?? 0) + 1);
    assert.ok(outbox.size > 0);
    for (const [caseId, n] of outbox) assert.equal(n, 1, `${caseId} reviewed ${n}x`);
    // Task-level guard: review tasks per case are bounded (1, plus at most a retry after failure).
    const perCase = new Map<string, number>();
    for (const t of s.task)
      if (t.skill === "review_output") perCase.set(String(t.case_id), (perCase.get(String(t.case_id)) ?? 0) + 1);
    for (const [caseId, n] of perCase) assert.ok(n <= 3, `${caseId} has ${n} review tasks`);
  }
});

test("tick: later wall time adds rows, same time is a no-op", () => {
  const sim = make();
  const before = sim.snapshot();
  assert.equal(sim.tick(NOW), false);
  assert.equal(sim.snapshot(), before);
  assert.equal(sim.tick(NOW + 120_000), true);
  const after = sim.snapshot();
  assert.ok(after.audit_event.length > before.audit_event.length);
  assert.ok(after.task.length >= before.task.length);
  assert.equal(sim.tick(NOW + 120_000), false);
});

test("setRate(0) stops new cases", () => {
  const sim = make();
  assert.equal(sim.ratePerMinute, 8);
  sim.setRate(0);
  assert.equal(sim.ratePerMinute, 0);
  const cases = sim.snapshot().case.length;
  for (let i = 1; i <= 30; i++) sim.tick(NOW + i * 10_000);
  assert.equal(sim.snapshot().case.length, cases);
  sim.setRate(-5);
  assert.equal(sim.ratePerMinute, 0);
  sim.setRate(30);
  for (let i = 31; i <= 60; i++) sim.tick(NOW + i * 10_000);
  assert.ok(sim.snapshot().case.length > cases);
});

test("inject('bank_change') opens a -BANK case dispatched to risk", () => {
  const sim = make();
  sim.setRate(0);
  const caseId = sim.inject("bank_change");
  assert.ok(caseId.endsWith("-BANK"), caseId);
  assert.equal(sim.tick(NOW + 5_000), true);
  const s = sim.snapshot();
  const row = s.case.find((c) => c.case_id === caseId)!;
  assert.equal(row.priority, "P0");
  assert.equal(row.scenario, "bank_change");
  const dispatch = s.dispatch.find((d) => d.case_id === caseId)!;
  assert.ok(dispatch);
  assert.equal(dispatch.agent, "risk");
  assert.equal(dispatch.skill, "screen_bank_change");
});

test("same seed and now yield identical case ids; different seeds differ", () => {
  const ids = (sim: DemoSim) => sim.snapshot().case.map((c) => c.case_id);
  assert.deepEqual(ids(make(5)), ids(make(5)));
  assert.deepEqual(make(5).snapshot().task.map((t) => t.task_id), make(5).snapshot().task.map((t) => t.task_id));
  const a = make(5).snapshot().case.map((c) => c.scenario);
  const b = make(6).snapshot().case.map((c) => c.scenario);
  assert.notDeepEqual(a, b);
});

// Generous bounds: the sim builds in ~100 ms alone, but the suite runs in
// parallel with other test files and dev tooling on a shared machine.
test("performance: construction < 3 s, 200 ticks < 500 ms", () => {
  let t0 = performance.now();
  const sim = make();
  const build = performance.now() - t0;
  assert.ok(build < 3_000, `construction took ${build.toFixed(0)} ms`);
  t0 = performance.now();
  for (let i = 1; i <= 200; i++) sim.tick(NOW + i * 1_000);
  const ticks = performance.now() - t0;
  assert.ok(ticks < 500, `200 ticks took ${ticks.toFixed(0)} ms`);
});

test("carrier_ap and risk artifacts show their work, and the work recomputes", () => {
  for (const seed of SEEDS) {
    const s = settled(seed);
    let shown = 0;
    for (const t of s.task) {
      if (!["carrier_ap", "risk"].includes(String(t.agent)) || !t.artifact_json) continue;
      const artifact = JSON.parse(String(t.artifact_json));
      if (!artifact.findings) continue;
      const workings = parseWorkings(artifact.findings.workings);
      assert.ok(workings.length > 0, `${String(t.task_id)} has no workings`);
      for (const w of workings) {
        assert.notEqual(recheckWorking(w).status, "mismatch", `${String(t.task_id)} ${w.label}`);
        assert.ok(w.evidence_refs.length > 0);
      }
      shown++;
    }
    assert.ok(shown > 0, `seed ${seed} produced no carrier_ap/risk results`);
  }
});

test("simulated review checks carry severity, basis and work", () => {
  for (const seed of SEEDS) {
    const reviews = settled(seed).review;
    assert.ok(reviews.length > 0);
    for (const r of reviews) {
      const checks = parseChecks(r.checks_json);
      assert.ok(checks.some((c) => c.severity === "advisory"));
      assert.ok(checks.every((c) => c.basis));
      for (const c of checks) if (c.work) assert.equal(recheckWorking(c.work).status, "match", c.name);
    }
  }
});

test("agent map: after history fast-forward some agent is working, backed by a busy heartbeat", () => {
  for (const seed of SEEDS) {
    const sim = make(seed);
    for (let i = 1; i <= 24; i++) sim.tick(NOW + i * 5_000);
    const now = NOW + 24 * 5_000;
    const s = sim.snapshot();
    const workloads = buildWorkloads(s, now);
    const working = Object.values(workloads).filter((w) => w.status === "working");
    assert.ok(working.length > 0, `seed ${seed}: no working agent`);
    for (const w of working) {
      assert.equal(s.agent.find((r) => r.name === w.id)?.status, "busy", `${w.id} working without busy heartbeat`);
      assert.ok(w.active >= 1);
    }
  }
});
