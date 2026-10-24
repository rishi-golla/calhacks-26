"use client";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Position,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Menu, Plus, X } from "lucide-react";
import { num, str, timestamp } from "@/lib/data";
import { useLive } from "./live-provider";
import { MASCOT_IDS, Mascot } from "./mascot";
import { useClock } from "./ui";
import {
  FARM,
  STEPS,
  activeSteps,
  displayUrl,
  feedOf,
  latestCase,
  layout,
  linksFor,
  scope,
  stepById,
  targetUrl,
  type Step,
} from "./workflow-steps";
import "./workflow.css";

type Tab = { node_id: string; url: string; title: string };

function mascotOf(step: Step) {
  const owner = STEPS.find(
    (s) =>
      s.agent && s.operator === step.operator && MASCOT_IDS.includes(s.agent),
  );
  return step.agent && MASCOT_IDS.includes(step.agent)
    ? step.agent
    : owner?.agent;
}

function tabHost(url: string) {
  try {
    const host = new URL(url).host;
    return host.endsWith(":3001") ? "Live tab" : host;
  } catch {
    return "Opening…";
  }
}

type StepData = {
  step: Step;
  events: number;
  status: string;
  working: boolean;
  url: string;
  farm: boolean;
  tick: number;
  leaving: boolean;
  open: () => void;
  remove: () => void;
};

function Chrome({ url, status }: { url: string; status: string }) {
  return (
    <div className="wf-chrome">
      <span className="wf-lights">
        <i />
        <i />
        <i />
      </span>
      <span className="wf-url">{displayUrl(url)}</span>
      <span className={`wf-live ${status}`}>{status}</span>
    </div>
  );
}

function LiveFrame({ id, tick }: { id: string; tick: number }) {
  const [src, setSrc] = useState("");
  useEffect(() => {
    const img = new Image();
    img.onload = () => setSrc(img.src);
    img.src = `${FARM}/frame/${id}.jpg?t=${tick}`;
    return () => {
      img.onload = null;
    };
  }, [id, tick]);
  if (!src) return <p className="wf-empty">Opening browser…</p>;
  // eslint-disable-next-line @next/next/no-img-element
  return <img className="wf-shot" alt="" src={src} />;
}

const StepNode = memo(function StepNode({ data }: NodeProps<Node<StepData>>) {
  const { step, events, status, farm, tick } = data;
  const mascot = mascotOf(step);
  return (
    <div
      className={`wf-node ${data.leaving ? "leaving" : ""}`}
      onClick={data.open}
    >
      <Handle type="target" position={Position.Left} />
      <button
        className="wf-remove nodrag"
        aria-label={`Remove ${step.title}`}
        onClick={(e) => {
          e.stopPropagation();
          data.remove();
        }}
      >
        <X size={12} />
      </button>
      <div className="wf-head">
        {mascot && <Mascot id={mascot} size={34} working={data.working} />}
        <div>
          <span className="wf-agent">{step.operator}</span>
          <strong>{step.title}</strong>
          <small>{step.subtitle}</small>
        </div>
      </div>
      <div className="wf-browser">
        <Chrome url={data.url} status={status} />
        <div className="wf-screen">
          {farm ? (
            <LiveFrame id={step.id} tick={tick} />
          ) : (
            <p className="wf-empty">Browser offline</p>
          )}
        </div>
      </div>
      <div className="wf-foot">
        {tabHost(data.url)} · {events} events
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
});
const nodeTypes = { step: StepNode };

type Manual = Record<string, { add: string[]; remove: string[] }>;

export default function Workflow({
  caseId,
  onClearCase,
  onOpenMenu,
}: {
  caseId: string;
  onClearCase: () => void;
  onOpenMenu: () => void;
}) {
  const { data, state } = useLive();
  const now = useClock();
  const [openId, setOpenId] = useState("");
  const [manual, setManual] = useState<Manual>({});
  const [palette, setPalette] = useState(false);
  const [farm, setFarm] = useState(false);
  const [tick, setTick] = useState(0);
  const [tabs, setTabs] = useState<Record<string, Tab>>({});
  const [leaving, setLeaving] = useState<string[]>([]);
  const followed = caseId || latestCase(data);
  const scoped = useMemo(() => scope(data, followed), [data, followed]);
  const mine = manual[followed] ?? { add: [], remove: [] };
  const visible = useMemo(() => {
    const s = activeSteps(scoped);
    for (const id of mine.add) s.add(id);
    for (const id of mine.remove) s.delete(id);
    return s;
  }, [scoped, mine.add, mine.remove]);
  const links = useMemo(() => linksFor(visible, scoped), [visible, scoped]);
  const { pos, level } = useMemo(
    () => layout(visible, links),
    [visible, links],
  );
  const feeds = useMemo(
    () => Object.fromEntries(STEPS.map((s) => [s.id, feedOf(s, scoped)])),
    [scoped],
  );
  const urls = useMemo(
    () =>
      Object.fromEntries(
        STEPS.map((s) => [s.id, targetUrl(s.id, scoped, data, followed)]),
      ),
    [scoped, data, followed],
  );
  const edit = (id: string, op: "add" | "remove") =>
    setManual((m) => {
      const cur = m[followed] ?? { add: [], remove: [] };
      const other = op === "add" ? "remove" : "add";
      return {
        ...m,
        [followed]: {
          ...cur,
          [op]: [...cur[op].filter((x) => x !== id), id],
          [other]: cur[other].filter((x) => x !== id),
        },
      } as Manual;
    });
  const statusOf = (s: Step) => {
    if (s.agent && s.agent !== "ingest") {
      const rec = data.agent.find((r) => r.name === s.agent);
      if (!rec) return "offline";
      if (now - timestamp(rec.last_seen) > 60_000) return "stale";
      return str(rec.status, "idle").toLowerCase();
    }
    if (state !== "live" && state !== "fallback") return "offline";
    return feeds[s.id].length ? "live" : "idle";
  };
  const workingOf = (s: Step) => {
    const name = mascotOf(s);
    const rec = data.agent.find((r) => r.name === name);
    return (
      !!rec &&
      now - timestamp(rec.last_seen) <= 60_000 &&
      !["offline", "error", "stopped"].includes(
        str(rec.status).toLowerCase(),
      ) &&
      num(rec.active) > 0
    );
  };

  const sent = useRef(new Map<string, string>());
  const pending = useRef(new Set<string>());
  useEffect(() => {
    let alive = true;
    const sync = async () => {
      try {
        const r = await fetch(`${FARM}/sessions`, { cache: "no-store" });
        if (!r.ok) throw new Error(String(r.status));
        const list: Tab[] = await r.json();
        if (!alive) return;
        setFarm(true);
        setTabs(Object.fromEntries(list.map((t) => [t.node_id, t])));
        const have = new Set(list.map((t) => t.node_id));
        for (const { id } of STEPS) {
          const url = urls[id];
          if (pending.current.has(id)) continue;
          if (have.has(id) && sent.current.get(id) === url) continue;
          sent.current.set(id, url);
          pending.current.add(id);
          fetch(`${FARM}/sessions/${id}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
          })
            .catch(() => sent.current.delete(id))
            .finally(() => pending.current.delete(id));
        }
      } catch {
        if (alive) setFarm(false);
      }
    };
    sync();
    const timer = setInterval(sync, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [urls]);
  useEffect(() => {
    if (!farm) return;
    const timer = setInterval(() => setTick((t) => t + 1), 700);
    return () => clearInterval(timer);
  }, [farm]);

  const shown = useRef(new Set<string>());
  useEffect(() => {
    const gone = [...shown.current].filter((id) => !visible.has(id));
    shown.current = new Set(visible);
    if (!gone.length) return;
    const start = setTimeout(() => setLeaving(gone), 0);
    const end = setTimeout(() => setLeaving([]), 450);
    return () => {
      clearTimeout(start);
      clearTimeout(end);
    };
  }, [visible]);

  useEffect(() => {
    if (!openId) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpenId("");
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openId]);

  const rendered = [...visible, ...leaving.filter((id) => !visible.has(id))];
  const lastPos = useRef(new Map<string, { x: number; y: number }>());
  for (const [id, p] of pos) lastPos.current.set(id, p);
  const nodes: Node<StepData>[] = rendered.map((id) => {
    const step = stepById(id)!;
    return {
      id,
      type: "step",
      position: pos.get(id) ?? lastPos.current.get(id) ?? { x: 0, y: 0 },
      data: {
        step,
        events: feeds[id].length,
        status: statusOf(step),
        working: workingOf(step),
        url: tabs[id]?.url || urls[id],
        farm,
        tick,
        leaving: !visible.has(id),
        open: () => setOpenId(id),
        remove: () => edit(id, "remove"),
      },
    };
  });
  const edges: Edge[] = links.map(({ from, to }) => {
    const back = (level.get(to) ?? 0) <= (level.get(from) ?? 0);
    return {
      id: `${from}-${to}`,
      source: from,
      target: to,
      animated: back || feeds[from].length > 0,
      className: back ? "wf-edge loop" : "wf-edge",
      markerEnd: {
        type: MarkerType.ArrowClosed,
        width: 14,
        height: 14,
        color: "#b5b5ba",
      },
    };
  });
  const open = stepById(openId);
  const caseTitle = str(
    data.case.find((r) => r.case_id === followed)?.title || followed,
  ).replace(/^\[W2 fixture\]\s*/, "");
  const hidden = STEPS.filter((s) => !visible.has(s.id));
  return (
    <div className="wf">
      <header className="wf-top">
        <button
          className="wf-menu"
          aria-label="Open navigation"
          onClick={onOpenMenu}
        >
          <Menu size={18} />
        </button>
        <strong>Workflow</strong>
        {followed &&
          (caseId ? (
            <button className="wf-chip" onClick={onClearCase}>
              {caseTitle}
              <X size={12} />
            </button>
          ) : (
            <span className="wf-chip following">
              <i /> Following latest case · {caseTitle}
            </span>
          ))}
        <span className="wf-sub">
          {visible.size} of {STEPS.length} steps in this run
        </span>
        <span className="wf-spacer" />
        <span className={`wf-conn ${farm ? "live" : ""}`}>
          {farm ? "Browsers live" : "Browsers offline"}
        </span>
        <span className={`wf-conn ${state}`}>
          {state === "live" ? "Data live" : `Data ${state}`}
        </span>
        <div className="wf-add-wrap">
          <button
            className="wf-add"
            onClick={() => setPalette((p) => !p)}
            disabled={!hidden.length}
          >
            <Plus size={14} /> Add step
          </button>
          {palette && hidden.length > 0 && (
            <div className="wf-palette">
              {hidden.map((s) => (
                <button
                  key={s.id}
                  onClick={() => {
                    edit(s.id, "add");
                    setPalette(false);
                  }}
                >
                  {mascotOf(s) && <Mascot id={mascotOf(s)!} size={24} />}
                  <span>
                    <b>{s.title}</b>
                    <small>{s.operator}</small>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </header>
      <ReactFlow
        key={followed}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
        minZoom={0.2}
        nodesConnectable={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={18}
          size={1.4}
          color="#cfcfd4"
        />
        <Controls showInteractive={false} position="bottom-left" />
      </ReactFlow>
      {open && (
        <div className="wf-overlay" onClick={() => setOpenId("")}>
          <div
            className="wf-window"
            role="dialog"
            aria-label={open.title}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="wf-window-bar">
              <Chrome
                url={tabs[open.id]?.url || urls[open.id]}
                status={statusOf(open)}
              />
              <button aria-label="Close" onClick={() => setOpenId("")}>
                <X size={16} />
              </button>
            </div>
            <div className="wf-window-body">
              <section className="wf-stream">
                {farm ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    className="wf-live-view"
                    alt={`${open.title} live browser`}
                    src={`${FARM}/stream/${open.id}`}
                  />
                ) : (
                  <p className="wf-empty">
                    Browser service offline. Start it with{" "}
                    <code>make browsers</code>.
                  </p>
                )}
              </section>
              <aside className="wf-info">
                <dl>
                  <dt>Step</dt>
                  <dd>{open.title}</dd>
                  <dt>Agent</dt>
                  <dd className="wf-agent-dd">
                    {mascotOf(open) && (
                      <Mascot
                        id={mascotOf(open)!}
                        size={20}
                        working={workingOf(open)}
                      />
                    )}
                    {open.operator}
                  </dd>
                  <dt>Status</dt>
                  <dd>{statusOf(open)}</dd>
                  <dt>Events in this case</dt>
                  <dd>{feeds[open.id].length}</dd>
                  <dt>Receives from</dt>
                  <dd>
                    {links
                      .filter((l) => l.to === open.id)
                      .map((l) => stepById(l.from)?.title)
                      .join(", ") || "—"}
                  </dd>
                  <dt>Sends to</dt>
                  <dd>
                    {links
                      .filter((l) => l.from === open.id)
                      .map((l) => stepById(l.to)?.title)
                      .join(", ") || "—"}
                  </dd>
                  <dt>Page</dt>
                  <dd>{tabs[open.id]?.title || "—"}</dd>
                </dl>
              </aside>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
