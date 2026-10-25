import assert from "node:assert/strict";
import test from "node:test";
import {
  AGENT_IDS,
  AGENT_META,
  NODE_META,
  agentColor,
  agentLabel,
  isAgentId,
  skillLabel,
} from "./agents";
import { AGENTS } from "./data";

test("every delegatesTo entry is a valid AgentId", () => {
  for (const id of AGENT_IDS)
    for (const target of AGENT_META[id].delegatesTo)
      assert.ok(isAgentId(target), `${id} delegates to unknown ${target}`);
});

test("no agent delegates to itself", () => {
  for (const id of AGENT_IDS)
    assert.ok(!AGENT_META[id].delegatesTo.includes(id), `${id} self-delegates`);
});

test("roster matches the nine service ids in data.ts AGENTS", () => {
  assert.equal(AGENT_IDS.length, 9);
  assert.deepEqual([...AGENT_IDS].sort(), AGENTS.map((a) => a.id as string).sort());
  for (const id of AGENT_IDS) assert.equal(AGENT_META[id].id, id);
});

test("agentLabel / agentColor fall back sensibly", () => {
  assert.equal(agentLabel("risk"), AGENT_META.risk.name);
  assert.equal(agentLabel("intake"), NODE_META.intake.name);
  assert.equal(agentLabel("human"), NODE_META.human.name);
  assert.equal(agentLabel("mystery"), "mystery");
  assert.equal(agentLabel(undefined), "unknown");
  assert.equal(agentLabel(null), "unknown");
  assert.equal(agentColor("cfo"), AGENT_META.cfo.color);
  assert.equal(agentColor("intake"), NODE_META.intake.color);
  assert.equal(agentColor("human"), NODE_META.human.color);
  assert.equal(agentColor("mystery"), "#9AA3B2");
  assert.equal(agentColor(undefined), "#9AA3B2");
});

test("isAgentId rejects non-agents and inherited keys", () => {
  assert.equal(isAgentId("intake"), false);
  assert.equal(isAgentId(42), false);
  assert.equal(isAgentId("risk"), true);
});

test("skillLabel humanizes unknown skills", () => {
  assert.equal(skillLabel("place_call"), "Place call");
  assert.equal(skillLabel("do_a_thing"), "do a thing");
  assert.equal(skillLabel(undefined), "task");
});
