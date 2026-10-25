"use client";
/**
 * The execution trace drawn like a git graph rotated 90°: one lane per agent
 * (mascot as the lane header), time flowing down, each task a branch segment
 * with its card, tool calls and document reads as commits on the lane line,
 * and delegations / reviews / approvals as curved edges between lanes.
 *
 * Everything shown is filtered by `cursor` (Infinity when live), so replay
 * renders exactly what had happened at that moment.
 */
import { memo, useMemo, useState, type CSSProperties } from "react";
import { FileText } from "lucide-react";
import { Mascot, type Mood } from "@/components/mascot";
import { agentColor, agentLabel, NODE_META, skillLabel } from "@/lib/agents";
import { fmtMoney } from "@/lib/ops";
import { str } from "@/lib/data";
import {
  clock,
  eventLabel,
  milestoneAt,
  routingReason,
  nodeStatus,
  shortDur,
  timer,
  toolLabel,
  toolsAt,
  type CaseTrace,
  type NodeStatus,
  type TraceEdge,
  type TraceLayout,
  type TraceNode,
} from "./model";

export type GraphProps = {
  trace: CaseTrace;
  layout: TraceLayout;
  /** Infinity when live. */
  cursor: number;
  /** Time used for running timers and the head line. */
  clockAt: number;
  /** Draw the head line (live case still running, or replay cursor). */
  showHead: boolean;
  replay: boolean;
  selected: string | null;
  onSelect: (id: string) => void;
  celebrating: Set<string>;
  pings: Set<string>;
};

const TERMINAL: NodeStatus[] = ["completed", "failed", "canceled", "approved", "rejected"];
const statusTone = (s: NodeStatus | null) =>
  s === "completed" || s === "approved"
    ? "var(--teal)"
    : s === "failed" || s === "rejected"
      ? "var(--red)"
      : s === "waiting" || s === "pending"
        ? "var(--orange)"
        : s === "queued" || s === "canceled"
          ? "var(--text-mute)"
          : "var(--yellow)";

const domId = (id: string) => id.replace(/[^a-zA-Z0-9_-]/g, "_");

function edgeStyle(e: TraceEdge, trace: CaseTrace): { color: string; dashed: boolean } {
  switch (e.kind) {
    case "dispatch":
      return { color: NODE_META.intake.color, dashed: false };
    case "delegation":
      return { color: agentColor(trace.nodeById.get(e.to)?.lane), dashed: false };
    case "review":
      return { color: agentColor("validator"), dashed: true };
    case "approval":
      return { color: NODE_META.human.color, dashed: true };
    case "decision":
      return { color: str(e.row?.decision) === "approved" ? "#22c09b" : "#e40014", dashed: true };
    case "retry":
      return { color: "#e40014", dashed: true };
  }
}

export const TraceGraph = memo(function TraceGraph(props: GraphProps) {
  const { trace, layout, cursor, clockAt, showHead, replay, selected, onSelect, celebrating, pings } = props;
  const [hovered, setHovered] = useState<string | null>(null);
  const focus = hovered ?? selected;

  const statuses = useMemo(() => new Map(trace.nodes.map((n) => [n.id, nodeStatus(n, cursor)])), [trace, cursor]);
  const visible = trace.nodes.filter((n) => statuses.get(n.id));
  const headY = layout.yOf(clockAt);
  // Lifecycle events: small ticks on the Intake lane, shown once the cursor passes them.
  const intakeLane = layout.laneById.get("intake");
  const ticks = intakeLane
    ? trace.milestones.flatMap((m) => {
        const text = milestoneAt(m, cursor);
        const y = layout.anchorY.get(m.id);
        if (!text || y === undefined) return [];
        const seen = m.times.filter((t) => t <= cursor);
        const when = seen.length > 1 ? `${clock(seen[0])}–${clock(seen[seen.length - 1])}` : clock(m.at);
        return [{ m, y, text, title: `${m.type === "gps.ping" ? text : eventLabel(m.type)} · ${when} · logged, no agent needed` }];
      })
    : [];

  const edges = trace.edges.filter((e) => e.at <= cursor && statuses.get(e.from) && statuses.get(e.to));
  const edgeGeom = edges.map((e) => {
    const from = trace.nodeById.get(e.from)!;
    const to = trace.nodeById.get(e.to)!;
    const fl = layout.laneById.get(from.lane);
    const tl = layout.laneById.get(to.lane);
    if (!fl || !tl) return null;
    const y1 =
      e.kind === "dispatch"
        ? (layout.nodeY.get(from.id) ?? 0)
        : e.kind === "review" || e.kind === "retry"
          ? (layout.anchorY.get(`end:${from.id}`) ?? layout.yOf(e.at))
          : (layout.anchorY.get(`src:${e.id}`) ?? layout.yOf(e.at));
    const y2 = e.kind === "decision" ? (layout.anchorY.get(`end:${to.id}`) ?? layout.yOf(e.at)) : (layout.nodeY.get(to.id) ?? y1);
    const x1 = fl.lineX;
    const x2 = tl.lineX;
    const dy = y2 - y1;
    const d = `M${x1} ${y1} C${x1} ${y1 + dy * 0.62} ${x2} ${y2 - dy * 0.38} ${x2} ${y2}`;
    const { color, dashed } = edgeStyle(e, trace);
    return { e, d, x1, y1, color, dashed, hl: focus === e.from || focus === e.to };
  });

  // Lane headers: who is working, who just finished, who is asleep (replay before their first task).
  const laneMood = (lane: string): { mood: Mood; asleep: boolean; label: string } => {
    const mine = trace.nodes.filter((n) => n.lane === lane);
    const seen = mine.filter((n) => statuses.get(n.id));
    // Only lifecycle ticks so far: the router is following the load, nothing to route yet.
    if (!seen.length && lane === "intake" && ticks.length) return { mood: "idle", asleep: false, label: "watching" };
    if (!seen.length) return { mood: "sleeping", asleep: true, label: "not yet" };
    const st = seen.map((n) => statuses.get(n.id)!);
    if (seen.some((n) => celebrating.has(n.id))) return { mood: "success", asleep: false, label: "done" };
    if (st.includes("working")) return { mood: lane === "voice_desk" ? "talking" : "working", asleep: false, label: "working" };
    if (st.includes("waiting") || st.includes("pending")) return { mood: "alert", asleep: false, label: lane === "human" ? "your call" : "waiting" };
    if (st[st.length - 1] === "failed") return { mood: "alert", asleep: false, label: "failed" };
    if (st.includes("queued")) return { mood: "idle", asleep: false, label: "queued" };
    return { mood: "idle", asleep: false, label: "done" };
  };

  // Gutter timestamps without collisions.
  const labels: { y: number; t: number; id: string }[] = [];
  let lastLabel = -100;
  let lastText = "";
  for (const n of [...visible].sort((a, b) => (layout.nodeY.get(a.id) ?? 0) - (layout.nodeY.get(b.id) ?? 0))) {
    const y = layout.nodeY.get(n.id) ?? 0;
    const text = clock(n.start);
    if (y - lastLabel < 14 || text === lastText) continue;
    labels.push({ y, t: n.start, id: n.id });
    lastLabel = y;
    lastText = text;
  }

  return (
    <div className="ct-graph" style={{ width: layout.width }}>
      <div className="ct-lanes-head">
        <div className="ct-gutter-head fl-micro">Time</div>
        {layout.lanes.map((lane) => {
          const m = laneMood(lane.id);
          const count = trace.nodes.filter((n) => n.lane === lane.id).length;
          return (
            <div
              key={lane.id}
              className={`ct-lane-head ${m.asleep ? "asleep" : ""}`}
              style={{ left: lane.x, width: lane.width, "--agent": agentColor(lane.id) } as CSSProperties}
            >
              <Mascot id={lane.id} size={38} mood={m.mood} />
              <div className="ct-lane-name">
                <span>{agentLabel(lane.id)}</span>
                <span className="fl-micro">
                  {count > 1 && lane.id !== "intake" ? `${count} ${lane.id === "human" ? "approvals" : "tasks"} · ` : ""}
                  {m.label}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="ct-canvas" style={{ height: layout.height }}>
        <svg className="ct-svg" width={layout.width} height={layout.height} aria-hidden>
          {/* lane guides */}
          {layout.lanes.map((lane) => (
            <line key={lane.id} x1={lane.lineX} x2={lane.lineX} y1={0} y2={layout.height} className="ct-guide" />
          ))}
          {/* idle gaps */}
          {layout.gaps.map((g, i) => (
            <line key={i} x1={8} x2={layout.width - 8} y1={(g.y0 + g.y1) / 2} y2={(g.y0 + g.y1) / 2} className="ct-gap-line" />
          ))}
          {/* edges under everything else */}
          <g className={`ct-edges ${focus ? "has-focus" : ""}`}>
            {edgeGeom.map((g) => {
              if (!g) return null;
              const id = domId(g.e.id);
              return (
                <g key={g.e.id} className={`ct-edge kind-${g.e.kind} ${g.hl ? "hl" : ""}`}>
                  <title>{edgeTitle(g.e, trace)}</title>
                  {g.dashed ? (
                    <>
                      <mask id={`m-${id}`} maskUnits="userSpaceOnUse" x={0} y={0} width={layout.width} height={layout.height}>
                        <path d={g.d} pathLength={1} className="ct-draw" stroke="#fff" strokeWidth={8} fill="none" />
                      </mask>
                      <path d={g.d} stroke={g.color} strokeWidth={1.6} strokeDasharray="4 4" fill="none" mask={`url(#m-${id})`} />
                    </>
                  ) : (
                    <path d={g.d} pathLength={1} className="ct-draw" stroke={g.color} strokeWidth={1.8} fill="none" />
                  )}
                  <circle cx={g.x1} cy={g.y1} r={3} fill={g.color} className="ct-pop" />
                </g>
              );
            })}
          </g>
          {/* task branches */}
          {visible.map((n) => {
            const lane = layout.laneById.get(n.lane);
            const y0 = layout.nodeY.get(n.id);
            if (!lane || y0 === undefined) return null;
            const status = statuses.get(n.id)!;
            const color = n.kind === "task" ? agentColor(n.lane) : n.kind === "intake" ? NODE_META.intake.color : NODE_META.human.color;
            const done = n.end && n.end <= cursor;
            const yEnd = n.kind === "intake" ? y0 : done ? (layout.anchorY.get(`end:${n.id}`) ?? layout.yOf(n.end)) : Math.max(y0, headY);
            const running = !done && n.kind !== "intake";
            return (
              <g key={n.id} className={`ct-branch ${running ? "running" : ""}`} style={{ "--agent": color } as CSSProperties}>
                {yEnd > y0 && (
                  <line x1={lane.lineX} x2={lane.lineX} y1={y0} y2={yEnd} stroke={color} className={running ? "ct-seg live" : "ct-seg"} />
                )}
                {n.waits.map(([from, to], i) => {
                  if (from > cursor) return null;
                  const a = layout.yOf(from);
                  const b = to && to <= cursor ? layout.yOf(to) : headY;
                  return b > a ? <line key={i} x1={lane.lineX} x2={lane.lineX} y1={a} y2={b} className="ct-seg wait" /> : null;
                })}
                {toolsAt(n, cursor).map(({ call, status: s }) => {
                  const y = layout.anchorY.get(`tc:${call.id}`) ?? layout.yOf(call.startedAt);
                  return (
                    <rect key={call.id} x={lane.lineX - 3.5} y={y - 3.5} width={7} height={7} className={`ct-tick tool ${s}`} style={{ fill: s === "failed" ? "#e40014" : color }}>
                      <title>
                        {toolLabel(call.name)} · {s}
                        {call.durationMs ? ` · ${shortDur(call.durationMs)}` : ""}
                      </title>
                    </rect>
                  );
                })}
                {n.docs
                  .filter((d) => d.at <= cursor)
                  .map((d) => {
                    const y = layout.anchorY.get(`doc:${d.id}`) ?? layout.yOf(d.at);
                    return (
                      <rect key={d.id} x={lane.lineX - 3} y={y - 3} width={6} height={6} className="ct-tick doc">
                        <title>Read {d.docRef}</title>
                      </rect>
                    );
                  })}
                <circle cx={lane.lineX} cy={y0} r={6} fill={color} className="ct-commit ct-pop" />
                {running && <circle cx={lane.lineX} cy={y0} r={6} className="ct-commit-pulse" stroke={color} />}
                {done && n.kind !== "intake" && <circle cx={lane.lineX} cy={yEnd} r={4} className="ct-end ct-pop" stroke={statusTone(status)} />}
              </g>
            );
          })}
          {intakeLane &&
            ticks.map((k) => (
              <line key={k.m.id} x1={intakeLane.lineX - 4} x2={intakeLane.lineX + 4} y1={k.y} y2={k.y} className="ct-milestone-tick" />
            ))}
          {showHead && <line x1={8} x2={layout.width - 8} y1={headY} y2={headY} className={`ct-head-line ${replay ? "replay" : ""}`} />}
        </svg>

        {layout.gaps.map((g, i) => (
          <span key={i} className="ct-gap-label fl-micro" style={{ transform: `translateY(${(g.y0 + g.y1) / 2 - 8}px)` }}>
            ⋯ {shortDur(g.dt)}
          </span>
        ))}
        {labels.map((l) => (
          <span key={l.id} className="ct-time fl-mono" style={{ transform: `translateY(${l.y - 7}px)` }}>
            {clock(l.t)}
          </span>
        ))}
        {intakeLane &&
          ticks.map((k) => (
            <span key={k.m.id} className="ct-milestone fl-reveal" style={{ left: intakeLane.cardX, top: k.y - 7 }} title={k.title}>
              {k.text}
              <span className="fl-mono">{clock(k.m.at)}</span>
            </span>
          ))}
        {showHead && (
          <span className={`ct-head-label ${replay ? "replay" : ""}`} style={{ transform: `translateY(${headY - 9}px)` }}>
            {replay ? clock(clockAt) : "NOW"}
          </span>
        )}

        {visible.map((n, i) => {
          const lane = layout.laneById.get(n.lane);
          const y = layout.nodeY.get(n.id);
          if (!lane || y === undefined) return null;
          return (
            <NodeCard
              key={n.id}
              node={n}
              trace={trace}
              status={statuses.get(n.id)!}
              statuses={statuses}
              cursor={cursor}
              clockAt={clockAt}
              style={{ left: lane.cardX, top: y - 11, width: lane.cardW, "--agent": agentColor(n.lane), "--delay": `${Math.min(i, 12) * 45}ms` } as CSSProperties}
              selected={selected === n.id}
              dim={Boolean(focus) && focus !== n.id && !trace.edges.some((e) => (e.from === focus && e.to === n.id) || (e.to === focus && e.from === n.id))}
              celebrating={celebrating.has(n.id)}
              ping={pings.has(n.id)}
              onSelect={onSelect}
              onHover={setHovered}
            />
          );
        })}
      </div>
    </div>
  );
});

function edgeTitle(e: TraceEdge, trace: CaseTrace) {
  const from = trace.nodeById.get(e.from);
  const to = trace.nodeById.get(e.to);
  const verb =
    e.kind === "dispatch"
      ? "routed"
      : e.kind === "delegation"
        ? "delegated"
        : e.kind === "review"
          ? "sent for review"
          : e.kind === "approval"
            ? "asked a human to approve"
            : e.kind === "decision"
              ? str(e.row?.decision)
              : "retried";
  return `${agentLabel(from?.lane)} ${verb} → ${agentLabel(to?.lane)}${e.skill && e.kind !== "decision" ? ` · ${skillLabel(e.skill)}` : ""} · ${clock(e.at)}`;
}

/* ------------------------------ node cards ------------------------------ */

type CardProps = {
  node: TraceNode;
  trace: CaseTrace;
  status: NodeStatus;
  statuses: Map<string, NodeStatus | null>;
  cursor: number;
  clockAt: number;
  style: CSSProperties;
  selected: boolean;
  dim: boolean;
  celebrating: boolean;
  ping: boolean;
  onSelect: (id: string) => void;
  onHover: (id: string | null) => void;
};

function NodeCard(p: CardProps) {
  const { node, status, selected, dim, celebrating, ping } = p;
  const working = status === "working" || status === "queued";
  const className = [
    "ct-card fl-reveal",
    `kind-${node.kind}`,
    `st-${status}`,
    selected ? "selected" : "",
    dim ? "dim" : "",
    ping ? "ping" : "",
  ].join(" ");
  return (
    <button
      className={className}
      style={p.style}
      onClick={() => p.onSelect(node.id)}
      onMouseEnter={() => p.onHover(node.id)}
      onMouseLeave={() => p.onHover(null)}
      onFocus={() => p.onHover(node.id)}
      onBlur={() => p.onHover(null)}
      aria-pressed={selected}
    >
      {node.kind === "task" ? <TaskBody {...p} /> : node.kind === "intake" ? <IntakeBody {...p} /> : <HumanBody {...p} />}
      {node.kind === "task" && (working || celebrating) && (
        <Mascot
          id={node.lane}
          size={30}
          mood={celebrating ? "success" : node.lane === "voice_desk" ? "talking" : "working"}
          className="ct-card-mascot"
        />
      )}
    </button>
  );
}

function StatusBadge({ node, status, clockAt }: { node: TraceNode; status: NodeStatus; clockAt: number }) {
  if (status === "working" || status === "waiting" || status === "pending")
    return (
      <span className="ct-badge" style={{ color: statusTone(status) }}>
        <i className="fl-dot pulse" style={{ "--accent": statusTone(status) } as CSSProperties} />
        {timer(clockAt - node.start)}
      </span>
    );
  if (status === "queued") return <span className="ct-badge fl-mute">queued</span>;
  if (status === "failed" || status === "rejected") return <span className="ct-badge" style={{ color: "var(--red)" }}>✗ {status}</span>;
  if (status === "canceled") return <span className="ct-badge fl-mute">canceled</span>;
  return (
    <span className="ct-badge" style={{ color: "var(--teal)" }}>
      ✓ {node.end > node.start ? shortDur(node.end - node.start) : "done"}
    </span>
  );
}

function TaskBody({ node, trace, status, statuses, cursor, clockAt }: CardProps) {
  const t = node.task!;
  const tools = toolsAt(node, cursor);
  const shown = tools.slice(-3);
  const hidden = tools.length - shown.length;
  const docs = node.docs.filter((d) => d.at <= cursor).length;
  const pendingChildren = trace.edges
    .filter((e) => e.kind === "delegation" && e.from === node.id && e.at <= cursor)
    .map((e) => trace.nodeById.get(e.to)!)
    .filter((c) => {
      const s = statuses.get(c.id);
      return s && !TERMINAL.includes(s);
    });
  const outcome =
    status === "completed"
      ? (t.outcome || "completed").replaceAll("_", " ")
      : status === "failed"
        ? str(t.artifact?.summary, "failed").slice(0, 80)
        : status === "waiting"
          ? "waiting for human"
          : status === "queued"
            ? "queued"
            : pendingChildren.length
              ? pendingChildren.length === 1
                ? `waiting on ${agentLabel(pendingChildren[0].lane).split(" ")[0]}`
                : `waiting on ${pendingChildren.length} agents`
              : cursor === Infinity && t.businessStatus
                ? t.businessStatus.replaceAll("_", " ")
                : "working";
  return (
    <>
      <span className="ct-card-top">
        <span className="ct-card-title">{skillLabel(t.skill)}</span>
        <StatusBadge node={node} status={status} clockAt={clockAt} />
      </span>
      <span className="ct-tools">
        {hidden > 0 && <span className="ct-tool more">+{hidden} earlier</span>}
        {shown.length > 0 &&
          shown.map(({ call, status: s }) => (
            <span key={call.id} className={`ct-tool ${s}`}>
              <span className="ct-tool-name" title={call.name}>
                {toolLabel(call.name)}
              </span>
              <span className="ct-tool-glyph">{s === "running" ? "…" : s === "completed" ? "✓" : "✗"}</span>
              <span className="ct-tool-dur">{s === "running" ? shortDur(clockAt - call.startedAt) : call.durationMs ? shortDur(call.durationMs) : ""}</span>
            </span>
          ))}
        {!shown.length && !TERMINAL.includes(status) && <span className="ct-tool more">planning…</span>}
      </span>
      <span className="ct-card-foot">
        {docs > 0 && (
          <span className="ct-docs" title={`${docs} document${docs === 1 ? "" : "s"} read`}>
            <FileText size={11} /> {docs}
          </span>
        )}
        <span className="ct-outcome" style={{ color: statusTone(status) }} title={str(t.artifact?.summary, "")}>
          {outcome}
        </span>
      </span>
    </>
  );
}

const INTAKE_TITLES: Record<string, string> = {
  accepted: "Routed", duplicate: "Duplicate", triage: "Triage",
};

function IntakeBody({ node, clockAt, status }: CardProps) {
  const d = node.dispatch!;
  const decision = str(d.status, "");
  const agent = str(d.agent, "");
  const reason = routingReason(str(d.reason, "")) || skillLabel(str(d.skill, ""));
  return (
    <>
      <span className="ct-card-top">
        <span className="ct-card-title">{INTAKE_TITLES[decision] ?? "Routed"}</span>
        {status !== "completed" && <StatusBadge node={node} status={status} clockAt={clockAt} />}
      </span>
      <span className="ct-card-line" style={{ color: agent ? agentColor(agent) : undefined }}>
        {agent ? `→ ${agentLabel(agent)}` : "Held for a person"}
      </span>
      <span className="ct-card-line fl-mute" title={reason}>
        {reason}
      </span>
    </>
  );
}

function HumanBody({ node, status, clockAt }: CardProps) {
  const a = node.approval!;
  return (
    <>
      <span className="ct-card-top">
        <span className="ct-card-title" title={`Approve ${str(a.action, "action").replaceAll("_", " ")}`}>
          {skillLabel(str(a.action, "approval")).replace(/^./, (c) => c.toUpperCase())}
        </span>
        <StatusBadge node={node} status={status} clockAt={clockAt} />
      </span>
      <span className="ct-card-line fl-num">{fmtMoney(Number(a.amount) || 0)} · {str(a.target_id || a.invoice_id, "")}</span>
      <span className="ct-card-line" style={{ color: statusTone(status) }}>
        {status === "pending" ? "awaiting approval" : `${status} by ${str(a.approver_name, "human")}`}
      </span>
    </>
  );
}
