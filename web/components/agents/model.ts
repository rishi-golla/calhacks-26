/**
 * Selectors local to the agent universe: which edges carry traffic, what a
 * case's path looks like, and how a batch of `LiveEvent`s becomes packets.
 * Pure functions over the snapshot.
 */
import { isAgentId, type AgentId, type NodeId } from "@/lib/agents";
import { str, timestamp, type Snapshot } from "@/lib/data";
import type { CaseView, LiveEvent } from "@/lib/ops";
import { edgeKey } from "./layout";
import type { PacketSpec } from "./packet-engine";

/* ------------------------------ case kinds ------------------------------ */

export type CaseKind = "payable" | "receivable";
export type KindTab = "all" | "payables" | "receivables" | "exceptions";

const RECEIVABLE = new Set(["shipper_short_pay", "overdue_collections", "customer_payment"]);

export function caseKind(c: CaseView): CaseKind {
  if (c.scenario && c.scenario !== "—") return RECEIVABLE.has(c.scenario) ? "receivable" : "payable";
  if (/-AR$/.test(c.caseId)) return "receivable";
  return c.agents[0] === "shipper_ar" ? "receivable" : "payable";
}

export function matchesTab(c: CaseView, tab: KindTab): boolean {
  if (tab === "all") return true;
  if (tab === "exceptions") return c.attention.length > 0 && c.status !== "resolved";
  return caseKind(c) === (tab === "payables" ? "payable" : "receivable");
}

export const scenarioLabel = (key: string) => (key && key !== "—" ? key.replaceAll("_", " ") : "unclassified");

/** Case id without the "CASE-" prefix every id shares. */
export const shortCaseId = (caseId: string) => caseId.replace(/^CASE-/, "");

/**
 * A case title without the trailing load id the case id already carries:
 * "Cobalt Components Ltd payment on LD-1025-247C3792" → "Cobalt Components Ltd payment".
 */
export function caseTitle(c: Pick<CaseView, "title" | "loadId" | "caseId">): string {
  const load = c.loadId;
  if (!load || !c.caseId.includes(load)) return c.title;
  const i = c.title.lastIndexOf(load);
  if (i < 0) return c.title;
  const short = (c.title.slice(0, i).replace(/\s+(on|for|·|-|—)\s*$/i, "") + c.title.slice(i + load.length)).trim();
  return short || c.title;
}

/* ------------------------------ edge traffic ------------------------------ */

const isReview = (row: Record<string, unknown>) => row.agent === "validator" && !row.parent_task_id;

export type EdgeStat = { key: string; from: NodeId; to: NodeId; total: number; recent: number; lastAt: number };

const WINDOW_MS = 5 * 60_000;

export function edgeTraffic(snapshot: Snapshot, cases: Map<string, CaseView>, now: number): Map<string, EdgeStat> {
  const out = new Map<string, EdgeStat>();
  const bump = (from: NodeId, to: NodeId, at: number) => {
    if (from === to) return;
    const key = edgeKey(from, to);
    let s = out.get(key);
    if (!s) {
      s = { key, from, to, total: 0, recent: 0, lastAt: 0 };
      out.set(key, s);
    }
    s.total += 1;
    if (now - at <= WINDOW_MS) s.recent += 1;
    if (at > s.lastAt) s.lastAt = at;
  };
  for (const d of snapshot.dispatch) if (isAgentId(d.agent)) bump("intake", d.agent, timestamp(d.ts));
  for (const e of snapshot.delegation_edge)
    if (isAgentId(e.from_agent) && isAgentId(e.to_agent)) bump(e.from_agent, e.to_agent, timestamp(e.ts));
  for (const t of snapshot.task)
    if (isReview(t)) {
      const root = cases.get(str(t.case_id, ""))?.agents[0];
      if (root && root !== "validator") bump(root, "validator", timestamp(t.started_at));
    }
  for (const a of snapshot.approval) {
    const by = isAgentId(a.requested_by) ? a.requested_by : "controller";
    bump(by, "human", timestamp(a.requested_at));
    if (a.decision && a.decision !== "pending") bump("human", by, timestamp(a.decided_at));
  }
  return out;
}

/** Stroke width for an edge: grows with the log of handoffs in the last five minutes. */
export const edgeWidth = (recent: number) => Math.min(6, 0.8 + 1.8 * Math.log10(1 + recent));

/* ------------------------------- case path ------------------------------- */

export type CasePath = { nodes: Set<NodeId>; edges: Set<string>; order: Map<NodeId, number> };

export function casePath(snapshot: Snapshot, c: CaseView): CasePath {
  const nodes = new Set<NodeId>(c.agents);
  const edges = new Set<string>();
  const order = new Map<NodeId, number>();
  c.agents.forEach((a, i) => order.set(a, i + 1));
  for (const d of snapshot.dispatch)
    if (d.case_id === c.caseId && isAgentId(d.agent)) {
      nodes.add("intake");
      edges.add(edgeKey("intake", d.agent));
    }
  for (const e of snapshot.delegation_edge)
    if (e.case_id === c.caseId && isAgentId(e.from_agent) && isAgentId(e.to_agent)) edges.add(edgeKey(e.from_agent, e.to_agent));
  if (c.agents.includes("validator") && c.agents[0] && c.agents[0] !== "validator") edges.add(edgeKey(c.agents[0], "validator"));
  for (const a of snapshot.approval)
    if (a.case_id === c.caseId) {
      const by = isAgentId(a.requested_by) ? a.requested_by : "controller";
      nodes.add("human");
      edges.add(edgeKey(by, "human"));
      if (a.decision && a.decision !== "pending") edges.add(edgeKey("human", by));
    }
  return { nodes, edges, order };
}

/* ---------------------------- events → packets ---------------------------- */

/** Events older than this are history replays (e.g. a source switch), not live traffic. */
const STALE_MS = 20_000;

export function packetsFor(events: LiveEvent[], cases: Map<string, CaseView>, wallNow: number): PacketSpec[] {
  const out: PacketSpec[] = [];
  const handedOff = new Set<string>();
  for (const e of events) if (e.type === "delegation" || e.type === "dispatch") handedOff.add(e.taskId);
  for (const e of events) {
    if (e.at && wallNow - e.at > STALE_MS) continue;
    switch (e.type) {
      case "case_opened":
        out.push({ from: "entry", to: "intake", skill: "new_case", label: "New case", caseId: e.caseId, kind: "case" });
        break;
      case "dispatch":
        if (isAgentId(e.to)) out.push({ from: "intake", to: e.to, skill: e.skill, caseId: e.caseId });
        break;
      case "delegation":
        if (isAgentId(e.from) && isAgentId(e.to)) out.push({ from: e.from, to: e.to, skill: e.skill, caseId: e.caseId });
        break;
      case "task_started": {
        // Settlement Quality Review is fed by the review outbox, not a delegation edge.
        if (e.agent !== "validator" || handedOff.has(e.taskId)) break;
        const root = cases.get(e.caseId)?.agents.find((a) => a !== "validator");
        if (root) out.push({ from: root, to: "validator", skill: e.skill, caseId: e.caseId });
        break;
      }
      case "lesson":
        if (isAgentId(e.agent) && e.agent !== "validator")
          out.push({ from: "validator", to: e.agent, skill: "lesson", label: "Lesson", caseId: e.caseId, kind: "lesson", landing: "💡 learned a lesson" });
        break;
      case "instruction":
        if (isAgentId(e.agent) && e.agent !== "validator")
          out.push({ from: "validator", to: e.agent, skill: "instructions", label: `Instructions v${e.version}`, caseId: e.caseId, kind: "lesson", landing: `💡 instructions v${e.version}` });
        break;
      case "approval": {
        const by: AgentId = isAgentId(e.agent) ? e.agent : "controller";
        if (e.decision === "pending") out.push({ from: by, to: "human", skill: "approval", label: "Approval", caseId: e.caseId, kind: "stamp" });
        else out.push({ from: "human", to: "controller", skill: e.decision, label: e.decision, caseId: e.caseId, kind: "stamp" });
        break;
      }
    }
  }
  return out;
}
