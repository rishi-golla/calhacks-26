"use client";
/**
 * Right rail: operational metrics and a searchable, filterable, virtualized
 * case list. Clicking a row selects the case (the map highlights its path);
 * double-click or the arrow opens the case trace.
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { memo, useDeferredValue, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { ArrowUpRight, Search } from "lucide-react";
import { AGENT_IDS, AGENT_META, agentColor, type AgentId } from "@/lib/agents";
import { fmtMoney, type CaseView } from "@/lib/ops";
import { matchesTab, scenarioLabel, type KindTab } from "./model";

const ROW_H = 72;
const OVERSCAN = 6;

const TABS: { id: KindTab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "payables", label: "Payables" },
  { id: "receivables", label: "Receivables" },
  { id: "exceptions", label: "Exceptions" },
];

const STATUS_ACCENT: Record<string, string> = {
  resolved: "var(--teal)",
  in_progress: "var(--aqua)",
  open: "var(--lilac)",
  human_owned: "var(--orange)",
};

const statusLabel = (s: string) => (s === "in_progress" ? "Working" : s === "human_owned" ? "Human" : s.replaceAll("_", " "));

export type RailMetrics = {
  active: number;
  exceptions: number;
  /** Handoffs and completed tool calls in the last five minutes. */
  handoffs: number;
  toolCalls: number;
};

type Props = {
  caseList: CaseView[];
  liveAgents: Map<string, Set<AgentId>>;
  selectedCase: string;
  onSelect: (caseId: string) => void;
  tab: KindTab;
  onTab: (tab: KindTab) => void;
  metrics: RailMetrics;
};

function CaseRailImpl({ caseList, liveAgents, selectedCase, onSelect, tab, onTab, metrics }: Props) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [priority, setPriority] = useState("all");
  const [agent, setAgent] = useState("all");
  const q = useDeferredValue(query.trim().toLowerCase());

  const rows = useMemo(() => {
    const out: CaseView[] = [];
    for (const c of caseList) {
      if (!matchesTab(c, tab)) continue;
      if (status !== "all" && (status === "active" ? c.status === "resolved" : c.status !== status)) continue;
      if (priority !== "all" && c.priority !== priority) continue;
      if (agent !== "all" && !c.agents.includes(agent as AgentId)) continue;
      if (q) {
        const hay = `${c.caseId} ${c.loadId} ${c.title} ${scenarioLabel(c.scenario)} ${c.agents.map((a) => AGENT_META[a].name).join(" ")}`.toLowerCase();
        if (!hay.includes(q)) continue;
      }
      out.push(c);
    }
    return out;
  }, [caseList, tab, status, priority, agent, q]);

  const list = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ top: 0, height: 600 });
  useEffect(() => {
    const el = list.current;
    if (!el) return;
    const measure = () => setView({ top: el.scrollTop, height: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const first = Math.max(0, Math.floor(view.top / ROW_H) - OVERSCAN);
  const last = Math.min(rows.length, Math.ceil((view.top + view.height) / ROW_H) + OVERSCAN);

  return (
    <>
      <div className="au-metrics" aria-label="Operations">
        <Metric v={metrics.active} k="Active cases" />
        <Metric v={metrics.exceptions} k="Exceptions" hot={metrics.exceptions > 0} />
        <Metric v={metrics.handoffs} k="Handoffs" sub="last 5 min" />
        <Metric v={metrics.toolCalls} k="Tool calls" sub="last 5 min" />
      </div>
      <div className="au-explorer-head">
        <div className="au-list-meta">
          <span className="fl-section">Case explorer</span>
          <span className="fl-micro">
            {rows.length.toLocaleString()} / {caseList.length.toLocaleString()}
          </span>
        </div>
        <label className="au-search">
          <Search size={13} />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search cases, carriers, agents…" aria-label="Search cases" />
        </label>
        <div className="fl-tabs" role="tablist" aria-label="Case kind">
          {TABS.map((t) => (
            <button key={t.id} role="tab" aria-selected={tab === t.id} className={`fl-tab ${tab === t.id ? "active" : ""}`} onClick={() => onTab(t.id)}>
              {t.label}
            </button>
          ))}
        </div>
        <div className="au-filters">
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
            <option value="all">Any status</option>
            <option value="active">Not resolved</option>
            <option value="open">Open</option>
            <option value="in_progress">Working</option>
            <option value="human_owned">Human</option>
            <option value="resolved">Resolved</option>
          </select>
          <select value={priority} onChange={(e) => setPriority(e.target.value)} aria-label="Priority">
            <option value="all">Any priority</option>
            {["P0", "P1", "P2", "P3"].map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <select value={agent} onChange={(e) => setAgent(e.target.value)} aria-label="Agent">
            <option value="all">Any agent</option>
            {AGENT_IDS.map((id) => (
              <option key={id} value={id}>
                {AGENT_META[id].name}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div
        className="au-list"
        ref={list}
        onScroll={(e) => {
          // Read synchronously: `currentTarget` is null once the updater runs.
          const top = e.currentTarget.scrollTop;
          setView((v) => ({ ...v, top }));
        }}
        role="listbox"
        aria-label="Cases"
      >
        <div style={{ height: rows.length * ROW_H, position: "relative" }}>
          {rows.slice(first, last).map((c, i) => (
            <CaseRow
              key={c.caseId}
              c={c}
              top={(first + i) * ROW_H}
              live={liveAgents.get(c.caseId)}
              selected={c.caseId === selectedCase}
              onSelect={onSelect}
              onOpen={(id) => router.push(`/cases/${encodeURIComponent(id)}`)}
            />
          ))}
        </div>
        {!rows.length && <div className="au-empty fl-micro">No cases match these filters</div>}
      </div>
    </>
  );
}

function Metric({ v, k, sub, hot = false }: { v: number; k: string; sub?: string; hot?: boolean }) {
  return (
    <div className="au-metric">
      <div className={`v ${hot ? "hot" : ""}`}>{v.toLocaleString()}</div>
      <div className="k">
        {k}
        {sub && <small>{sub}</small>}
      </div>
    </div>
  );
}

function CaseRow({
  c,
  top,
  live,
  selected,
  onSelect,
  onOpen,
}: {
  c: CaseView;
  top: number;
  live: Set<AgentId> | undefined;
  selected: boolean;
  onSelect: (id: string) => void;
  onOpen: (id: string) => void;
}) {
  const accent = STATUS_ACCENT[c.status] ?? "var(--line-strong)";
  const needs = c.attention.length > 0 && c.status !== "resolved";
  return (
    <div
      className={`au-row ${selected ? "sel" : ""} ${needs ? "needs" : ""}`}
      style={{ top }}
      role="option"
      aria-selected={selected}
      tabIndex={0}
      onClick={() => onSelect(selected ? "" : c.caseId)}
      onDoubleClick={() => onOpen(c.caseId)}
      onKeyDown={(e) => {
        if (e.key === "Enter") onOpen(c.caseId);
        if (e.key === " ") {
          e.preventDefault();
          onSelect(selected ? "" : c.caseId);
        }
      }}
    >
      <span className="id">
        {c.caseId}
        <small>{c.priority}</small>
      </span>
      <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <span className="au-status" style={{ "--accent": accent } as CSSProperties}>
          {statusLabel(c.status)}
        </span>
        <Link
          href={`/cases/${encodeURIComponent(c.caseId)}`}
          className="open"
          aria-label={`Open ${c.caseId} trace`}
          onClick={(e) => e.stopPropagation()}
        >
          <ArrowUpRight size={13} />
        </Link>
      </span>
      <span className="title" title={c.title}>
        {c.title}
      </span>
      <span className="foot">
        <span className="dots" aria-label={`Route: ${c.agents.map((a) => AGENT_META[a].name).join(" → ")}`}>
          {c.agents.map((a) => (
            <i key={a} className={live?.has(a) ? "live" : ""} style={{ "--c": agentColor(a) } as CSSProperties} title={AGENT_META[a].name} />
          ))}
        </span>
        {c.exposureUsd ? <span className="amt">{fmtMoney(c.exposureUsd)}</span> : null}
      </span>
    </div>
  );
}

export const CaseRail = memo(CaseRailImpl);
