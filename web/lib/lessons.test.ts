import assert from "node:assert/strict";
import test from "node:test";
import { emptySnapshot, type Snapshot } from "./data";
import { effectiveLearned, idList, learnedLines, lessonIndex, scopeChips } from "./lessons";
import { diffSnapshots } from "./ops";

const T0 = "2026-10-25T07:55:49.216Z";
const T1 = "2026-10-25T08:41:27.770Z";

function snap(): Snapshot {
  const s = emptySnapshot();
  s.review = [{ id: "REV-A", case_id: "CASE-LD-1025-6CA28255", target_task_id: "T-AP", verdict: "issue", ts: T0 }];
  s.agent_memory = [
    // Live shape: source_ref is the review id.
    { id: "M1", agent: "carrier_ap", scope: '{"carrier_id":"C-77","lane":"Reno, NV -> Stockton, CA","skill":"process_carrier_invoice"}', text: "Confirm remit-to changes.", source: "validator", source_ref: "REV-A", version: 1, active: true, created_at: T0 },
    // Sim shape: source_ref is the validator task; its review is REV-<task>.
    { id: "M2", agent: "cfo", scope: { claim_type: "detention" }, text: "Reconcile to the subledger.", source: "validator", source_ref: "A", version: 1, active: true, created_at: T1 },
    { id: "M3", agent: "carrier_ap", scope: "{}", text: "Retired.", source: "validator", source_ref: "", version: 1, active: false, created_at: T0 },
    { id: "M4", agent: "carrier_ap", scope: "{}", text: "Written by a human.", source: "human", source_ref: "", version: 1, active: true, created_at: T0 },
  ];
  s.instruction_version = [
    { id: "carrier_ap:4", agent: "carrier_ap", version: 4, diff: "+Learned: old", author: "validator", source_ref: "REV-A", active: false, created_at: T0 },
    { id: "carrier_ap:5", agent: "carrier_ap", version: 5, diff: "--- a/instructions.md\n+++ b/instructions.md\n@@ -13,0 +14,2 @@\n+\n+Learned: Do not approve settlement while a remit-to change is unverified.\n", author: "validator", source_ref: "REV-A", active: true, created_at: T0 },
  ];
  s.task = [
    { task_id: "T1", case_id: "C1", agent: "carrier_ap", memory_ids: ["M1"], updated_at: T1 },
    { task_id: "T2", case_id: "C2", agent: "carrier_ap", memory_ids: '["M1"]', updated_at: T1 },
    { task_id: "T3", case_id: "C3", agent: "cfo", memory_ids: [], updated_at: T1 },
  ];
  return s;
}

test("idList reads arrays, JSON strings and single ids", () => {
  assert.deepEqual(idList(["a", "b"]), ["a", "b"]);
  assert.deepEqual(idList('["a"]'), ["a"]);
  assert.deepEqual(idList("a"), ["a"]);
  assert.deepEqual(idList(""), []);
  assert.deepEqual(idList(undefined), []);
});

test("scopeChips orders carrier, lane, claim type, skill", () => {
  assert.deepEqual(scopeChips('{"skill":"process_carrier_invoice","lane":"A -> B","carrier_id":"C-1","claim_type":"detention"}'), ["C-1", "A → B", "detention", "process carrier invoice"]);
  assert.deepEqual(scopeChips("not json"), []);
});

test("learnedLines keeps only added Learned: lines", () => {
  assert.deepEqual(learnedLines("+++ b/x\n+Learned: one\n-Learned: gone\n+other\n"), ["one"]);
});

test("lessonIndex groups active validator lessons with their source case and recall count", () => {
  const idx = lessonIndex(snap());
  assert.deepEqual(idx.all.map((l) => l.id), ["M2", "M1"]);
  assert.equal(idx.retired, 1);
  const ap = idx.byAgent.get("carrier_ap")!;
  assert.equal(ap.length, 1);
  assert.equal(ap[0].caseId, "CASE-LD-1025-6CA28255");
  assert.equal(ap[0].recalled, 2);
  assert.deepEqual(ap[0].scope, ["C-77", "Reno, NV → Stockton, CA", "process carrier invoice"]);
  // Sim: source_ref "A" resolves through review "REV-A".
  assert.equal(idx.byAgent.get("cfo")![0].caseId, "CASE-LD-1025-6CA28255");
  assert.equal(idx.byAgent.get("cfo")![0].recalled, 0);
  const instr = idx.instructions.get("carrier_ap")!;
  assert.equal(instr.version, 5);
  // v4's learned line is still in effect at v5: versions are incremental.
  assert.deepEqual(instr.learned, ["old", "Do not approve settlement while a remit-to change is unverified."]);
  assert.equal(idx.recalls, 2);
});

test("diffSnapshots reports new lessons, instruction versions and recalls", () => {
  const prev = snap();
  prev.agent_memory = prev.agent_memory.slice(0, 1);
  prev.instruction_version = prev.instruction_version.slice(0, 1);
  prev.task = prev.task.map((t) => (t.task_id === "T3" ? t : { ...t }));
  const next = snap();
  // T3 recalls a Validator lesson; T4 loaded only a human-written memory, which is not a recall.
  next.task = [...prev.task.slice(0, 2), { ...prev.task[2], memory_ids: ["M2"] }, { task_id: "T4", case_id: "C4", agent: "carrier_ap", memory_ids: ["M4"], updated_at: T1 }];
  const events = diffSnapshots(prev, next);
  const lesson = events.filter((e) => e.type === "lesson");
  assert.equal(lesson.length, 1);
  assert.deepEqual(lesson[0], { type: "lesson", at: Date.parse(T1), caseId: "CASE-LD-1025-6CA28255", agent: "cfo", lessonId: "M2", text: "Reconcile to the subledger." });
  const instr = events.filter((e) => e.type === "instruction");
  assert.equal(instr.length, 1);
  assert.equal(instr[0].type === "instruction" && instr[0].version, 5);
  const recalls = events.filter((e) => e.type === "lesson_recalled");
  assert.equal(recalls.length, 1);
  assert.equal(recalls[0].type === "lesson_recalled" && recalls[0].taskId, "T3");
});

test("effectiveLearned replays the version chain, reverts included", () => {
  const add = (line: string) => `--- a/instructions.md\n+++ b/instructions.md\n@@ -13,0 +14,1 @@\n+Learned: ${line}\n`;
  const history = [
    { agent: "carrier_ap", version: 1, diff: add("Check receipt"), author: "validator", reverts_version: 0 },
    { agent: "carrier_ap", version: 2, diff: add("Verify totals"), author: "validator", reverts_version: 0 },
    // A person rolls back to v1: the revert row carries v1's diff and points at it.
    { agent: "carrier_ap", version: 3, diff: add("Check receipt"), author: "human", reverts_version: 1 },
    { agent: "carrier_ap", version: 4, diff: "@@ -14,1 +14,1 @@\n-Learned: Check receipt\n+Learned: Check receipt and rate con\n", author: "validator", reverts_version: 0 },
    // An empty compensating version restores the baseline.
    { agent: "carrier_ap", version: 5, diff: "", author: "human", reverts_version: 0 },
  ];
  const at = effectiveLearned(history);
  assert.deepEqual(at.get(2), ["Check receipt", "Verify totals"]);
  assert.deepEqual(at.get(3), ["Check receipt"]);
  assert.deepEqual(at.get(4), ["Check receipt and rate con"]);
  assert.deepEqual(at.get(5), []);
});

test("lessonIndex shows the current instruction version whoever wrote it", () => {
  const s = snap();
  s.instruction_version = [
    ...s.instruction_version.map((r) => ({ ...r, active: false })),
    { id: "carrier_ap:6", agent: "carrier_ap", version: 6, diff: "+Learned: old", author: "human", source_ref: "carrier_ap:3", active: true, created_at: T1, reverts_version: 4 },
  ];
  const instr = lessonIndex(s).instructions.get("carrier_ap")!;
  assert.equal(instr.version, 6);
  assert.equal(instr.author, "human");
  assert.equal(instr.revertsVersion, 4);
  // v4's state: its own diff on an empty v3 (absent from this history).
  assert.deepEqual(instr.learned, ["old"]);
});
