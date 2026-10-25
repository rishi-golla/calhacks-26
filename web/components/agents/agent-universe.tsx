"use client";
/**
 * Universal agent view (/agents).
 *
 * A living map of the back office: nine agent mascots plus Intake (and the
 * Human controller when an approval is waiting), with every dispatch and
 * delegation flying as a document packet between them. Work is aggregated
 * per node, never drawn one-workflow-per-case, so the map stays readable at
 * hundreds of concurrent cases. Packets and tickers are imperative
 * (`PacketEngine`); React renders the nodes, edges and rail.
 *
 * Learning is drawn quietly: a muted light-bulb count on each agent that has
 * lessons, a light-bulb packet for every new lesson, a brief glow on the count
 * whenever a task recalls one, and gold lines from Settlement Quality Review
 * to its learners only while one of them is focused (lib/lessons).
 */
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type RefObject } from "react";
import { ArrowUpRight, X, Zap } from "lucide-react";
import Backdrop from "@/components/fx/backdrop";
import { useLiveEvents, useOps } from "@/components/data-provider";
import { moodForStatus, type Mood } from "@/components/mascot";
import { AGENT_IDS, AGENT_META, isAgentId, type AgentId, type NodeId } from "@/lib/agents";
import { timestamp } from "@/lib/data";
import { lessonIndex } from "@/lib/lessons";
import { intakeArriving, type LiveEvent } from "@/lib/ops";
import { AgentNode, type NodeTone, type NodeView } from "./agent-node";
import { AgentPanel } from "./agent-panel";
import { CaseRail, type RailMetrics } from "./case-rail";
import { Edges, type LessonEdge } from "./edges";
import { STAGE_H, STAGE_W, setStretch } from "./layout";
import { casePath, edgeTraffic, matchesTab, packetsFor, shortCaseId, type KindTab } from "./model";
import { PacketEngine } from "./packet-engine";
import "./agents.css";

const STALE_MS = 20_000;
const WINDOW_MS = 5 * 60_000;
const SUCCESS_MS = 1_500;
const ALERT_MS = 3_200;

/* ---------------------------- transient moods ---------------------------- */

type Transient = Partial<Record<NodeId, Mood>>;

/** Short-lived mood overrides (success hop, failure alert) driven by events. */
function useTransientMoods() {
  const [moods, setMoods] = useState<Transient>({});
  const active = useRef(new Map<NodeId, Mood>());
  const timers = useRef(new Map<NodeId, number>());
  const lastSuccess = useRef(new Map<NodeId, number>());
  const trigger = useCallback((updates: [NodeId, Mood][]) => {
    if (!updates.length) return;
    const now = performance.now();
    let changed = false;
    for (const [id, mood] of updates) {
      const current = active.current.get(id);
      if (mood === "success") {
        // Never cut short an alert or a hop in progress; leave a beat between hops.
        if (current || now - (lastSuccess.current.get(id) ?? -1e9) < SUCCESS_MS + 900) continue;
        lastSuccess.current.set(id, now);
      }
      active.current.set(id, mood);
      changed = true;
      window.clearTimeout(timers.current.get(id));
      timers.current.set(
        id,
        window.setTimeout(
          () => {
            active.current.delete(id);
            timers.current.delete(id);
            setMoods(Object.fromEntries(active.current));
          },
          mood === "alert" ? ALERT_MS : SUCCESS_MS,
        ),
      );
    }
    if (changed) setMoods(Object.fromEntries(active.current));
  }, []);
  useEffect(() => {
    const map = timers.current;
    return () => {
      for (const t of map.values()) window.clearTimeout(t);
    };
  }, []);
  return [moods, trigger] as const;
}

/* ------------------------------ stage fit ------------------------------ */

type Fit = { s: number; x: number; y: number; ts: number; stretch: number };

function useStageFit(ref: RefObject<HTMLDivElement | null>) {
  const [fit, setFit] = useState<Fit>({ s: 0.7, x: 0, y: 64, ts: 1.4, stretch: 1 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const w = el.clientWidth;
      const h = el.clientHeight;
      const top = 70;
      const bottom = 62;
      const availH = h - top - bottom;
      const s = Math.max(0.35, Math.min((w - 40) / STAGE_W, availH / STAGE_H, 1.3));
      // Spare height spreads the rows instead of leaving an empty band.
      const stretch = Math.min(1.4, Math.max(1, availH / s / STAGE_H));
      setStretch(stretch);
      setFit({
        s,
        x: (w - STAGE_W * s) / 2,
        y: top + Math.max(0, (availH - STAGE_H * stretch * s) / 2),
        ts: Math.min(1.7, Math.max(1, 1 / s)),
        stretch: Math.round(stretch * 100) / 100,
      });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return fit;
}

/* ------------------------------ helpers ------------------------------ */

const toneFor = (status: string, transient: Mood | undefined): NodeTone =>
  transient === "alert" || status === "error" ? "alert" : status === "working" ? "working" : status === "idle" ? "idle" : "sleeping";

const docName = (ref: string) => (ref.split(":")[1] ?? ref).split("#")[0];

/* ------------------------------ component ------------------------------ */

export default function AgentUniverse() {
  const { snapshot, fullSnapshot, cases, caseList, workloads, ops, attention, now, simulated, sim } = useOps();
  const [engine] = useState(() => new PacketEngine());
  const map = useRef<HTMLDivElement>(null);
  const layer = useRef<HTMLDivElement>(null);
  const fit = useStageFit(map);
  const [transient, trigger] = useTransientMoods();
  const [selectedCase, setSelectedCase] = useState("");
  const [tab, setTab] = useState<KindTab>("all");
  // Deep link from the case trace: /agents?case=<id> pre-selects that case.
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("case");
    if (id) setSelectedCase(id);
  }, []);
  const [hover, setHover] = useState<NodeId | null>(null);
  const [pinned, setPinned] = useState<NodeId | null>(null);
  const hoverTimer = useRef(0);

  useEffect(() => {
    if (layer.current) engine.attach(layer.current);
    return () => engine.detach();
  }, [engine]);

  /* ---------- derived ---------- */

  const traffic = useMemo(() => [...edgeTraffic(snapshot, cases, now).values()], [snapshot, cases, now]);
  const handoffs5m = useMemo(() => traffic.reduce((s, e) => s + (e.from === "human" || e.to === "human" ? 0 : e.recent), 0), [traffic]);

  const toolCalls5m = useMemo(() => {
    let n = 0;
    for (const e of snapshot.audit_event) if (e.kind === "tool_call" && e.reason_code === "tool_completed" && now - timestamp(e.ts) <= WINDOW_MS) n++;
    return n;
  }, [snapshot.audit_event, now]);

  const liveCalls = useMemo(() => snapshot.call.filter((c) => c.status === "ringing" || c.status === "in_progress").length, [snapshot.call]);

  const selected = selectedCase ? cases.get(selectedCase) : undefined;
  const path = useMemo(() => (selected ? casePath(snapshot, selected) : null), [snapshot, selected]);

  const liveAgents = useMemo(() => {
    const out = new Map<string, Set<AgentId>>();
    for (const id of AGENT_IDS)
      for (const t of workloads[id].activeTasks) {
        const set = out.get(t.caseId);
        if (set) set.add(id);
        else out.set(t.caseId, new Set([id]));
      }
    return out;
  }, [workloads]);

  // Learning outlives "Start fresh": lessons, their source cases and recall counts read the unscoped tables.
  const { task, agent_memory, review, instruction_version } = fullSnapshot;
  const lessons = useMemo(() => lessonIndex({ task, agent_memory, review, instruction_version }), [task, agent_memory, review, instruction_version]);
  const lessonEdges = useMemo<LessonEdge[]>(
    () => [...lessons.byAgent.entries()].filter(([agent]) => agent !== "validator").map(([agent, list]) => ({ agent, count: list.length })),
    [lessons],
  );
  // Read by the live-event handler, which only needs the latest texts.
  const lessonText = useRef(new Map<string, string>());
  useEffect(() => {
    lessonText.current = new Map(lessons.all.map((l) => [l.id, l.text]));
  }, [lessons]);

  const pendingApprovals = useMemo(() => snapshot.approval.filter((a) => !a.decision || a.decision === "pending"), [snapshot.approval]);

  // Packet visibility follows the case filter and the selected case.
  useEffect(() => {
    engine.setVisibility((caseId) => {
      if (selectedCase) return caseId === selectedCase ? "show" : "dim";
      // Lessons whose source case is not on the board (none, or before Start fresh) still fly.
      if (!caseId || !cases.has(caseId)) return "show";
      if (tab === "all") return "show";
      const c = cases.get(caseId);
      return c && matchesTab(c, tab) ? "show" : "hide";
    });
  }, [engine, selectedCase, tab, cases]);

  /* ---------- live events → packets, moods, tickers ---------- */

  useLiveEvents((events: LiveEvent[]) => {
    const wallNow = Date.now();
    engine.emit(packetsFor(events, cases, wallNow));
    const moods: [NodeId, Mood][] = [];
    for (const e of events) {
      if (e.at && wallNow - e.at > STALE_MS) continue;
      switch (e.type) {
        case "task_finished":
          if (!isAgentId(e.agent)) break;
          if (e.state === "completed") {
            moods.push([e.agent, "success"]);
            engine.ticker(e.agent, `✓ ${(e.outcome || "done").replaceAll("_", " ")}`);
          } else if (e.state === "failed") {
            moods.push([e.agent, "alert"]);
            engine.ticker(e.agent, "✕ failed · retry queued");
            engine.pulse(e.agent, true);
          } else if (e.state === "input-required") engine.ticker(e.agent, "⧗ awaiting approval");
          break;
        case "tool_call":
          if (isAgentId(e.agent) && e.status === "started") engine.ticker(e.agent, `› ${e.name}`);
          break;
        case "doc_read":
          if (isAgentId(e.agent)) engine.ticker(e.agent, `▤ ${docName(e.docRef)}`);
          break;
        case "case_opened":
          engine.ticker("intake", `+ ${e.caseId}`);
          break;
        case "lesson":
          if (isAgentId(e.agent)) engine.ticker("validator", `💡 lesson → ${AGENT_META[e.agent].nick}`, "lesson");
          break;
        case "lesson_recalled":
          if (isAgentId(e.agent)) {
            const first = lessonText.current.get(e.lessonIds[0]);
            engine.glowLesson(e.agent);
            // Recalls are routine (most tasks load a lesson), so they get a plain ticker line, not the gold one.
            engine.ticker(e.agent, `💡 recalled ${e.lessonIds.length > 1 ? `${e.lessonIds.length} lessons` : first ? `“${first.slice(0, 48)}${first.length > 48 ? "…" : ""}”` : "a lesson"}`);
          }
          break;
        case "dispatch":
          if (isAgentId(e.to)) engine.ticker("intake", `→ ${AGENT_META[e.to].nick}`);
          break;
        case "call":
          engine.ticker("voice_desk", `☎ ${e.status.replaceAll("_", " ")}`);
          break;
        case "approval":
          if (e.decision === "pending") engine.ticker("human", "⧗ approval requested");
          else {
            moods.push(["human", "success"]);
            engine.ticker("human", `✓ ${e.decision}`);
          }
          break;
      }
    }
    trigger(moods);
  });

  /* ---------- interaction ---------- */

  const onHover = useCallback((id: NodeId | null) => {
    window.clearTimeout(hoverTimer.current);
    hoverTimer.current = window.setTimeout(() => setHover(id), id ? 160 : 280);
  }, []);
  const holdHover = useCallback(() => window.clearTimeout(hoverTimer.current), []);
  const onSelectNode = useCallback((id: NodeId) => setPinned((p) => (p === id ? null : id)), []);
  const onChip = useCallback((caseId: string) => setSelectedCase((c) => (c === caseId ? "" : caseId)), []);
  useEffect(() => () => window.clearTimeout(hoverTimer.current), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setPinned(null);
      setHover(null);
      setSelectedCase("");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const panelId = pinned ?? hover;
  const focus = panelId;

  /* ---------- node views ---------- */

  const chipsFor = (ids: string[], max = 3) => {
    const visible = ids.filter((id) => {
      const c = cases.get(id);
      return !c || matchesTab(c, tab);
    });
    return {
      chips: visible
        .slice(0, max)
        .map((id) => `${id}~${cases.get(id)?.priority ?? "P3"}`)
        .join("|"),
      more: Math.max(0, visible.length - max),
    };
  };

  const views: NodeView[] = [];
  {
    const awaiting = caseList.filter((c) => c.status === "open");
    const arriving = intakeArriving(caseList, now);
    views.push({
      id: "intake",
      mood: transient.intake ?? (arriving ? "working" : "idle"),
      tone: arriving ? "working" : "idle",
      counts: `${awaiting.length}|new`,
      ...chipsFor(awaiting.map((c) => c.caseId)),
    });
  }
  for (const id of AGENT_IDS) {
    const w = workloads[id];
    const t = transient[id];
    const talking = id === "voice_desk" && liveCalls > 0;
    const mood: Mood = t ?? (talking ? "talking" : moodForStatus(w.status));
    const caseIds = [...w.activeTasks].sort((a, b) => b.startedAt - a.startedAt).map((x) => x.caseId);
    views.push({
      id,
      lessons: id === "validator" ? lessons.all.filter((l) => l.agent !== "validator").length : (lessons.byAgent.get(id)?.length ?? 0),
      mood,
      bubble: mood === "talking" ? (liveCalls === 1 ? "On a call" : `${liveCalls} calls live`) : undefined,
      tone: toneFor(w.status, t),
      counts: `${w.active}|active|${w.done.toLocaleString()}|done${w.queued > 0 ? `|${w.queued}|queued` : ""}${w.stalled > 0 ? `|${w.stalled}|stalled` : ""}`,
      ...chipsFor([...new Set(caseIds)]),
    });
  }
  const showHuman = pendingApprovals.length > 0 || path?.nodes.has("human") || panelId === "human";
  if (showHuman) {
    const decided = snapshot.approval.length - pendingApprovals.length;
    views.push({
      id: "human",
      mood: transient.human ?? (pendingApprovals.length ? "alert" : "idle"),
      tone: pendingApprovals.length ? "alert" : "idle",
      counts: `${pendingApprovals.length}|waiting|${decided}|done`,
      ...chipsFor([...new Set(pendingApprovals.map((a) => String(a.case_id ?? "")))], 2),
    });
  }

  const metrics = useMemo<RailMetrics>(
    () => ({
      active: ops.casesActive,
      exceptions: attention.length,
      handoffs: handoffs5m,
      toolCalls: toolCalls5m,
    }),
    [ops, attention.length, handoffs5m, toolCalls5m],
  );

  const stageStyle = {
    height: STAGE_H * fit.stretch,
    transform: `translate(${fit.x.toFixed(1)}px, ${fit.y.toFixed(1)}px) scale(${fit.s.toFixed(4)})`,
    "--ts": fit.ts.toFixed(3),
  } as CSSProperties;

  return (
    <div className={`au ${simulated ? "simulated" : ""} ${selected ? "au-has-case" : ""}`}>
      <section className="au-map" ref={map} aria-label="Agent universe map">
        <Backdrop variant="field" intensity={0.5} />

        <div className="au-stage" style={stageStyle}>
          <Edges stats={traffic} focus={focus} path={path?.edges ?? null} hideHuman={!showHuman} stretch={fit.stretch} lessons={lessonEdges} />
          {views.map((v) => (
            <AgentNode
              key={v.id}
              {...v}
              engine={engine}
              stretch={fit.stretch}
              state={path ? (path.nodes.has(v.id) ? "on" : "dim") : ""}
              focus={focus === v.id}
              order={isAgentId(v.id) ? (path?.order.get(v.id) ?? 0) : 0}
              selectedCase={selectedCase}
              onHover={onHover}
              onSelect={onSelectNode}
              onCase={onChip}
            />
          ))}
          <div className="au-packets" ref={layer} />
        </div>

        {selected && (
          <div className="au-casebar" role="status">
            <span className="fl-mono" style={{ fontSize: 12 }} title={selected.caseId}>
              {shortCaseId(selected.caseId)}
            </span>
            <span className="route">
              {selected.agents.map((a, i) => (
                <span key={a} style={{ display: "inline-flex", gap: 6 }}>
                  {i > 0 && <i>→</i>}
                  <span style={{ color: AGENT_META[a].color }}>{AGENT_META[a].nick}</span>
                </span>
              ))}
              {!selected.agents.length && "waiting for dispatch"}
            </span>
            <Link href={`/cases/${encodeURIComponent(selected.caseId)}`} className="fl-btn small">
              Trace <ArrowUpRight size={12} />
            </Link>
            <button className="fl-btn quiet small" onClick={() => setSelectedCase("")} aria-label="Clear selection">
              <X size={13} />
            </button>
          </div>
        )}

        {simulated && (
          <div className="au-simbar" aria-label="Simulation controls">
            <span>Rate</span>
            <input
              type="range"
              min={0}
              max={120}
              step={1}
              value={sim.ratePerMinute}
              onChange={(e) => sim.setRate(Number(e.target.value))}
              aria-label="Cases per minute"
              title={`${sim.ratePerMinute} cases / min`}
            />
            <button className="fl-btn small" onClick={() => sim.inject("detention_unsupported")}>
              <Zap size={11} /> Detention
            </button>
            <button className="fl-btn small" onClick={() => sim.inject("bank_change")}>
              <Zap size={11} /> Bank change
            </button>
            <button className="fl-btn small" onClick={() => sim.inject()}>
              <Zap size={11} /> Random
            </button>
          </div>
        )}
      </section>

      <section className="au-rail" aria-label="Operations and cases">
        <CaseRail caseList={caseList} liveAgents={liveAgents} selectedCase={selectedCase} onSelect={setSelectedCase} tab={tab} onTab={setTab} metrics={metrics} />
        {panelId && (
          <AgentPanel
            key={panelId}
            id={panelId}
            pinned={pinned === panelId}
            mood={views.find((v) => v.id === panelId)?.mood ?? "idle"}
            snapshot={snapshot}
            workload={isAgentId(panelId) ? workloads[panelId] : undefined}
            lessons={lessons}
            cases={cases}
            now={now}
            onClose={() => {
              setPinned(null);
              setHover(null);
            }}
            onPin={() => setPinned(panelId)}
            onCase={onChip}
            onEnter={holdHover}
            onLeave={() => !pinned && onHover(null)}
          />
        )}
      </section>
    </div>
  );
}
