"use client";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  ArrowLeft,
  Check,
  ChevronLeft,
  ChevronRight,
  Lock,
  Menu,
  Plus,
  X,
} from "lucide-react";
import { money, str, timestamp } from "@/lib/data";
import {
  useApprovalDecision,
  type ApprovalDecision,
} from "./approval-decision";
import { useLive } from "./live-provider";
import { MASCOT_IDS, Mascot } from "./mascot";
import { useClock } from "./ui";
import {
  FARM,
  STEPS,
  activeSteps,
  feedOf,
  latestCase,
  layout,
  linksFor,
  scope,
  stepById,
  targetUrl,
  type Line,
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

function urlParts(value: string) {
  try {
    const url = new URL(value);
    return { host: url.host, path: url.pathname };
  } catch {
    return { host: "", path: "" };
  }
}

function relativeAge(ts: number, now: number) {
  if (!ts) return "";
  const seconds = Math.max(0, Math.floor((now - ts) / 1000));
  if (seconds < 10) return "now";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

type StepState = "working" | "need" | "idle";

type StepData = {
  step: Step;
  host: string;
  path: string;
  doing: string;
  when: string;
  state: StepState;
  working: boolean;
  farm: boolean;
  tick: number;
  leaving: boolean;
  open: () => void;
  remove: () => void;
};

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
  const { step, farm, tick } = data;
  const mascot = mascotOf(step);
  return (
    <div
      className={`wf-node ${data.state} ${data.leaving ? "leaving" : ""}`}
      onClick={data.open}
    >
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
      <div className="wf-win">
        <div className="wf-bar">
          <span className="wf-dots">
            <i />
            <i />
            <i />
          </span>
          <span className="wf-url">
            <Lock className="wf-lock" size={9} />
            <span className="wf-address">
              <b>{data.host}</b>
              {data.path}
            </span>
          </span>
        </div>
        <div className="wf-screen">
          {farm ? (
            <LiveFrame id={step.id} tick={tick} />
          ) : (
            <p className="wf-empty">Browser offline</p>
          )}
        </div>
      </div>
      <div className="wf-meta">
        {mascot ? (
          <Mascot id={mascot} size={26} working={data.working} />
        ) : (
          <i className={`wf-status-dot ${data.state}`} aria-hidden="true" />
        )}
        <div className="wf-details">
          <div className="wf-name">{step.operator}</div>
          <div className="wf-doing">{data.doing}</div>
        </div>
        {data.state === "need" ? (
          <button
            className="wf-review nodrag"
            onClick={(e) => {
              e.stopPropagation();
              data.open();
            }}
          >
            Review
          </button>
        ) : (
          <span className="wf-when">{data.when}</span>
        )}
      </div>
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
  const { data, state, mutate } = useLive();
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
  const { pos } = useMemo(() => layout(visible, links), [visible, links]);
  const orderedIds = useMemo(
    () =>
      [...visible].sort((left, right) => {
        const leftPos = pos.get(left) ?? { x: 0, y: 0 };
        const rightPos = pos.get(right) ?? { x: 0, y: 0 };
        return (
          leftPos.y - rightPos.y ||
          leftPos.x - rightPos.x ||
          STEPS.findIndex((step) => step.id === left) -
            STEPS.findIndex((step) => step.id === right)
        );
      }),
    [visible, pos],
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
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpenId("");
        return;
      }
      if (
        (event.key !== "ArrowLeft" && event.key !== "ArrowRight") ||
        event.target instanceof HTMLInputElement ||
        event.target instanceof HTMLTextAreaElement
      )
        return;
      const currentIndex = orderedIds.indexOf(openId);
      const nextId =
        orderedIds[currentIndex + (event.key === "ArrowLeft" ? -1 : 1)];
      if (nextId) {
        event.preventDefault();
        setOpenId(nextId);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openId, orderedIds]);

  const measured = useRef(new Map<string, { width: number; height: number }>());
  const onNodesChange = useCallback((changes: NodeChange<Node<StepData>>[]) => {
    for (const c of changes)
      if (c.type === "dimensions" && c.dimensions)
        measured.current.set(c.id, c.dimensions);
  }, []);

  const rendered = [...visible, ...leaving.filter((id) => !visible.has(id))];
  const lastPos = useRef(new Map<string, { x: number; y: number }>());
  for (const [id, p] of pos) lastPos.current.set(id, p);
  const nodeData = new Map<string, StepData>();
  for (const id of rendered) {
    const step = stepById(id)!;
    const status = statusOf(step);
    const needsApproval = scoped.approval.some(
      (approval) =>
        approval.decision === "pending" &&
        (step.id === "human" ||
          (Boolean(step.agent) && approval.requested_by === step.agent)),
    );
    let newest: Line | undefined;
    for (const item of feeds[id])
      if (!newest || item.ts > newest.ts) newest = item;
    const url = tabs[id]?.url || urls[id];
    const { host, path } = urlParts(url);
    const owner = STEPS.find((s) => s.agent && s.agent === mascotOf(step));
    const rec = owner && data.agent.find((r) => r.name === owner.agent);
    const working =
      !!rec &&
      now - timestamp(rec.last_seen) <= 60_000 &&
      !["offline", "error", "stopped"].includes(
        str(rec.status).toLowerCase(),
      ) &&
      scoped.task.some(
        (t) => t.agent === owner.agent && str(t.a2a_state) === "working",
      );
    const nodeState: StepState = needsApproval
      ? "need"
      : ["live", "online", "working", "busy"].includes(status)
        ? "working"
        : "idle";
    const doing = needsApproval
      ? "Waiting for your approval"
      : `${newest?.text || step.subtitle}${
          status === "offline" || status === "stale" ? ` · ${status}` : ""
        }`;
    nodeData.set(id, {
      step,
      host,
      path,
      doing,
      when: newest ? relativeAge(newest.ts, now) : "",
      state: nodeState,
      working,
      farm,
      tick,
      leaving: !visible.has(id),
      open: () => setOpenId(id),
      remove: () => edit(id, "remove"),
    });
  }
  const workingAgents = [...visible].filter((id) => {
    const step = stepById(id)!;
    return Boolean(step.agent) && nodeData.get(id)?.state === "working";
  }).length;
  const allLive = farm && state === "live";
  const statusText = allLive
    ? `Live · ${workingAgents} agents`
    : [!farm ? "Browsers offline" : "", state !== "live" ? `Data ${state}` : ""]
        .filter(Boolean)
        .join(" · ");
  const nodes: Node<StepData>[] = rendered.map((id) => {
    return {
      id,
      type: "step",
      position: pos.get(id) ?? lastPos.current.get(id) ?? { x: 0, y: 0 },
      measured: measured.current.get(id),
      data: nodeData.get(id)!,
    };
  });
  const open = stepById(openId);
  const openApproval = open
    ? scoped.approval.find(
        (approval) =>
          approval.decision === "pending" &&
          (open.id === "human" ||
            (Boolean(open.agent) && approval.requested_by === open.agent)),
      )
    : undefined;
  const approvalDecision = useApprovalDecision({
    approval: openApproval,
    approvals: data.approval,
    mutate,
  });
  const openIndex = orderedIds.indexOf(openId);
  const moveOpenStep = (offset: number) => {
    const nextId = orderedIds[openIndex + offset];
    if (nextId) setOpenId(nextId);
  };
  const submitApproval = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const submitter = (event.nativeEvent as SubmitEvent)
      .submitter as HTMLButtonElement | null;
    if (submitter?.value === "approved" || submitter?.value === "rejected")
      void approvalDecision.decide(submitter.value as ApprovalDecision);
  };
  const caseTitle = followed
    ? str(
        data.case.find((r) => r.case_id === followed)?.title || followed,
      ).replace(/^\[W2 fixture\]\s*/, "")
    : "";
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
        <strong>{caseTitle || "Workflow"}</strong>
        {followed &&
          (caseId ? (
            <button
              className="wf-chip"
              aria-label={`Clear case ${followed}`}
              onClick={onClearCase}
            >
              <span className="wf-case-id">{followed}</span>
              <X size={12} />
            </button>
          ) : (
            <span className="wf-sub">{followed}</span>
          ))}
        <span className="wf-spacer" />
        <span className={`wf-status-chip ${allLive ? "live" : ""}`}>
          <i />
          {statusText}
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
        edges={[]}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        fitView
        fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
        minZoom={0.2}
        nodesConnectable={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={20}
          size={1.2}
          color="#d4d4d8"
        />
        <Controls showInteractive={false} position="bottom-left" />
      </ReactFlow>
      {open && (
        <div className="wf-overlay">
          <div
            className="wf-c2"
            role="dialog"
            aria-modal="true"
            aria-label={`${open.title} live browser`}
          >
            <header className="wf-c2-topbar">
              <button
                type="button"
                className="wf-c2-back"
                aria-label="Back to workflow"
                onClick={() => setOpenId("")}
              >
                <ArrowLeft size={18} />
              </button>
              <div className="wf-c2-current">
                <strong>
                  {mascotOf(open) && (
                    <Mascot
                      id={mascotOf(open)!}
                      size={18}
                      working={nodeData.get(open.id)?.working}
                    />
                  )}
                  {open.title}
                </strong>
                <span>{followed}</span>
              </div>
              <nav className="wf-c2-steps" aria-label="Steps in this case">
                {orderedIds.map((id, index) => {
                  const step = stepById(id)!;
                  const completed = index < openIndex;
                  const current = index === openIndex;
                  return (
                    <button
                      key={id}
                      type="button"
                      className={`wf-c2-step-chip ${completed ? "complete" : ""} ${current ? "current" : ""}`}
                      aria-current={current ? "step" : undefined}
                      title={step.title}
                      onClick={() => setOpenId(id)}
                    >
                      {completed ? (
                        <Check size={13} aria-hidden="true" />
                      ) : current ? (
                        <i className="wf-c2-step-pulse" aria-hidden="true" />
                      ) : (
                        <i className="wf-c2-step-future" aria-hidden="true" />
                      )}
                      <span>{step.title}</span>
                    </button>
                  );
                })}
              </nav>
              <button
                type="button"
                className="wf-c2-close"
                aria-label="Close browser view"
                onClick={() => setOpenId("")}
              >
                <X size={18} />
              </button>
            </header>

            <div className="wf-c2-browser">
              <div className="wf-c2-tab-strip">
                <div
                  className="wf-c2-tab"
                  title={tabs[open.id]?.title || open.title}
                >
                  <i aria-hidden="true" />
                  <span>{tabs[open.id]?.title || open.title}</span>
                </div>
              </div>
              <div className="wf-c2-address">
                <Lock size={13} aria-hidden="true" />
                <span>{tabs[open.id]?.url || urls[open.id]}</span>
              </div>
              <section
                className="wf-c2-screen"
                aria-label="Live browser stream"
              >
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
            </div>

            <button
              type="button"
              className="wf-c2-side-control previous"
              aria-label="Previous step"
              disabled={openIndex <= 0}
              onClick={() => moveOpenStep(-1)}
            >
              <ChevronLeft size={21} />
            </button>
            <button
              type="button"
              className="wf-c2-side-control next"
              aria-label="Next step"
              disabled={openIndex < 0 || openIndex >= orderedIds.length - 1}
              onClick={() => moveOpenStep(1)}
            >
              <ChevronRight size={21} />
            </button>

            <div
              className={`wf-c2-bottombar ${openApproval ? "has-approval" : ""}`}
            >
              <div className="wf-c2-bottom-main">
                <i className="wf-c2-pulse-dot" aria-hidden="true" />
                <span className="wf-c2-doing">
                  {nodeData.get(open.id)?.doing || open.subtitle}
                </span>
                <span className="wf-c2-step-count">
                  Step {openIndex + 1} of {orderedIds.length}
                </span>
              </div>
              {openApproval && (
                <div className="wf-c2-approval">
                  <div className="wf-c2-approval-details">
                    <span>Approval required</span>
                    <strong>
                      {str(openApproval.action).replaceAll("_", " ")} ·{" "}
                      {money(openApproval.amount)}
                    </strong>
                    <small>Requested by {str(openApproval.requested_by)}</small>
                  </div>
                  <form
                    className="wf-c2-approval-form"
                    onSubmit={submitApproval}
                  >
                    <input
                      autoComplete="one-time-code"
                      type="password"
                      minLength={6}
                      maxLength={64}
                      required
                      disabled={approvalDecision.busy}
                      value={approvalDecision.code}
                      aria-label="One-time approval code"
                      aria-invalid={Boolean(approvalDecision.error)}
                      onChange={(event) =>
                        approvalDecision.setCode(event.target.value)
                      }
                      placeholder="One-time code"
                    />
                    <button
                      type="submit"
                      value="approved"
                      disabled={approvalDecision.busy || state !== "live"}
                    >
                      Approve
                    </button>
                    <button
                      type="submit"
                      value="rejected"
                      disabled={approvalDecision.busy || state !== "live"}
                    >
                      Reject
                    </button>
                  </form>
                  {approvalDecision.error && (
                    <p className="wf-c2-approval-error" role="alert">
                      {approvalDecision.error}
                    </p>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
