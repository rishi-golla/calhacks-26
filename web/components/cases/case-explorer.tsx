"use client";
/**
 * /cases — dense, windowed list of every case with search, facets and sort.
 * Rows link to the per-case execution trace.
 */
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Search, SlidersHorizontal, X } from "lucide-react";
import { useLiveEvents, useOps } from "@/components/data-provider";
import { Mascot } from "@/components/mascot";
import { AGENT_META, skillLabel, type AgentId } from "@/lib/agents";
import { str } from "@/lib/data";
import { fmtDuration, fmtMoney, type AttentionKind, type CaseView } from "@/lib/ops";
import { fmtSince } from "@/lib/view-epoch";
import { EpochNote } from "@/components/shell/view-epoch";
import { caseTitle, shortCaseId } from "@/components/agents/model";
import { caseDuration, caseKind, KIND_LABEL, shortDur, type CaseKind } from "./model";
import "./cases.css";

const ROW_H = 48;
const OVERSCAN = 8;

type StatusFilter = "all" | "active" | "resolved" | "human_owned";
type SortKey = "newest" | "exposure" | "duration";

const STATUS_TABS: { id: StatusFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "resolved", label: "Resolved" },
  { id: "human_owned", label: "Human" },
];
const SORTS: { id: SortKey; label: string }[] = [
  { id: "newest", label: "Newest" },
  { id: "exposure", label: "Exposure" },
  { id: "duration", label: "Duration" },
];
const PRIORITIES = ["P0", "P1", "P2", "P3"];
const KINDS: CaseKind[] = ["payable", "receivable", "bank_change"];

export const ATTENTION_META: Record<AttentionKind, { label: string; tone: string }> = {
  approval_pending: { label: "Approval", tone: "hot" },
  task_failed: { label: "Failed", tone: "bad" },
  input_required: { label: "Input", tone: "warn" },
  flag_open: { label: "Flag", tone: "hot" },
  review_issue: { label: "Review", tone: "warn" },
  hold_active: { label: "Hold", tone: "info" },
  call_no_answer: { label: "No answer", tone: "warn" },
};
/** Kinds that can need a person (`attentionAudience`); a hold never does on its own. */
const NEEDS_YOU_KINDS = (Object.keys(ATTENTION_META) as AttentionKind[]).filter((k) => k !== "hold_active");
/** Kinds the agents handle among themselves, in quiet chips: a task parked on another agent, an agent-placed hold. */
const HANDLING_META: Partial<Record<AttentionKind, { label: string }>> = {
  input_required: { label: "Waiting" },
  hold_active: { label: "Hold" },
};
const HANDLING_KINDS = Object.keys(HANDLING_META) as AttentionKind[];

export function PriorityChip({ priority }: { priority: string }) {
  const tone = priority === "P0" ? "bad solid" : priority === "P1" ? "hot outline" : priority === "P2" ? "warn outline" : "";
  return <span className={`fl-chip ${tone} cx-chip`}>{priority || "—"}</span>;
}

export function StatusChip({ status, active = 0 }: { status: string; active?: number }) {
  const tone = status === "resolved" ? "ok" : status === "human_owned" ? "hot" : status === "in_progress" ? "warn" : "info";
  const label = status === "in_progress" ? "In progress" : status === "human_owned" ? "Human" : status || "open";
  return (
    <span className={`fl-chip outline ${tone} cx-chip`}>
      {status !== "resolved" && active > 0 && <i className="fl-dot pulse" />}
      {label}
    </span>
  );
}

/* ------------------------- URL-persisted filters ------------------------- */

type Filters = {
  q: string;
  status: StatusFilter;
  priorities: string[];
  /** Matches `CaseView.needsYou`. */
  attention: AttentionKind[];
  /** Matches `CaseView.agentsHandling`. */
  handling: AttentionKind[];
  agents: AgentId[];
  kinds: CaseKind[];
  sort: SortKey;
};
const EMPTY: Filters = { q: "", status: "all", priorities: [], attention: [], handling: [], agents: [], kinds: [], sort: "newest" };

function readFilters(): Filters {
  if (typeof window === "undefined") return EMPTY;
  const p = new URLSearchParams(window.location.search);
  const list = (k: string) => (p.get(k) ?? "").split(",").filter(Boolean);
  const status = p.get("status") as StatusFilter;
  const sort = p.get("sort") as SortKey;
  return {
    q: p.get("q") ?? "",
    status: STATUS_TABS.some((s) => s.id === status) ? status : "all",
    priorities: list("p").filter((v) => PRIORITIES.includes(v)),
    attention: list("attn").filter((v): v is AttentionKind => (NEEDS_YOU_KINDS as string[]).includes(v)),
    // Links from before the split carried holds in `attn`.
    handling: [...new Set([...list("wait"), ...list("attn").filter((v) => v === "hold_active")])].filter((v): v is AttentionKind =>
      (HANDLING_KINDS as string[]).includes(v),
    ),
    agents: list("agent").filter((v): v is AgentId => v in AGENT_META),
    kinds: list("kind").filter((v): v is CaseKind => (KINDS as string[]).includes(v)),
    sort: SORTS.some((s) => s.id === sort) ? sort : "newest",
  };
}
function writeFilters(f: Filters) {
  const p = new URLSearchParams(window.location.search);
  const set = (k: string, v: string) => (v ? p.set(k, v) : p.delete(k));
  set("q", f.q);
  set("status", f.status === "all" ? "" : f.status);
  set("p", f.priorities.join(","));
  set("attn", f.attention.join(","));
  set("wait", f.handling.join(","));
  set("agent", f.agents.join(","));
  set("kind", f.kinds.join(","));
  set("sort", f.sort === "newest" ? "" : f.sort);
  const qs = p.toString();
  window.history.replaceState(window.history.state, "", `${window.location.pathname}${qs ? `?${qs}` : ""}`);
}

const toggle = <T,>(list: T[], value: T) => (list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);

/* ------------------------------ explorer ------------------------------ */

export default function CaseExplorer() {
  const { caseList, snapshot, now, ops, simulated, source, viewEpoch } = useOps();
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const hydrated = useRef(false);
  useEffect(() => {
    setFilters(readFilters());
    hydrated.current = true;
  }, []);
  useEffect(() => {
    if (hydrated.current) writeFilters(filters);
  }, [filters]);
  const patch = useCallback((p: Partial<Filters>) => setFilters((f) => ({ ...f, ...p })), []);

  // Freshly opened cases flash in.
  const [fresh, setFresh] = useState<Map<string, number>>(() => new Map());
  useLiveEvents((events) => {
    // The first diff after a load replays history as "new"; only flash what just happened.
    const recent = Date.now() - 10_000;
    const opened = events.filter((e) => e.type === "case_opened" && e.at > recent).map((e) => e.caseId);
    if (!opened.length) return;
    setFresh((prev) => {
      const next = new Map(prev);
      const at = Date.now();
      for (const id of opened) next.set(id, at);
      return next;
    });
  });
  useEffect(() => {
    if (!fresh.size) return;
    const timer = setTimeout(() => {
      setFresh((prev) => new Map([...prev].filter(([, at]) => Date.now() - at < 1600)));
    }, 1700);
    return () => clearTimeout(timer);
  }, [fresh]);

  const companies = useMemo(() => new Map(snapshot.company.map((c) => [str(c.id, ""), str(c.name, "")])), [snapshot.company]);
  const indexed = useMemo(
    () =>
      caseList.map((c) => {
        const kind = caseKind(c);
        const text = [
          c.caseId,
          c.loadId,
          c.title,
          companies.get(str(c.row.carrier_id, "")) ?? "",
          companies.get(str(c.row.shipper_id, "")) ?? "",
          str(c.row.carrier_id, ""),
          ...c.agents.flatMap((a) => [a, AGENT_META[a].name, AGENT_META[a].nick]),
          ...c.tasks.flatMap((t) => [t.skill, skillLabel(t.skill)]),
          KIND_LABEL[kind],
        ]
          .join(" ")
          .toLowerCase();
        return { c, kind, text };
      }),
    [caseList, companies],
  );

  const rows = useMemo(() => {
    const terms = filters.q.toLowerCase().split(/\s+/).filter(Boolean);
    const out = indexed.filter(({ c, kind, text }) => {
      if (filters.status === "active" && (c.status === "resolved" || c.status === "human_owned")) return false;
      if (filters.status === "resolved" && c.status !== "resolved") return false;
      if (filters.status === "human_owned" && c.status !== "human_owned") return false;
      if (filters.priorities.length && !filters.priorities.includes(c.priority)) return false;
      if (filters.kinds.length && !filters.kinds.includes(kind)) return false;
      // "Needs you" and "Agents handling" chips are one choice of open items: a
      // case matches any chip picked in either group (so an old `attn` link
      // holding a hold and another kind still matches either).
      if (
        (filters.attention.length || filters.handling.length) &&
        !filters.attention.some((k) => c.needsYou.includes(k)) &&
        !filters.handling.some((k) => c.agentsHandling.includes(k))
      )
        return false;
      if (filters.agents.length && !filters.agents.every((a) => c.agents.includes(a))) return false;
      return terms.every((t) => text.includes(t));
    });
    if (filters.sort === "exposure") out.sort((a, b) => b.c.exposureUsd - a.c.exposureUsd || b.c.openedAt - a.c.openedAt);
    else if (filters.sort === "duration") out.sort((a, b) => caseDuration(b.c, now) - caseDuration(a.c, now));
    return out;
  }, [indexed, filters, now]);

  const anyFilter =
    filters.q ||
    filters.status !== "all" ||
    filters.priorities.length ||
    filters.attention.length ||
    filters.handling.length ||
    filters.agents.length ||
    filters.kinds.length;

  /* windowing */
  const bodyRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewH, setViewH] = useState(900);
  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setViewH(el.clientHeight));
    ro.observe(el);
    setViewH(el.clientHeight);
    return () => ro.disconnect();
  }, []);
  const frame = useRef(0);
  const onScroll = () => {
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => setScrollTop(bodyRef.current?.scrollTop ?? 0));
  };
  useEffect(() => {
    bodyRef.current?.scrollTo({ top: 0 });
    setScrollTop(0);
  }, [filters]);
  const first = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN);
  const last = Math.min(rows.length, Math.ceil((scrollTop + viewH) / ROW_H) + OVERSCAN);

  return (
    <div className={`cx-page ${simulated ? "is-sim" : ""}`}>
      {/* Zero counts are the resting state; show the ones that say something. */}
      {(ops.casesActive || ops.casesResolvedToday || ops.casesNeedingYou || ops.medianResolutionS) > 0 && (
        <header className="cx-head">
          <div className="cx-stats">
            {ops.casesActive > 0 && <Stat label="Open" value={ops.casesActive} tone="var(--yellow)" />}
            {ops.casesResolvedToday > 0 && <Stat label="Resolved today" value={ops.casesResolvedToday} tone="var(--teal)" />}
            {ops.casesNeedingYou > 0 && <Stat label="Needs you" value={ops.casesNeedingYou} tone="var(--orange)" />}
            {ops.medianResolutionS > 0 && <Stat label="Median resolution" value={fmtDuration(ops.medianResolutionS)} />}
          </div>
        </header>
      )}

      <div className="cx-filters">
        <div className="cx-filter-row">
          <label className="cx-search">
            <Search size={14} aria-hidden />
            <input
              value={filters.q}
              onChange={(e) => patch({ q: e.target.value })}
              placeholder="Search cases"
              aria-label="Search cases"
              spellCheck={false}
            />
            {filters.q && (
              <button onClick={() => patch({ q: "" })} aria-label="Clear search">
                <X size={13} />
              </button>
            )}
          </label>
          <div className="fl-tabs cx-tabs" role="group" aria-label="Status">
            {STATUS_TABS.map((s) => (
              <button key={s.id} className={`fl-tab ${filters.status === s.id ? "active" : ""}`} onClick={() => patch({ status: s.id })}>
                {s.label}
              </button>
            ))}
          </div>
          <div className="cx-toggles" role="group" aria-label="Priority">
            {PRIORITIES.map((p) => (
              <button
                key={p}
                className={`cx-toggle ${filters.priorities.includes(p) ? "on" : ""}`}
                aria-pressed={filters.priorities.includes(p)}
                onClick={() => patch({ priorities: toggle(filters.priorities, p) })}
              >
                {p}
              </button>
            ))}
          </div>
          <MoreFilters count={filters.kinds.length + filters.attention.length + filters.handling.length}>
            <div className="cx-more-group">
              <span className="fl-micro">Type</span>
              <div className="cx-toggles" role="group" aria-label="Kind">
                {KINDS.map((k) => (
                  <button
                    key={k}
                    className={`cx-toggle ${filters.kinds.includes(k) ? "on" : ""}`}
                    aria-pressed={filters.kinds.includes(k)}
                    onClick={() => patch({ kinds: toggle(filters.kinds, k) })}
                  >
                    {KIND_LABEL[k]}
                  </button>
                ))}
              </div>
            </div>
            <div className="cx-more-group">
              <span className="fl-micro">Needs you</span>
              <div className="cx-toggles" role="group" aria-label="Needs you">
                {NEEDS_YOU_KINDS.map((k) => (
                  <button
                    key={k}
                    className={`cx-toggle tone-${ATTENTION_META[k].tone} ${filters.attention.includes(k) ? "on" : ""}`}
                    aria-pressed={filters.attention.includes(k)}
                    onClick={() => patch({ attention: toggle(filters.attention, k) })}
                  >
                    {ATTENTION_META[k].label}
                  </button>
                ))}
              </div>
            </div>
            <div className="cx-more-group">
              <span className="fl-micro">Agents handling</span>
              <div className="cx-toggles" role="group" aria-label="Agents handling">
                {HANDLING_KINDS.map((k) => (
                  <button
                    key={k}
                    className={`cx-toggle ${filters.handling.includes(k) ? "on" : ""}`}
                    aria-pressed={filters.handling.includes(k)}
                    onClick={() => patch({ handling: toggle(filters.handling, k) })}
                  >
                    {HANDLING_META[k]!.label}
                  </button>
                ))}
              </div>
            </div>
          </MoreFilters>
          {anyFilter ? (
            <span className="cx-count fl-micro">
              {rows.length} of {caseList.length}
              <button className="fl-btn quiet small" onClick={() => setFilters({ ...EMPTY, sort: filters.sort })}>
                <X size={12} /> Clear
              </button>
            </span>
          ) : null}
          <div className="cx-sort">
            <span className="fl-micro">Sort</span>
            <div className="fl-tabs cx-tabs" role="group" aria-label="Sort">
              {SORTS.map((s) => (
                <button key={s.id} className={`fl-tab ${filters.sort === s.id ? "active" : ""}`} onClick={() => patch({ sort: s.id })}>
                  {s.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      <div className="cx-table" role="table" aria-label="Cases" aria-rowcount={rows.length}>
        <div className="cx-row cx-row-head" role="row">
          <span role="columnheader">Case</span>
          <span role="columnheader">Title</span>
          <span role="columnheader">Pri</span>
          <span role="columnheader">Status</span>
          <span role="columnheader" className="num">
            Exposure
          </span>
          <span role="columnheader">Route</span>
          <span role="columnheader" className="num">
            Age
          </span>
          <span role="columnheader">Attention</span>
        </div>
        <div className="cx-body" ref={bodyRef} onScroll={onScroll}>
          {rows.length === 0 ? (
            <div className="cx-empty">
              {caseList.length === 0 ? (
                <>
                  <Mascot id="intake" size={64} mood="sleeping" />
                  <span>
                    {source === "connecting"
                      ? "Connecting…"
                      : viewEpoch.active && viewEpoch.epoch
                        ? `No cases since ${fmtSince(viewEpoch.epoch.since, now)} yet.`
                        : "No cases yet."}
                  </span>
                </>
              ) : (
                <>
                  <Mascot id="intake" size={64} mood="alert" />
                  <span>No cases match these filters.</span>
                  <button className="fl-btn small" onClick={() => setFilters(EMPTY)}>
                    Clear filters
                  </button>
                </>
              )}
            </div>
          ) : (
            <div className="cx-rows" style={{ height: rows.length * ROW_H }}>
              {rows.slice(first, last).map(({ c, kind }, i) => (
                <CaseRow key={c.caseId} c={c} kind={kind} now={now} top={(first + i) * ROW_H} fresh={fresh.has(c.caseId)} />
              ))}
            </div>
          )}
        </div>
      </div>
      <EpochNote style={{ display: "block", padding: "10px 0 12px" }} />
    </div>
  );
}

function CaseRow({ c, kind, now, top, fresh }: { c: CaseView; kind: CaseKind; now: number; top: number; fresh: boolean }) {
  const resolved = c.status === "resolved";
  // The load id is already in the case id; only say it again when it is not.
  const sub = [c.loadId && !c.caseId.includes(c.loadId) ? c.loadId : "", c.synthetic ? "synthetic" : ""].filter(Boolean).join(" · ");
  return (
    <Link
      href={`/cases/${encodeURIComponent(c.caseId)}`}
      className={`cx-row ${fresh ? "cx-fresh" : ""}`}
      style={{ transform: `translateY(${top}px)` }}
      role="row"
    >
      <span className="cx-id fl-mono" title={c.caseId}>
        {shortCaseId(c.caseId)}
      </span>
      <span className="cx-title" title={`${KIND_LABEL[kind]} · ${c.title}`}>
        <span className="cx-title-text">{caseTitle(c)}</span>
        {sub && <span className="cx-sub">{sub}</span>}
      </span>
      <span>
        <PriorityChip priority={c.priority} />
      </span>
      {/* Resolved is the normal end state; the column only carries the exceptions. */}
      <span>{!resolved && <StatusChip status={c.status} active={c.activeTasks} />}</span>
      <span className="num fl-num">{c.exposureUsd ? fmtMoney(c.exposureUsd) : null}</span>
      <span className="cx-agents">
        {c.agents.length ? (
          c.agents.map((a) => (
            <Mascot
              key={a}
              id={a}
              size={18}
              title={AGENT_META[a].name}
              mood={c.tasks.some((t) => t.agent === a && !t.terminal) ? "working" : "idle"}
            />
          ))
        ) : (
          <span className="fl-mute fl-micro">queued</span>
        )}
      </span>
      <span className={`num fl-mono cx-age ${resolved ? "" : "live"}`}>{c.openedAt ? shortDur(caseDuration(c, now)) : null}</span>
      <span className="cx-attn">
        {c.needsYou.map((k) => (
          <span key={k} className={`fl-chip outline ${ATTENTION_META[k].tone} cx-chip`}>
            {ATTENTION_META[k].label}
          </span>
        ))}
        {c.agentsHandling.map((k) => (
          <span key={`agents:${k}`} className="fl-chip outline cx-chip" title="Agents are handling this">
            {HANDLING_META[k]?.label ?? ATTENTION_META[k].label}
          </span>
        ))}
      </span>
    </Link>
  );
}

/** Rarely used filter groups, folded behind one button that shows how many are on. */
function MoreFilters({ count, children }: { count: number; children: React.ReactNode }) {
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const close = (e: PointerEvent | KeyboardEvent) => {
      const el = ref.current;
      if (!el?.open) return;
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !el.contains(e.target as Node)) el.open = false;
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", close);
    };
  }, []);
  return (
    <details className="cx-more" ref={ref}>
      <summary className={`cx-toggle cx-more-btn ${count ? "on" : ""}`} aria-label="More filters" title="Type, needs you, agents handling">
        <SlidersHorizontal size={12} aria-hidden />
        {count ? count : null}
      </summary>
      <div className="cx-more-panel fl-popover">{children}</div>
    </details>
  );
}

function Stat({ label, value, tone }: { label: string; value: number | string; tone?: string }) {
  return (
    <div className="cx-stat">
      <span className="fl-micro">{label}</span>
      <span className="fl-num cx-stat-value" style={tone ? { color: tone } : undefined}>
        {value}
      </span>
    </div>
  );
}
