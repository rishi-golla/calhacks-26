"use client";
import { memo, useEffect, useMemo, useState } from "react";
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
import {
  Brain,
  Building2,
  ChartNoAxesCombined,
  CheckCheck,
  Database,
  FileSearch,
  GitCommitHorizontal,
  Headphones,
  Landmark,
  Menu,
  Receipt,
  ShieldCheck,
  Sparkles,
  Truck,
  UserRound,
  Wallet,
  X,
  type LucideIcon,
} from "lucide-react";
import { money, str, timestamp, type Row, type Snapshot } from "@/lib/data";
import { useLive } from "./live-provider";
import "./workflow.css";

type Line = { key: string; ts: number; text: string; meta?: string };
type Step = {
  id: string;
  title: string;
  subtitle: string;
  url: string;
  icon: LucideIcon;
  x: number;
  y: number;
  agent?: string;
  sources: string[];
  feed: (d: Snapshot) => Line[];
};

const rows = (d: Snapshot, table: string): Row[] =>
  (d as Record<string, Row[]>)[table] ?? [];
const line = (table: string, r: Row, ts: unknown, text: string): Line => ({
  key: `${table}:${str(r.id ?? r.case_id ?? r.name ?? text)}`,
  ts: timestamp(ts),
  text,
  meta: r.case_id ? str(r.case_id) : undefined,
});
const byAgent = (agent: string) => (d: Snapshot) => [
  ...rows(d, "audit_event")
    .filter((r) => r.agent === agent)
    .map((r) => line("audit", r, r.ts, str(r.summary || r.kind))),
  ...rows(d, "task")
    .filter((r) => r.agent === agent)
    .map((r) =>
      line(
        "task",
        { ...r, id: r.task_id },
        r.updated_at || r.started_at,
        `${str(r.skill)} · ${str(r.business_status || r.a2a_state)}`,
      ),
    ),
];

const STEPS: Step[] = [
  {
    id: "generator",
    title: "Synthetic generator",
    subtitle: "Loads, invoices, PODs, bank lines",
    url: "sim://generator/raw",
    icon: Database,
    x: 0,
    y: 300,
    sources: ["case", "invoice"],
    feed: (d) => [
      ...rows(d, "case").map((r) =>
        line(
          "case",
          r,
          r.opened_at,
          `Opened ${str(r.case_id)} · ${str(r.title)}`,
        ),
      ),
      ...rows(d, "invoice").map((r) =>
        line(
          "invoice",
          r,
          r.issued_at,
          `${str(r.id)} · ${money(r.amount)} · ${str(r.status)}`,
        ),
      ),
    ],
  },
  {
    id: "ingest",
    title: "Ingestion",
    subtitle: "Raw documents to routable events",
    url: "ingest://raw→events",
    icon: FileSearch,
    x: 340,
    y: 300,
    agent: "ingest",
    sources: ["doc_read", "audit_event"],
    feed: (d) => [
      ...rows(d, "doc_read").map((r) =>
        line(
          "doc",
          r,
          0,
          `${str(r.doc_ref)} · p${str(r.page)} · ${str(r.span)}`,
        ),
      ),
      ...byAgent("ingest")(d),
    ],
  },
  {
    id: "asus",
    title: "ASUS secure env",
    subtitle: "Redaction, local LLM, routing",
    url: "asus.local/router",
    icon: ShieldCheck,
    x: 680,
    y: 300,
    sources: ["routing_outbox", "delegation_edge"],
    feed: (d) => [
      ...rows(d, "routing_outbox").map((r) =>
        line(
          "route",
          r,
          r.created_at,
          `${str(r.kind)} → ${str(r.target_agent)} · ${str(r.status)}`,
        ),
      ),
      ...rows(d, "delegation_edge").map((r) =>
        line(
          "edge",
          r,
          r.ts,
          `${str(r.from_agent)} → ${str(r.to_agent)} · ${str(r.skill)}`,
        ),
      ),
    ],
  },
  {
    id: "carrier_ap",
    title: "Carrier AP",
    subtitle: "Three-way match: rate con, POD, invoice",
    url: "a2a://carrier_ap:9101",
    icon: Truck,
    x: 1020,
    y: 40,
    agent: "carrier_ap",
    sources: ["audit_event", "task"],
    feed: byAgent("carrier_ap"),
  },
  {
    id: "shipper_ar",
    title: "Shipper AR",
    subtitle: "Payments, short-pay, collections",
    url: "a2a://shipper_ar",
    icon: Receipt,
    x: 1020,
    y: 300,
    agent: "shipper_ar",
    sources: ["audit_event", "task"],
    feed: byAgent("shipper_ar"),
  },
  {
    id: "risk",
    title: "Risk",
    subtitle: "Bank-change and fraud screening",
    url: "a2a://risk",
    icon: ShieldCheck,
    x: 1020,
    y: 560,
    agent: "risk",
    sources: ["audit_event", "task"],
    feed: byAgent("risk"),
  },
  {
    id: "contacts",
    title: "Live data · contacts",
    subtitle: "FMCSA carriers, SEC shippers",
    url: "fmcsa.dot.gov · sec.gov",
    icon: Building2,
    x: 1020,
    y: 820,
    sources: ["company", "contact"],
    feed: (d) => [
      ...rows(d, "company").map((r) =>
        line(
          "company",
          r,
          0,
          `${str(r.name)} · USDOT ${str(r.usdot)} · ${str(r.phone)}`,
        ),
      ),
      ...rows(d, "contact").map((r) =>
        line(
          "contact",
          r,
          0,
          `${str(r.name)} · ${str(r.title)} · ${str(r.phone)} · ${str(r.email)}`,
        ),
      ),
    ],
  },
  {
    id: "verification",
    title: "Verification",
    subtitle: "Confirms identity against public records",
    url: "a2a://verification",
    icon: CheckCheck,
    x: 1360,
    y: 560,
    agent: "verification",
    sources: ["audit_event", "task"],
    feed: byAgent("verification"),
  },
  {
    id: "voice_desk",
    title: "Voice Desk",
    subtitle: "Demo-routed calls and email",
    url: "a2a://voice_desk",
    icon: Headphones,
    x: 1700,
    y: 560,
    agent: "voice_desk",
    sources: ["call", "audit_event"],
    feed: (d) => [
      ...rows(d, "call").map((r) =>
        line(
          "call",
          r,
          r.ts ?? r.started_at,
          `${str(r.id)} · ${str(r.status)}`,
        ),
      ),
      ...byAgent("voice_desk")(d),
    ],
  },
  {
    id: "controller",
    title: "Controller",
    subtitle: "Deterministic pass: holds, ledger",
    url: "a2a://controller",
    icon: Landmark,
    x: 1360,
    y: 170,
    agent: "controller",
    sources: ["hold", "ledger_entry", "audit_event"],
    feed: (d) => [
      ...rows(d, "hold").map((r) =>
        line(
          "hold",
          r,
          r.placed_at,
          `${str(r.id)} · ${money(r.amount)} · ${str(r.status)}`,
        ),
      ),
      ...rows(d, "ledger_entry").map((r) =>
        line("ledger", r, r.posted_at, `${str(r.id)} · ${str(r.memo)}`),
      ),
      ...byAgent("controller")(d),
    ],
  },
  {
    id: "cfo",
    title: "CFO",
    subtitle: "Cash forecast and capital",
    url: "a2a://cfo",
    icon: ChartNoAxesCombined,
    x: 1700,
    y: 40,
    agent: "cfo",
    sources: ["cash_forecast", "audit_event"],
    feed: (d) => [
      ...rows(d, "cash_forecast").map((r) =>
        line(
          "forecast",
          r,
          r.updated_at,
          `${str(r.period)} · ${str(r.category)} · ${money(r.amount)}`,
        ),
      ),
      ...byAgent("cfo")(d),
    ],
  },
  {
    id: "payments",
    title: "Payments · Nessie",
    subtitle: "Capital One sandbox payouts",
    url: "api.nessieisreal.com",
    icon: Wallet,
    x: 2040,
    y: 40,
    sources: ["payment_proposal", "invoice"],
    feed: (d) => [
      ...rows(d, "payment_proposal").map((r) =>
        line(
          "payment",
          r,
          r.created_at ?? r.ts,
          `${str(r.id)} · ${money(r.amount)} · ${str(r.status)}`,
        ),
      ),
      ...rows(d, "invoice")
        .filter((r) => Number(r.paid_amount) > 0)
        .map((r) =>
          line(
            "paid",
            r,
            r.issued_at,
            `${str(r.id)} paid ${money(r.paid_amount)}`,
          ),
        ),
    ],
  },
  {
    id: "audit",
    title: "Audit trail",
    subtitle: "Git-style change sets per result",
    url: "spacetime://change_set",
    icon: GitCommitHorizontal,
    x: 1700,
    y: 300,
    sources: ["change_set"],
    feed: (d) =>
      rows(d, "change_set").map((r) =>
        line(
          "cs",
          r,
          r.ts,
          `${str(r.hash).slice(0, 7)} ${str(r.agent)}: ${str(r.summary)}`,
        ),
      ),
  },
  {
    id: "human",
    title: "Human input",
    subtitle: "Approvals and flags",
    url: "ggf://human-inbox",
    icon: UserRound,
    x: 2040,
    y: 300,
    sources: ["approval", "flag"],
    feed: (d) => [
      ...rows(d, "approval").map((r) =>
        line(
          "approval",
          r,
          r.decided_at || r.expires_at,
          `${str(r.action)} · ${money(r.amount)} · ${str(r.decision || "pending")}`,
        ),
      ),
      ...rows(d, "flag").map((r) =>
        line("flag", r, r.ts, `${str(r.kind)}: ${str(r.note)}`),
      ),
    ],
  },
  {
    id: "validator",
    title: "Validator",
    subtitle: "Reviews every agent output",
    url: "a2a://validator",
    icon: Sparkles,
    x: 2040,
    y: 560,
    agent: "validator",
    sources: ["review", "audit_event"],
    feed: (d) => [
      ...rows(d, "review").map((r) =>
        line("review", r, r.ts, `${str(r.target_task_id)} · ${str(r.verdict)}`),
      ),
      ...byAgent("validator")(d),
    ],
  },
  {
    id: "instructions",
    title: "Instructions & memory",
    subtitle: "What the agents learn",
    url: "spacetime://instruction_version",
    icon: Brain,
    x: 2380,
    y: 560,
    sources: ["instruction_version", "agent_memory"],
    feed: (d) => [
      ...rows(d, "instruction_version").map((r) =>
        line(
          "instr",
          r,
          r.created_at,
          `${str(r.agent)} v${str(r.version)} · ${str(r.reason)}`,
        ),
      ),
      ...rows(d, "agent_memory").map((r) =>
        line("memory", r, r.created_at, `${str(r.agent)}: ${str(r.text)}`),
      ),
    ],
  },
];

const LINKS: [string, string, boolean?][] = [
  ["generator", "ingest"],
  ["ingest", "asus"],
  ["asus", "carrier_ap"],
  ["asus", "shipper_ar"],
  ["asus", "risk"],
  ["risk", "verification"],
  ["contacts", "verification"],
  ["verification", "voice_desk"],
  ["carrier_ap", "controller"],
  ["shipper_ar", "controller"],
  ["controller", "cfo"],
  ["cfo", "payments"],
  ["controller", "audit"],
  ["voice_desk", "audit"],
  ["audit", "human"],
  ["human", "validator"],
  ["validator", "instructions"],
  ["instructions", "asus", true],
];

const sorted = (lines: Line[]) => [...lines].sort((a, b) => b.ts - a.ts);
const clock = (ts: number) =>
  ts
    ? new Date(ts).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "";

function scope(d: Snapshot, caseId: string): Snapshot {
  if (!caseId) return d;
  const out: Record<string, Row[]> = {};
  for (const [k, v] of Object.entries(d))
    out[k] = (v as Row[]).filter(
      (r) => !("case_id" in r) || r.case_id === caseId,
    );
  return out as Snapshot;
}

type StepData = { step: Step; lines: Line[]; status: string; open: () => void };

function Chrome({ url, status }: { url: string; status: string }) {
  return (
    <div className="wf-chrome">
      <span className="wf-lights">
        <i />
        <i />
        <i />
      </span>
      <span className="wf-url">{url}</span>
      <span className={`wf-live ${status}`}>{status}</span>
    </div>
  );
}

const StepNode = memo(function StepNode({ data }: NodeProps<Node<StepData>>) {
  const { step, lines, status } = data;
  const Icon = step.icon;
  return (
    <div className="wf-node" onClick={data.open}>
      <Handle type="target" position={Position.Left} />
      <div className="wf-head">
        <span className="wf-icon">
          <Icon size={15} strokeWidth={1.8} />
        </span>
        <div>
          <strong>{step.title}</strong>
          <small>{step.subtitle}</small>
        </div>
      </div>
      <div className="wf-browser">
        <Chrome url={step.url} status={status} />
        <div className="wf-screen">
          {lines.length === 0 ? (
            <p className="wf-empty">Waiting for events…</p>
          ) : (
            lines.slice(0, 4).map((l) => (
              <p
                key={l.key}
                className={Date.now() - l.ts < 60_000 ? "fresh" : ""}
              >
                <span>{clock(l.ts)}</span>
                {l.text}
              </p>
            ))
          )}
        </div>
      </div>
      <div className="wf-foot">
        {lines.length} events · {step.sources.join(", ")}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
});
const nodeTypes = { step: StepNode };

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
  const [openId, setOpenId] = useState("");
  const scoped = useMemo(() => scope(data, caseId), [data, caseId]);
  const feeds = useMemo(
    () => Object.fromEntries(STEPS.map((s) => [s.id, sorted(s.feed(scoped))])),
    [scoped],
  );
  const statusOf = (s: Step) => {
    if (s.agent && s.agent !== "ingest") {
      const rec = data.agent.find((r) => r.name === s.agent);
      if (!rec) return "offline";
      return str(rec.status, "idle").toLowerCase();
    }
    if (state !== "live" && state !== "fallback") return "offline";
    return feeds[s.id].length ? "live" : "idle";
  };
  const nodes: Node<StepData>[] = STEPS.map((step) => ({
    id: step.id,
    type: "step",
    position: { x: step.x * 0.84, y: step.y * 0.86 },
    data: {
      step,
      lines: feeds[step.id],
      status: statusOf(step),
      open: () => setOpenId(step.id),
    },
  }));
  const edges: Edge[] = LINKS.map(([from, to, loop]) => ({
    id: `${from}-${to}`,
    source: from,
    target: to,
    animated: Boolean(loop) || feeds[from].length > 0,
    className: loop ? "wf-edge loop" : "wf-edge",
    markerEnd: {
      type: MarkerType.ArrowClosed,
      width: 14,
      height: 14,
      color: "#b5b5ba",
    },
  }));
  useEffect(() => {
    if (!openId) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpenId("");
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openId]);
  const open = STEPS.find((s) => s.id === openId);
  const caseTitle = data.case.find((r) => r.case_id === caseId)?.title;
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
        <span className="wf-sub">Golden Gate Freight · every step, live</span>
        {caseId && (
          <button className="wf-chip" onClick={onClearCase}>
            {str(caseTitle || caseId).replace(/^\[W2 fixture\]\s*/, "")}
            <X size={12} />
          </button>
        )}
        <span className={`wf-conn ${state}`}>
          {state === "live" ? "Live" : state}
        </span>
      </header>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.04 }}
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
              <Chrome url={open.url} status={statusOf(open)} />
              <button aria-label="Close" onClick={() => setOpenId("")}>
                <X size={16} />
              </button>
            </div>
            <div className="wf-window-body">
              <section className="wf-stream">
                <h2>
                  <open.icon size={16} /> {open.title}
                </h2>
                <p className="wf-window-sub">{open.subtitle}</p>
                {feeds[open.id].length === 0 && (
                  <p className="wf-empty">
                    No events yet. This updates the moment{" "}
                    {open.sources.join(" or ")} changes.
                  </p>
                )}
                <ol>
                  {feeds[open.id].map((l) => (
                    <li
                      key={l.key}
                      className={Date.now() - l.ts < 60_000 ? "fresh" : ""}
                    >
                      <time>{clock(l.ts)}</time>
                      <span>{l.text}</span>
                      {l.meta && <em>{l.meta}</em>}
                    </li>
                  ))}
                </ol>
              </section>
              <aside className="wf-info">
                <dl>
                  <dt>Status</dt>
                  <dd>{statusOf(open)}</dd>
                  <dt>Events</dt>
                  <dd>{feeds[open.id].length}</dd>
                  <dt>Live sources</dt>
                  <dd>{open.sources.join(", ")}</dd>
                  <dt>Receives from</dt>
                  <dd>
                    {LINKS.filter((l) => l[1] === open.id)
                      .map((l) => STEPS.find((s) => s.id === l[0])?.title)
                      .join(", ") || "—"}
                  </dd>
                  <dt>Sends to</dt>
                  <dd>
                    {LINKS.filter((l) => l[0] === open.id)
                      .map((l) => STEPS.find((s) => s.id === l[1])?.title)
                      .join(", ") || "—"}
                  </dd>
                </dl>
              </aside>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
