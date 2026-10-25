/**
 * Validator lessons: what Settlement Quality Review taught each agent, where
 * it came from, and how often the agent has recalled it since.
 *
 * - `agent_memory` rows with `source = "validator"` are lessons. `source_ref`
 *   is the review that produced one (live), or the validator task (sim).
 * - `instruction_version` rows authored by the validator are the agent's
 *   versioned instructions; their diffs add `Learned:` lines.
 * - A task's `memory_ids` lists the lessons it loaded, so a lesson's recall
 *   count is the number of tasks naming it.
 *
 * Learning outlives "Start fresh": pass the unscoped snapshot, so a lesson
 * keeps its source case and every recall since it was written.
 */
import { isAgentId, type AgentId } from "./agents";
import { json, str, timestamp, type Row, type Snapshot } from "./data";

export type Lesson = {
  id: string;
  agent: AgentId;
  text: string;
  /** Scope as display chips, e.g. ["C-77", "Reno, NV → Stockton, CA", "detention", "process carrier invoice"]. */
  scope: string[];
  at: number;
  /** The case whose review taught it, when the snapshot still has that review or task. */
  caseId: string;
  active: boolean;
  /** Tasks in the snapshot that loaded this lesson. */
  recalled: number;
  lastRecalledAt: number;
};

export type InstructionVersion = {
  id: string;
  agent: AgentId;
  version: number;
  /** "validator", or "human" for a person's edit or rollback. */
  author: string;
  at: number;
  /** Every `Learned:` line in effect at this version, oldest first. */
  learned: string[];
  /** Set when this version rolled back to an earlier one. */
  revertsVersion: number;
  caseId: string;
};

export type LessonIndex = {
  /** Active lessons per agent, newest first. */
  byAgent: Map<AgentId, Lesson[]>;
  /** Every active lesson, newest first. */
  all: Lesson[];
  /** Lessons no longer active (superseded or reverted by a human). */
  retired: number;
  /** The current instruction version per agent, any author, with its effective learned lines. */
  instructions: Map<AgentId, InstructionVersion>;
  /** Total recalls across all active lessons. */
  recalls: number;
};

/** A string-array column: an array from the WS client, or JSON / a single id from the SQL fallback. */
export function idList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((v) => str(v, "")).filter(Boolean);
  if (typeof value !== "string" || !value) return [];
  const parsed = json<unknown>(value, null);
  if (Array.isArray(parsed)) return parsed.map((v) => str(v, "")).filter(Boolean);
  return value.startsWith("[") ? [] : [value];
}

export const isLessonRow = (row: Row) => str(row.source, "") === "validator" && isAgentId(row.agent);

const SCOPE_ORDER = ["carrier_id", "lane", "claim_type", "skill"] as const;

export function scopeChips(raw: unknown): string[] {
  const scope = json<Record<string, unknown>>(raw, {});
  if (!scope || typeof scope !== "object" || Array.isArray(scope)) return [];
  const out: string[] = [];
  for (const key of SCOPE_ORDER) {
    const v = str(scope[key], "");
    if (!v) continue;
    out.push(key === "lane" ? v.replace(/\s*->\s*/, " → ") : key === "carrier_id" ? v : v.replaceAll("_", " "));
  }
  return out;
}

/** `Learned: …` lines a unified diff adds (`+`) or removes (`-`). */
function diffLearned(diff: unknown, sign: "+" | "-"): string[] {
  return str(diff, "")
    .split("\n")
    .filter((l) => l.startsWith(sign) && !l.startsWith(sign.repeat(3)))
    .map((l) => l.slice(1).trim())
    .filter((l) => /^learned:/i.test(l))
    .map((l) => l.replace(/^learned:\s*/i, ""));
}

/** `+Learned: …` lines of a unified diff. */
export const learnedLines = (diff: unknown) => diffLearned(diff, "+");

/**
 * The `Learned:` lines in effect at each version of one agent's instruction
 * history. Versions are incremental diffs, so this replays the chain the way
 * `common.learning.reconstruct_instructions` does: a revert restores the
 * target version's state, an empty compensating version restores the
 * baseline, and any other version applies its diff to the one before.
 * The checked-in baseline carries no learned lines.
 */
export function effectiveLearned(history: Row[]): Map<number, string[]> {
  const rows = new Map<number, Row>();
  for (const r of history) rows.set(Number(r.version) || 0, r);
  const memo = new Map<number, string[]>([[0, []]]);
  const at = (version: number, depth = 0): string[] => {
    const hit = memo.get(version);
    if (hit) return hit;
    const row = rows.get(version);
    let lines: string[];
    if (!row || depth > 512) lines = [];
    else {
      const revert = Number(row.reverts_version) || 0;
      if (revert && revert < version) lines = at(revert, depth + 1);
      else if (!str(row.diff, "")) lines = [];
      else {
        const removed = new Set(diffLearned(row.diff, "-"));
        lines = [...at(version - 1, depth + 1).filter((l) => !removed.has(l)), ...diffLearned(row.diff, "+")];
      }
    }
    memo.set(version, lines);
    return lines;
  };
  for (const v of [...rows.keys()].sort((a, b) => a - b)) at(v);
  return memo;
}

/**
 * The case a lesson came from. `source_ref` names the review (live: the
 * review id; sim: the validator task, whose review is `REV-<task>`), or a task.
 */
export function sourceCase(ref: string, reviews: Map<string, Row>, tasks: Map<string, Row>): string {
  if (!ref) return "";
  const review = reviews.get(ref) ?? reviews.get(`REV-${ref}`);
  if (review) return str(review.case_id, "");
  const task = tasks.get(ref);
  return task ? str(task.case_id, "") : "";
}

const byId = (rows: Row[], key: string) => {
  const out = new Map<string, Row>();
  for (const r of rows) out.set(str(r[key], ""), r);
  return out;
};

/** The tables lessons are built from; pass only these so callers can memoize on them. */
export type LessonTables = Pick<Snapshot, "task" | "agent_memory" | "review" | "instruction_version">;

export function lessonIndex(snapshot: LessonTables): LessonIndex {
  const reviews = byId(snapshot.review, "id");
  const tasks = byId(snapshot.task, "task_id");
  const recalls = new Map<string, { n: number; last: number }>();
  for (const t of snapshot.task)
    for (const id of idList(t.memory_ids)) {
      const r = recalls.get(id) ?? { n: 0, last: 0 };
      r.n += 1;
      r.last = Math.max(r.last, timestamp(t.updated_at));
      recalls.set(id, r);
    }

  const all: Lesson[] = [];
  let retired = 0;
  for (const row of snapshot.agent_memory) {
    if (!isLessonRow(row)) continue;
    if (row.active === false) {
      retired += 1;
      continue;
    }
    const id = str(row.id, "");
    const r = recalls.get(id);
    all.push({
      id,
      agent: row.agent as AgentId,
      text: str(row.text, ""),
      scope: scopeChips(row.scope),
      at: timestamp(row.created_at),
      caseId: sourceCase(str(row.source_ref, ""), reviews, tasks),
      active: true,
      recalled: r?.n ?? 0,
      lastRecalledAt: r?.last ?? 0,
    });
  }
  all.sort((a, b) => b.at - a.at);
  const byAgent = new Map<AgentId, Lesson[]>();
  for (const l of all) {
    const list = byAgent.get(l.agent);
    if (list) list.push(l);
    else byAgent.set(l.agent, [l]);
  }

  // One version chain per agent, whoever wrote each version; the latest is current.
  const histories = new Map<AgentId, Row[]>();
  for (const row of snapshot.instruction_version) {
    if (!isAgentId(row.agent)) continue;
    const list = histories.get(row.agent);
    if (list) list.push(row);
    else histories.set(row.agent, [row]);
  }
  const instructions = new Map<AgentId, InstructionVersion>();
  for (const [agent, rows] of histories) {
    const latest = rows.reduce((a, b) => ((Number(b.version) || 0) > (Number(a.version) || 0) ? b : a));
    const version = Number(latest.version) || 0;
    instructions.set(agent, {
      id: str(latest.id, ""),
      agent,
      version,
      author: str(latest.author, ""),
      at: timestamp(latest.created_at),
      learned: effectiveLearned(rows).get(version) ?? [],
      revertsVersion: Number(latest.reverts_version) || 0,
      caseId: sourceCase(str(latest.source_ref, ""), reviews, tasks),
    });
  }

  return { byAgent, all, retired, instructions, recalls: all.reduce((s, l) => s + l.recalled, 0) };
}

/* ------------------------------ one case ------------------------------ */

/** A memory row as the case trace shows it: a Validator lesson, or a note a person wrote. */
export type MemoryNote = {
  id: string;
  agent: string;
  text: string;
  scope: string[];
  byValidator: boolean;
  active: boolean;
  /** The case whose review taught it ("" when unknown). */
  sourceCase: string;
  sourceRef: string;
  at: number;
};

export const memoryNote = (row: Row): MemoryNote => ({
  id: str(row.id, ""),
  agent: str(row.agent, ""),
  text: str(row.text, ""),
  scope: scopeChips(row.scope),
  byValidator: str(row.source, "") === "validator",
  active: row.active !== false,
  sourceCase: str(row.source_case, ""),
  sourceRef: str(row.source_ref, ""),
  at: timestamp(row.created_at),
});

/**
 * Memory and instruction rows for one case's trace: every memory its tasks
 * loaded, plus the lessons and instruction versions its own reviews wrote.
 * Memory rows are stamped with `source_case` (looked up in the whole
 * snapshot, since a lesson usually comes from an earlier case).
 */
export function caseMemory(snapshot: LessonTables, caseTasks: Row[], caseReviews: Row[]): Pick<Snapshot, "agent_memory" | "instruction_version"> {
  const used = new Set(caseTasks.flatMap((t) => idList(t.memory_ids)));
  // Live lessons point at their review; simulated ones at the Validator task.
  const refs = new Set([...caseReviews.map((r) => str(r.id, "")), ...caseTasks.map((t) => str(t.task_id, ""))]);
  refs.delete("");
  const memory = snapshot.agent_memory.filter((m) => used.has(str(m.id, "")) || refs.has(str(m.source_ref, "")));
  if (!memory.length && !snapshot.instruction_version.some((v) => refs.has(str(v.source_ref, "")))) return { agent_memory: [], instruction_version: [] };
  const lookup = (ref: string) => {
    if (!ref) return "";
    const review = snapshot.review.find((r) => r.id === ref || r.id === `REV-${ref}`);
    if (review) return str(review.case_id, "");
    return str(snapshot.task.find((t) => t.task_id === ref)?.case_id, "");
  };
  return {
    agent_memory: memory.map((m) => ({ ...m, source_case: lookup(str(m.source_ref, "")) })),
    instruction_version: snapshot.instruction_version.filter((v) => refs.has(str(v.source_ref, ""))),
  };
}

/**
 * The refs a Validator task's lessons point at: the review it wrote (live:
 * `findings.review_id` in its artifact; sim: `REV-<task>`) or the task itself.
 */
export function reviewRefs(taskId: string, artifact: Record<string, unknown> | null | undefined): Set<string> {
  const findings = artifact?.findings as Record<string, unknown> | undefined;
  const out = new Set([taskId, `REV-${taskId}`, str(findings?.review_id, "")]);
  out.delete("");
  return out;
}
