"use client";
/**
 * Activity panel for one node: status, model, workload, its current tool
 * calls, the cases it is working and its last ten events.
 * Slides over the case explorer; hover previews, click pins.
 */
import Link from "next/link";
import { useMemo, type CSSProperties } from "react";
import { Pin, X } from "lucide-react";
import { Mascot, type Mood } from "@/components/mascot";
import { AGENT_META, NODE_META, agentLabel, isAgentId, skillLabel, type NodeId } from "@/lib/agents";
import { num, str, timestamp, type Row, type Snapshot } from "@/lib/data";
import { buildTimeline, buildToolCalls, fmtAgo, fmtMoney, type AgentWorkload, type CaseView, type TimelineEvent } from "@/lib/ops";
import { PRIORITY_COLOR, shortCase } from "./agent-node";

type Props = {
  id: NodeId;
  pinned: boolean;
  mood: Mood;
  snapshot: Snapshot;
  workload: AgentWorkload | undefined;
  cases: Map<string, CaseView>;
  now: number;
  onClose: () => void;
  onPin: () => void;
  onCase: (caseId: string) => void;
  onEnter: () => void;
  onLeave: () => void;
};

/** Newest `n` rows by a timestamp field, without assuming table order. */
function latest(rows: Row[], field: string, n: number): Row[] {
  if (rows.length <= n) return rows;
  const keyed = rows.map((r, i) => [timestamp(r[field]) || i, r] as const);
  keyed.sort((a, b) => b[0] - a[0]);
  return keyed.slice(0, n).map(([, r]) => r);
}

const EVENT_ACCENT: Partial<Record<TimelineEvent["kind"], string>> = {
  task_completed: "var(--teal)",
  task_failed: "var(--red)",
  task_waiting: "var(--yellow)",
  task_started: "var(--aqua)",
  delegation: "var(--lilac)",
  dispatch: "var(--aqua)",
  approval: "var(--orange)",
  hold: "var(--orange)",
  call: "var(--orange)",
  doc_read: "var(--yellow)",
  review: "var(--teal)",
  policy: "var(--red)",
};

const STATUS_CHIP: Record<string, string> = { working: "ok", idle: "info", offline: "", stale: "warn", error: "bad" };

export function AgentPanel({ id, pinned, mood, snapshot, workload, cases, now, onClose, onPin, onCase, onEnter, onLeave }: Props) {
  const meta = isAgentId(id) ? AGENT_META[id] : NODE_META[id as "intake" | "human"];

  const { events, tools } = useMemo(() => {
    if (id === "human") return { events: [] as TimelineEvent[], tools: [] };
    const mine = (r: Row) => r.agent === id;
    const sub: Snapshot = {
      ...snapshot,
      dispatch: id === "intake" ? latest(snapshot.dispatch, "ts", 40) : latest(snapshot.dispatch.filter(mine), "ts", 20),
      delegation_edge: id === "intake" ? [] : latest(snapshot.delegation_edge.filter((r) => r.from_agent === id || r.to_agent === id), "ts", 30),
      task: id === "intake" ? [] : latest(snapshot.task.filter(mine), "updated_at", 40),
      doc_read: id === "intake" ? [] : latest(snapshot.doc_read.filter(mine), "ts", 20),
      audit_event: id === "intake" ? [] : latest(snapshot.audit_event.filter(mine), "ts", 160),
    };
    // The audit log repeats each delegation edge; keep the edge, labelled with names.
    const edges = new Set<string>();
    for (const r of sub.delegation_edge) edges.add(str(r.task_id, ""));
    const timeline = buildTimeline(sub)
      .filter((e) => (e.agent === id || e.to === id) && !(e.kind === "delegation" && e.id.startsWith("audit:") && edges.has(e.taskId)))
      .map((e) => (e.kind === "delegation" && e.from && e.to ? { ...e, title: `${agentLabel(e.from)} → ${agentLabel(e.to)}`, detail: skillLabel(e.detail) } : e));
    const activeIds = new Set(workload?.activeTasks.map((t) => t.taskId) ?? []);
    const calls = buildToolCalls(sub).filter((c) => activeIds.has(c.taskId));
    const open = calls.filter((c) => !c.endedAt);
    return { events: timeline.slice(-10).reverse(), tools: (open.length ? open : calls.slice(-1)).slice(-4).reverse() };
  }, [id, snapshot, workload]);

  const approvals = useMemo(
    () =>
      id === "human"
        ? snapshot.approval
            .filter((a) => !a.decision || a.decision === "pending")
            .sort((a, b) => timestamp(b.requested_at) - timestamp(a.requested_at))
        : [],
    [id, snapshot.approval],
  );

  const activeCases = id === "human" ? approvals.map((a) => str(a.case_id, "")) : id === "intake" ? [] : (workload?.activeCases ?? []);

  return (
    <aside className="au-panel" aria-label={`${meta.name} activity`} onPointerEnter={onEnter} onPointerLeave={onLeave}>
      <header className="au-panel-head" style={{ "--c": meta.color } as CSSProperties}>
        <Mascot id={id} mood={mood} size={58} />
        <div style={{ minWidth: 0 }}>
          <div className="fl-micro" style={{ color: meta.color }}>
            {meta.nick}
          </div>
          <h2>{meta.name}</h2>
          <div className="fl-dim" style={{ fontSize: 12, marginTop: 3 }}>
            {meta.role}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, alignSelf: "start" }}>
          <button className="fl-btn quiet small" onClick={onClose} aria-label="Close panel">
            <X size={14} />
          </button>
          {!pinned && (
            <button className="fl-btn quiet small" onClick={onPin} aria-label="Pin panel" title="Pin">
              <Pin size={13} />
            </button>
          )}
        </div>
      </header>
      <div className="au-panel-body">
        {workload && (
          <section className="au-panel-sec">
            <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 10 }}>
              <span className={`fl-chip outline ${STATUS_CHIP[workload.status] ?? ""}`}>{workload.status}</span>
              <span className="fl-chip">{workload.model}</span>
              {!workload.enabled && <span className="fl-chip outline warn">disabled</span>}
            </div>
            <div className="au-kv">
              <KV v={workload.active} k="Active" />
              <KV v={workload.queued} k="Queued" />
              <KV v={workload.done.toLocaleString()} k="Done" />
              {workload.errors > 0 && <KV v={workload.errors} k="Errors" hot />}
            </div>
          </section>
        )}
        {id === "intake" && (
          <section className="au-panel-sec">
            <div className="au-kv">
              <KV v={snapshot.case.filter((c) => c.status === "open").length} k="Awaiting" />
              <KV v={snapshot.case.length.toLocaleString()} k="Cases" />
              <KV v={snapshot.dispatch.filter((d) => now - timestamp(d.ts) < 300_000).length} k="Routed 5m" />
            </div>
          </section>
        )}
        {id === "human" && (
          <section className="au-panel-sec">
            <div className="fl-section" style={{ "--accent": "var(--orange)" } as CSSProperties}>
              Waiting on you
            </div>
            {approvals.slice(0, 12).map((a) => (
              <div key={str(a.id)} className="au-tool">
                <span className="fl-chip outline hot">{str(a.action, "action").replaceAll("_", " ")}</span>
                <button style={{ textAlign: "left", minWidth: 0 }} onClick={() => onCase(str(a.case_id, ""))}>
                  <code>{str(a.case_id)}</code>
                </button>
                <span className="fl-mono">{fmtMoney(num(a.amount))}</span>
              </div>
            ))}
          </section>
        )}
        {tools.length > 0 && (
          <section className="au-panel-sec">
            <div className="fl-section">{tools.some((t) => !t.endedAt) ? "Tool calls" : "Last tool call"}</div>
            {tools.map((t) => (
              <div key={t.id} className="au-tool">
                <span className={`fl-chip outline ${t.endedAt ? "" : "info"}`}>{t.endedAt ? t.status : "running"}</span>
                <code title={t.taskId}>{t.name}</code>
                <span className="fl-micro">{t.endedAt ? `${Math.round(t.durationMs)}ms` : fmtAgo(t.startedAt, now)}</span>
              </div>
            ))}
          </section>
        )}
        {activeCases.length > 0 && (
          <section className="au-panel-sec">
            <div className="fl-section">Cases</div>
            <div className="au-links">
              {activeCases.slice(0, 16).map((caseId) => (
                <Link
                  key={caseId}
                  href={`/cases/${encodeURIComponent(caseId)}`}
                  style={{ "--p": PRIORITY_COLOR[cases.get(caseId)?.priority ?? "P3"] } as CSSProperties}
                  onMouseEnter={() => onCase(caseId)}
                  title={cases.get(caseId)?.title}
                >
                  {shortCase(caseId)}
                </Link>
              ))}
              {activeCases.length > 16 && <span className="fl-micro">+{activeCases.length - 16}</span>}
            </div>
          </section>
        )}
        {events.length > 0 && (
          <section className="au-panel-sec">
            <div className="fl-section">Events</div>
            <ul className="au-events">
              {events.map((e) => (
                <li key={e.id} style={{ "--accent": EVENT_ACCENT[e.kind] } as CSSProperties}>
                  <i />
                  <div style={{ minWidth: 0 }}>
                    <div className="t">{e.title}</div>
                    <div className="d">
                      {e.kind.replace("_", " ")} · {e.caseId}
                      {e.detail ? ` · ${e.detail}` : ""}
                    </div>
                  </div>
                  <time>{fmtAgo(e.at, now)}</time>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </aside>
  );
}

function KV({ v, k, hot = false }: { v: number | string; k: string; hot?: boolean }) {
  return (
    <div>
      <div className="v" style={hot ? { color: "var(--orange)" } : undefined}>
        {v}
      </div>
      <div className="k">{k}</div>
    </div>
  );
}
