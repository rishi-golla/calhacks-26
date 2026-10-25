"use client";
/**
 * Activity panel for one node: a particle portrait of its mascot, non-default status,
 * model, workload, its current tool calls, the cases it is working, the
 * lessons it has learned from (or, for Settlement Quality Review, sent to)
 * other agents, and its last ten events.
 * Slides over the case explorer; hover previews, click pins.
 */
import Link from "next/link";
import { useMemo, useState, type CSSProperties } from "react";
import { Pin, X } from "lucide-react";
import { Mascot, type Mood } from "@/components/mascot";
import { AGENT_META, NODE_META, agentLabel, isAgentId, skillLabel, type AgentId, type NodeId } from "@/lib/agents";
import { num, str, timestamp, type Row, type Snapshot } from "@/lib/data";
import type { Lesson, LessonIndex } from "@/lib/lessons";
import { buildTimeline, buildToolCalls, fmtAgo, fmtMoney, type AgentWorkload, type CaseView, type TimelineEvent } from "@/lib/ops";
import { PRIORITY_COLOR, shortCase } from "./agent-node";
import { MascotPortrait, useWebGl2 } from "./mascot-portrait";

type Props = {
  id: NodeId;
  pinned: boolean;
  mood: Mood;
  snapshot: Snapshot;
  workload: AgentWorkload | undefined;
  lessons: LessonIndex;
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

export function AgentPanel({ id, pinned, mood, snapshot, workload, lessons, cases, now, onClose, onPin, onCase, onEnter, onLeave }: Props) {
  const meta = isAgentId(id) ? AGENT_META[id] : NODE_META[id as "intake" | "human"];
  // The human controller has no generated character, so it keeps the flat blob.
  const [portraitFailed, setPortraitFailed] = useState(false);
  const portrait = useWebGl2() && id !== "human" && !portraitFailed;

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
      .map((e) =>
        e.kind === "delegation" && e.from && e.to
          ? { ...e, title: `${agentLabel(e.from)} → ${agentLabel(e.to)}`, detail: skillLabel(e.detail) }
          : // The routing reason and a started task's id are plumbing; the title already says what happened.
            e.kind === "dispatch"
            ? { ...e, title: `→ ${agentLabel(e.to)}`, detail: "" }
            : e.kind === "task_started"
              ? { ...e, title: skillLabel(e.row.skill ?? e.title), detail: "" }
              : e,
      );
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
      {portrait && <MascotPortrait id={id} onError={() => setPortraitFailed(true)} />}
      <header className={`au-panel-head${portrait ? " no-mascot" : ""}`} style={{ "--c": meta.color } as CSSProperties}>
        {!portrait && <Mascot id={id} mood={mood} size={58} />}
        <div style={{ minWidth: 0 }}>
          <div className="fl-micro" style={{ color: meta.color }}>
            {meta.nick}
          </div>
          <h2 title={meta.role}>{meta.name}</h2>
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
          <WorkloadSection workload={workload} />
        )}
        {id === "intake" && (
          <KVSection
            items={[
              [snapshot.case.filter((c) => c.status === "open").length, "Awaiting"],
              [snapshot.case.length, "Cases"],
              [snapshot.dispatch.filter((d) => now - timestamp(d.ts) < 300_000).length, "Routed 5m"],
            ]}
          />
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
                <li key={e.id} style={{ "--accent": EVENT_ACCENT[e.kind] } as CSSProperties} title={e.kind.replaceAll("_", " ")}>
                  <i />
                  <div style={{ minWidth: 0 }}>
                    <div className="t">{e.title}</div>
                    <div className="d" title={e.caseId}>
                      {[shortCase(e.caseId), e.detail].filter(Boolean).join(" · ")}
                    </div>
                  </div>
                  <time>{fmtAgo(e.at, now)}</time>
                </li>
              ))}
            </ul>
          </section>
        )}
        {/* Learning is reference detail: last, under the live activity. */}
        {isAgentId(id) && <Lessons id={id} index={lessons} now={now} />}
      </div>
    </aside>
  );
}

/** Workload counters with the zeros left out; status and model only when they are not the default. */
function WorkloadSection({ workload }: { workload: AgentWorkload }) {
  const chips = [
    workload.status !== "idle" && (
      <span key="s" className={`fl-chip outline ${STATUS_CHIP[workload.status] ?? ""}`}>
        {workload.status}
      </span>
    ),
    workload.model && workload.model !== "auto" && (
      <span key="m" className="fl-chip">
        {workload.model}
      </span>
    ),
    !workload.enabled && (
      <span key="d" className="fl-chip outline warn">
        disabled
      </span>
    ),
  ].filter(Boolean);
  const items: [number, string, boolean?][] = [
    [workload.active, "Active"],
    [workload.queued, "Queued"],
    [workload.stalled, "Stalled", true],
    [workload.done, "Done"],
    [workload.errors, "Errors", true],
  ];
  if (!chips.length && !items.some(([v]) => v > 0)) return null;
  return (
    <section className="au-panel-sec">
      {chips.length > 0 && <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 10 }}>{chips}</div>}
      <KVGrid items={items} />
    </section>
  );
}

/** Lessons shown before "+N more". */
const LESSONS_SHOWN = 6;

/** What this agent learned from Settlement Quality Review; for the Validator, what it taught. */
function Lessons({ id, index, now }: { id: AgentId; index: LessonIndex; now: number }) {
  const [all, setAll] = useState(false);
  const teacher = id === "validator";
  const list = teacher ? index.all.filter((l) => l.agent !== "validator") : (index.byAgent.get(id) ?? []);
  const instructions = teacher ? undefined : index.instructions.get(id);
  if (!list.length && !instructions) return null;
  const recalled = list.reduce((n, l) => n + l.recalled, 0);
  const shown = all ? list : list.slice(0, LESSONS_SHOWN);
  return (
    <section className="au-panel-sec au-learn">
      <div className="fl-section">
        {teacher ? "Lessons sent" : "Lessons learned"}
      </div>
      <div className="au-kv">
        <KV v={list.length} k={teacher ? "Active" : "Lessons"} />
        {teacher ? <KV v={new Set(list.map((l) => l.agent)).size} k="Agents" /> : <KV v={instructions ? `v${instructions.version}` : "—"} k="Instructions" />}
        <KV v={recalled.toLocaleString()} k="Recalled" />
      </div>
      {instructions && (instructions.learned.length > 0 || instructions.author === "human") && (
        <div className="au-instr">
          <div className="fl-micro">
            Instructions v{instructions.version}
            {instructions.author === "human" && (instructions.revertsVersion ? ` · rolled back to v${instructions.revertsVersion} by a person` : " · edited by a person")} ·{" "}
            {fmtAgo(instructions.at, now)}
          </div>
          {instructions.learned.map((line, i) => (
            <p key={i}>{line}</p>
          ))}
          {!instructions.learned.length && <p className="fl-dim">No learned rules in effect.</p>}
        </div>
      )}
      {list.length > 0 && (
        <ul className="au-lesson-list">
          {shown.map((l) => (
            <LessonItem key={l.id} lesson={l} teacher={teacher} now={now} />
          ))}
        </ul>
      )}
      {list.length > LESSONS_SHOWN && (
        <button className="fl-btn quiet small au-lesson-more" onClick={() => setAll((v) => !v)}>
          {all ? "Show fewer" : `Show all ${list.length}`}
        </button>
      )}
    </section>
  );
}

function KVSection({ items }: { items: [number, string, boolean?][] }) {
  if (!items.some(([v]) => v > 0)) return null;
  return (
    <section className="au-panel-sec">
      <KVGrid items={items} />
    </section>
  );
}

function KVGrid({ items }: { items: [number, string, boolean?][] }) {
  const shown = items.filter(([v]) => v > 0);
  if (!shown.length) return null;
  return (
    <div className="au-kv">
      {shown.map(([v, k, hot]) => (
        <KV key={k} v={v.toLocaleString()} k={k} hot={hot} />
      ))}
    </div>
  );
}

function LessonItem({ lesson: l, teacher, now }: { lesson: Lesson; teacher: boolean; now: number }) {
  const [open, setOpen] = useState(false);
  return (
    <li style={{ "--c": AGENT_META[l.agent].color } as CSSProperties}>
      {teacher && <div className="to">→ {AGENT_META[l.agent].name}</div>}
      <button type="button" className={`txt ${open ? "open" : ""}`} onClick={() => setOpen((v) => !v)} title={open ? "Collapse" : "Expand"}>
        {l.text}
      </button>
      {l.scope.length > 0 && (
        <div className="scope">
          {l.scope.map((c) => (
            <span key={c}>{c}</span>
          ))}
        </div>
      )}
      <div className="foot">
        {l.caseId ? (
          <>
            from{" "}
            <Link href={`/cases/${encodeURIComponent(l.caseId)}`}>
              {shortCase(l.caseId)}
            </Link>
          </>
        ) : (
          "from a review"
        )}
        <span>· {fmtAgo(l.at, now)}</span>
        <span className={l.recalled ? "hot" : ""}>· {l.recalled ? `used ${l.recalled}×` : "not used yet"}</span>
      </div>
    </li>
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
