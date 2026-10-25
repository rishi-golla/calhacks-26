import assert from "node:assert/strict";
import test from "node:test";
import { ACTIVITY_HOLD_MS, agentActivity, toolDoing } from "../components/agents/model";
import { AGENT_IDS, type AgentId } from "./agents";
import { emptySnapshot, type Row } from "./data";
import type { AgentWorkload, TaskView } from "./ops";

const NOW = Date.parse("2026-10-25T12:00:00Z");

const task = (taskId: string): TaskView => ({ taskId } as TaskView);

function workloads(active: Partial<Record<AgentId, string[]>>): Record<AgentId, AgentWorkload> {
  const out = {} as Record<AgentId, AgentWorkload>;
  for (const id of AGENT_IDS) out[id] = { id, activeTasks: (active[id] ?? []).map(task) } as AgentWorkload;
  return out;
}

let seq = 0;
/** One tool_call audit row: a call `id` on `taskId` reaching `status` at `at`. */
function call(taskId: string, id: string, name: string, status: "started" | "completed", at: number): Row {
  return {
    id: `a${seq++}`,
    kind: "tool_call",
    task_id: taskId,
    agent: "carrier_ap",
    ts: new Date(at).toISOString(),
    reason_code: `tool_${status}`,
    summary: JSON.stringify({ tool_call_id: id, tool_name: name, status }),
  };
}

test("toolDoing reads tool names as plain-words steps", () => {
  assert.equal(toolDoing("calculate_load_margin"), "Calculating load margin");
  assert.equal(toolDoing("read_load_record"), "Reading load record");
  assert.equal(toolDoing("execute_current_skill"), "Running workflow");
  assert.equal(toolDoing("load_margin"), "Calculating load margin");
  assert.equal(toolDoing("friction_review"), "Reviewing friction");
  assert.equal(toolDoing("mystery_tool"), "Mystery tool");
});

test("an idle agent shows nothing and a live task with no open call is thinking", () => {
  const snap = emptySnapshot();
  const out = agentActivity(snap, workloads({ carrier_ap: ["t1"] }), NOW);
  assert.deepEqual(out.carrier_ap, { text: "Thinking…", more: 0, thinking: true });
  assert.equal(out.risk, undefined);
});

test("a finished step holds briefly, then gives way to thinking", () => {
  const snap = emptySnapshot();
  snap.audit_event = [call("t1", "c1", "read_load_record", "started", NOW - 3000), call("t1", "c1", "read_load_record", "completed", NOW - 1000)];
  assert.equal(agentActivity(snap, workloads({ carrier_ap: ["t1"] }), NOW).carrier_ap?.text, "Reading load record");
  assert.equal(agentActivity(snap, workloads({ carrier_ap: ["t1"] }), NOW + ACTIVITY_HOLD_MS).carrier_ap?.text, "Thinking…");
});

test("a running call beats a newer finished one still in its hold, across tasks", () => {
  const snap = emptySnapshot();
  snap.audit_event = [
    call("t1", "c1", "match_customer_deposits", "started", NOW - 5000),
    call("t2", "c2", "read_load_record", "started", NOW - 2000),
    call("t2", "c2", "read_load_record", "completed", NOW - 500),
  ];
  const out = agentActivity(snap, workloads({ carrier_ap: ["t1", "t2"] }), NOW);
  assert.deepEqual(out.carrier_ap, { text: "Matching customer deposits", more: 1, thinking: false });
});

test("calls on tasks that are no longer live are ignored", () => {
  const snap = emptySnapshot();
  snap.audit_event = [call("gone", "c9", "audit_accessorials", "started", NOW - 1000)];
  assert.equal(agentActivity(snap, workloads({ carrier_ap: ["t1"] }), NOW).carrier_ap?.text, "Thinking…");
});
