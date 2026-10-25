"use client";
/**
 * /cases/[id] — per-case execution trace. Header, ledger strip,
 * the git-style graph (built only from rows that exist), a replay scrubber
 * and a details drawer.
 */
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { ArrowLeft, ListOrdered, Network, Pause, Play, Radio, Rewind } from "lucide-react";
import { useLiveEvents, useOps } from "@/components/data-provider";
import Backdrop from "@/components/fx/backdrop";
import { Mascot } from "@/components/mascot";
import { agentColor } from "@/lib/agents";
import { fmtMoney } from "@/lib/ops";
import { PriorityChip, StatusChip } from "./case-explorer";
import { CaseLog, NodeDetails } from "./node-panel";
import { TraceGraph } from "./trace-graph";
import { buildCaseTrace, caseKind, clock, KIND_LABEL, layoutTrace, nodeStatus, shortDur, sideEffectAt, timer, type CaseTrace as Trace, type NodeStatus } from "./model";
import "./cases.css";

type Speed = 1 | 4 | 16;
const SPEEDS: Speed[] = [1, 4, 16];
/** Idle stretches longer than this replay faster so playback never stalls. */
const IDLE_SKIP_MS = 8_000;

export default function CaseTrace({ caseId }: { caseId: string }) {
  // Unscoped: a direct link to a case from before a "Start fresh" epoch still opens.
  const { fullSnapshot: snapshot, allCases: cases, now, simulated, source } = useOps();
  const c = cases.get(caseId);
  const trace = useMemo(() => buildCaseTrace(snapshot, caseId), [snapshot, caseId]);

  /* -------- replay state -------- */
  const [replayAt, setReplayAt] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<Speed>(4);
  /** Live events for this case that arrived while replaying. */
  const [missed, setMissed] = useState(0);
  const replay = replayAt !== null;
  const t0 = trace.first;
  const liveEnd = trace.open ? Math.max(now, trace.last) : trace.last;
  const tEnd = Math.max(liveEnd, t0 + 1);
  const cursor = replay ? replayAt : Infinity;
  const clockAt = replay ? replayAt : trace.open ? Math.max(now, trace.last) : trace.last;
  const tail = trace.open ? tEnd : 0;
  const layout = useMemo(() => layoutTrace(trace, tail), [trace, tail]);

  const endRef = useRef(tEnd);
  endRef.current = tEnd;
  const eventTimes = useMemo(() => [...new Set([...trace.timeline.map((e) => e.at), ...trace.nodes.map((n) => n.start)])].sort((a, b) => a - b), [trace]);
  const eventTimesRef = useRef(eventTimes);
  eventTimesRef.current = eventTimes;

  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    let last = performance.now();
    const step = (ts: number) => {
      const dt = Math.min(100, ts - last);
      last = ts;
      setReplayAt((at) => {
        if (at === null) return at;
        const next = eventTimesRef.current.find((t) => t > at);
        const idle = next === undefined ? at < endRef.current : next - at > IDLE_SKIP_MS;
        const advanced = at + dt * speed * (idle ? 6 : 1);
        return Math.min(endRef.current, advanced);
      });
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing, speed]);
  useEffect(() => {
    if (playing && replayAt !== null && replayAt >= tEnd) setPlaying(false);
  }, [playing, replayAt, tEnd]);

  const goLive = useCallback(() => {
    setReplayAt(null);
    setPlaying(false);
    setMissed(0);
  }, []);
  const startReplay = useCallback(() => {
    setReplayAt(t0);
    setPlaying(true);
  }, [t0]);
  const togglePlay = () => {
    if (!replay || (replayAt ?? 0) >= tEnd) startReplay();
    else setPlaying((p) => !p);
  };

  /* -------- selection / drawer -------- */
  const [selected, setSelected] = useState<string | null>(null);
  const [panel, setPanel] = useState<"closed" | "node" | "log">("closed");
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const onSelect = useCallback((id: string) => {
    const again = selectedRef.current === id;
    setSelected(again ? null : id);
    setPanel(again ? "closed" : "node");
  }, []);
  const selectFromPanel = useCallback((id: string) => {
    setSelected(id);
    setPanel("node");
  }, []);
  const closePanel = useCallback(() => {
    setPanel("closed");
    setSelected(null);
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closePanel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [closePanel]);
  const selectedNode = selected ? trace.nodeById.get(selected) : undefined;

  /* -------- celebrations: a task that just completed makes its mascot hop -------- */
  const statuses = useMemo(() => new Map(trace.nodes.map((n) => [n.id, nodeStatus(n, cursor)])), [trace, cursor]);
  const prevStatuses = useRef<{ mode: boolean; map: Map<string, NodeStatus | null> } | null>(null);
  const [celebrating, setCelebrating] = useState<Set<string>>(() => new Set());
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(() => {
    const prev = prevStatuses.current;
    prevStatuses.current = { mode: replay, map: statuses };
    if (!prev || prev.mode !== replay) return;
    const done: string[] = [];
    for (const [id, s] of statuses) {
      const before = prev.map.get(id);
      if ((s === "completed" || s === "approved") && before !== s && before !== undefined) done.push(id);
    }
    if (!done.length) return;
    setCelebrating((cur) => new Set([...cur, ...done]));
    timers.current.push(
      setTimeout(() => {
        setCelebrating((cur) => {
          const next = new Set(cur);
          for (const id of done) next.delete(id);
          return next;
        });
      }, 2200),
    );
  }, [statuses, replay]);
  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  /* -------- live events for this case: pings while live, a counter while replaying -------- */
  const [pings, setPings] = useState<Set<string>>(() => new Set());
  const replayRef = useRef(replay);
  replayRef.current = replay;
  useLiveEvents((events) => {
    const recent = Date.now() - 10_000;
    const mine = events.filter((e) => e.caseId === caseId && e.at > recent);
    if (!mine.length) return;
    if (replayRef.current) {
      setMissed((m) => m + mine.length);
      return;
    }
    const ids = [...new Set(mine.map((e) => ("taskId" in e ? e.taskId : "")).filter(Boolean))];
    if (!ids.length) return;
    setPings((cur) => new Set([...cur, ...ids]));
    timers.current.push(
      setTimeout(() => {
        setPings((cur) => {
          const next = new Set(cur);
          for (const id of ids) next.delete(id);
          return next;
        });
      }, 900),
    );
  }, { unscoped: true });

  /* -------- scrolling: follow the replay head, stick to the bottom while live -------- */
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(false);
  const headY = layout.yOf(clockAt);
  const onScroll = () => {
    const el = scrollRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  };
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !replay || !playing) return;
    const y = headY + 70;
    if (y > el.scrollTop + el.clientHeight - 90 || y < el.scrollTop + 70) el.scrollTo({ top: Math.max(0, y - el.clientHeight * 0.6) });
  }, [headY, replay, playing]);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || replay) return;
    if (stick.current && el.scrollHeight > el.clientHeight) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [layout.height, replay]);

  /* -------- render -------- */
  if (!c && trace.nodes.length === 0 && trace.milestones.length === 0) {
    return (
      <div className={`ct-missing ${simulated ? "is-sim" : ""}`}>
        <Mascot id="intake" size={96} mood={source === "connecting" ? "working" : "alert"} />
        <div className="fl-section">Case {caseId}</div>
        <h1 className="fl-display" style={{ fontSize: 34 }}>
          {source === "connecting" ? "Connecting…" : "Case not found."}
        </h1>
        <Link href="/cases" className="fl-btn">
          <ArrowLeft size={14} /> Back to cases
        </Link>
      </div>
    );
  }

  const tasks = trace.nodes.filter((n) => n.kind === "task" && n.start <= cursor);
  const agents = new Set(tasks.map((n) => n.lane)).size;
  const openedAt = c?.openedAt || t0;
  const endedAt = c ? (c.resolvedAt && c.status === "resolved" && c.resolvedAt <= cursor ? c.resolvedAt : 0) : !trace.open && trace.last <= cursor ? trace.last : 0;
  const status = c?.status ?? (trace.open ? "in_progress" : "resolved");
  const statusAtCursor = replay ? (c?.resolvedAt && c.resolvedAt <= cursor ? "resolved" : "in_progress") : status;

  return (
    <div className={`ct-page ${simulated ? "is-sim" : ""}`}>
      <header className="ct-head">
        <div className="ct-head-main">
          <div className="ct-crumbs">
            <Link href="/cases" className="fl-btn quiet small">
              <ArrowLeft size={13} /> Cases
            </Link>
            <span className="fl-section">
              {caseId} · {c ? KIND_LABEL[caseKind(c)] : "Case"}
            </span>
            {c && <PriorityChip priority={c.priority} />}
            <StatusChip status={statusAtCursor} active={replay ? 0 : (c?.activeTasks ?? 0)} />
            {replay && <span className="fl-chip outline agent">Replay</span>}
          </div>
          <h1 className="fl-display ct-title">{c?.title ?? caseId}</h1>
          <div className="ct-meta fl-mono">
            <span>Opened {clock(openedAt)}</span>
            {/* While replaying, the scrubber readout carries the elapsed time. */}
            {endedAt ? <span>Took {shortDur(endedAt - openedAt)}</span> : replay ? null : <span>{shortDur(clockAt - openedAt)} so far</span>}
            <span>
              {agents} agent{agents === 1 ? "" : "s"}
            </span>
            {c?.exposureUsd ? <span>Exposure {fmtMoney(c.exposureUsd)}</span> : null}
          </div>
        </div>
        <div className="ct-head-actions">
          <button className="fl-btn small" onClick={startReplay} title="Replay this case from the first event">
            <Rewind size={13} /> Replay
          </button>
          <button className={`fl-btn small ${replay ? "" : "primary"}`} onClick={goLive} title="Show everything up to now">
            <Radio size={13} /> Live
            {replay && missed > 0 && <span className="ct-missed fl-mono">+{missed}</span>}
          </button>
          <Link className="fl-btn small" href={`/agents?case=${encodeURIComponent(caseId)}`} title="See this case on the agent graph">
            <Network size={13} /> Map
          </Link>
          <button className={`fl-btn small ${panel === "log" ? "primary" : ""}`} onClick={() => setPanel((p) => (p === "log" ? "closed" : "log"))}>
            <ListOrdered size={13} /> Log
          </button>
        </div>
      </header>

      <SideEffects trace={trace} cursor={cursor} onSelect={selectFromPanel} />

      <div className="ct-body">
        <div className="ct-stage">
          <Backdrop variant="glow" intensity={0.28} color={agentColor(trace.nodes.find((n) => n.kind === "task")?.lane ?? "intake")} />
          <div className={`ct-scroll ${panel !== "closed" ? "with-panel" : ""}`} ref={scrollRef} onScroll={onScroll}>
            <TraceGraph
              trace={trace}
              layout={layout}
              cursor={cursor}
              clockAt={clockAt}
              showHead={replay || trace.open}
              replay={replay}
              selected={selected}
              onSelect={onSelect}
              celebrating={celebrating}
              pings={pings}
            />
            {trace.nodes.length === 0 && (
              <div className="ct-waiting">
                <Mascot id="intake" size={56} mood="working" />
                <span>Not routed yet.</span>
              </div>
            )}
          </div>
          <aside className={`ct-panel ${panel !== "closed" ? "open" : ""}`} aria-hidden={panel === "closed"}>
            {panel === "node" && selectedNode ? (
              <NodeDetails trace={trace} node={selectedNode} cursor={cursor} onSelect={selectFromPanel} onClose={closePanel} />
            ) : panel === "log" ? (
              <CaseLog trace={trace} cursor={cursor} onSelect={selectFromPanel} onClose={closePanel} />
            ) : null}
          </aside>
        </div>
        <Scrubber
          trace={trace}
          t0={t0}
          tEnd={tEnd}
          at={replay ? replayAt : tEnd}
          replay={replay}
          playing={playing}
          speed={speed}
          onToggle={togglePlay}
          onSpeed={setSpeed}
          onSeek={(t) => {
            setPlaying(false);
            setReplayAt(t);
          }}
        />
      </div>
    </div>
  );
}

/* ------------------------------ ledger ------------------------------ */

const EFFECT_TONE: Record<string, string> = {
  active: "hot",
  held: "hot",
  released: "ok",
  proposed: "info",
  awaiting_approval: "hot",
  sent: "ok",
  paid: "ok",
  pending: "hot",
  approved: "ok",
  rejected: "bad",
  posted: "ok",
};

function SideEffects({ trace, cursor, onSelect }: { trace: Trace; cursor: number; onSelect: (id: string) => void }) {
  const items = trace.sideEffects
    .filter((s) => s.kind !== "forecast")
    .map((s) => ({ s, status: sideEffectAt(s, cursor) }))
    .filter((x) => x.status);
  return (
    <div className="ct-effects" aria-label="Ledger">
      <span className="fl-section ct-effects-label" style={{ "--accent": "var(--yellow)" } as CSSProperties}>
        Ledger
      </span>
      {items.length === 0 ? (
        <span className="fl-micro">{cursor === Infinity ? "None" : "None yet"}</span>
      ) : (
        <div className="ct-effects-list">
          {items.map(({ s, status }) => {
            const target = s.kind === "approval" ? `human:${String(s.row.id)}` : trace.nodeById.has(s.taskId) ? s.taskId : "";
            const body = (
              <>
                <span className="fl-micro">{s.kind}</span>
                <span className="ct-effect-label">{s.label}</span>
                <span className="fl-num">{fmtMoney(s.amount)}</span>
                <span className={`fl-chip outline ${EFFECT_TONE[status!] ?? ""} ct-mini-chip`}>{status!.replaceAll("_", " ")}</span>
              </>
            );
            return target ? (
              <button key={s.id} className="ct-effect fl-reveal" onClick={() => onSelect(target)} title={`${s.label} · ${clock(s.at)}`}>
                {body}
              </button>
            ) : (
              <span key={s.id} className="ct-effect fl-reveal" title={`${s.label} · ${clock(s.at)}`}>
                {body}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ------------------------------ scrubber ------------------------------ */

function Scrubber(p: {
  trace: Trace;
  t0: number;
  tEnd: number;
  at: number;
  replay: boolean;
  playing: boolean;
  speed: Speed;
  onToggle: () => void;
  onSpeed: (s: Speed) => void;
  onSeek: (t: number) => void;
}) {
  const span = Math.max(1, p.tEnd - p.t0);
  const progress = Math.min(1, Math.max(0, (p.at - p.t0) / span));
  const ticks = [
    ...p.trace.nodes.filter((n) => n.start >= p.t0).map((n) => ({ id: n.id, x: (n.start - p.t0) / span, color: agentColor(n.lane) })),
    // Lifecycle milestones: muted, they are context rather than work.
    ...p.trace.milestones.filter((m) => m.at >= p.t0).map((m) => ({ id: m.id, x: (m.at - p.t0) / span, color: "var(--text-mute)" })),
  ];
  return (
    <div className="ct-scrubber">
      <button className="fl-btn small ct-play" onClick={p.onToggle} aria-label={p.playing ? "Pause replay" : "Play replay"}>
        {p.playing ? <Pause size={13} /> : <Play size={13} />}
        {p.playing ? "Pause" : "Play"}
      </button>
      <div className="fl-tabs ct-speed" role="group" aria-label="Replay speed">
        {SPEEDS.map((s) => (
          <button key={s} className={`fl-tab ${p.speed === s ? "active" : ""}`} onClick={() => p.onSpeed(s)}>
            {s}×
          </button>
        ))}
      </div>
      <div className="ct-track">
        <div className="ct-track-rail">
          <i className="ct-track-fill" style={{ transform: `scaleX(${progress})` }} />
          {ticks.map((t) => (
            <i key={t.id} className="ct-track-tick" style={{ left: `${t.x * 100}%`, background: t.color }} />
          ))}
        </div>
        <input
          type="range"
          min={0}
          max={span}
          step={100}
          value={Math.round(p.at - p.t0)}
          onChange={(e) => p.onSeek(p.t0 + Number(e.target.value))}
          aria-label="Replay position"
          aria-valuetext={clock(p.at)}
        />
      </div>
      <span className="ct-readout fl-mono">
        {p.replay && (
          <>
            {timer(p.at - p.t0)} <span className="fl-mute">/ {timer(span)}</span>
          </>
        )}
      </span>
    </div>
  );
}
