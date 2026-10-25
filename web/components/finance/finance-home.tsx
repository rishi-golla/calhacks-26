"use client";
/**
 * Finance home (`/`): the brokerage's financial health and the autonomous
 * back office that keeps it, in real time. Everything renders from `useOps()`
 * (live SpacetimeDB or the labeled browser simulation); nothing polls.
 *
 * Layout: greeting → 4 KPI cards → needs attention | cash flow, cash position
 * + operations | payables breakdown, live ledger, automation goal → agent crew
 * → invoices & payments.
 */
import Link from "next/link";
import { Suspense, useEffect, useMemo, useState, type CSSProperties } from "react";
import { useSearchParams } from "next/navigation";
import { ArrowDown, ArrowRight, ArrowUp, CalendarDays, HandCoins, Lightbulb, Receipt, Target, TrendingUp, Wallet, type LucideIcon } from "lucide-react";
import { useOps } from "@/components/data-provider";
import { Mascot } from "@/components/mascot";
import { fmtAgo, fmtDuration, type AttentionItem, type TrendPoint } from "@/lib/ops";
import { AgentCrew } from "./agent-crew";
import { ApprovalActions, KIND_META, titleFor } from "./attention-queue";
import { LineChart } from "./charts";
import { CountUp } from "./count-up";
import { change, count, dayLabel, emptyNote, nick, periodLabel, usd, usdShort } from "./format";
import { InvoicesPanel } from "./invoices-table";
import { LiveTicker } from "./live-ticker";
import "./finance.css";
import "./finora.css";

type Range = 7 | 30;
const pct0 = (v: number) => `${Math.round(v)}%`;
const reveal = (i: number) => ({ "--delay": `${i * 60}ms` }) as CSSProperties;
const GOAL = 0.8;

export default function FinanceHome() {
  const [range, setRange] = useState<Range>(30);
  return (
    <div className="fl-page fh fn">
      <div className="fn-grid">
        <div className="fn-main">
          <Greeting range={range} setRange={setRange} />
          <KpiCards range={range} />
          <div className="fn-row2 fl-reveal" style={reveal(2)}>
            <AttentionList />
            <CashFlow />
          </div>
          <div className="fl-reveal" style={reveal(3)}>
            <CashPosition range={range} />
          </div>
        </div>
        <aside className="fn-side fl-reveal" style={reveal(1)}>
          <PayablesDonut />
          <LiveTicker />
          <AutomationGoal />
        </aside>
      </div>
      <div className="fl-reveal" style={reveal(4)}>
        <AgentCrew />
      </div>
      <div className="fl-reveal" style={reveal(5)}>
        <InvoicesPanel />
      </div>
      <Suspense fallback={null}>
        <FocusFromQuery />
      </Suspense>
    </div>
  );
}

function CardHead({ title, href, more = "View all" }: { title: string; href?: string; more?: string }) {
  return (
    <header className="fn-card-head">
      <h2>{title}</h2>
      {href && (
        <Link href={href} className="fn-more">
          {more} <ArrowRight size={13} />
        </Link>
      )}
    </header>
  );
}

function Greeting({ range, setRange }: { range: Range; setRange: (r: Range) => void }) {
  const { source, simulated, ops, attention, now, lastUpdated } = useOps();
  const hour = new Date(now).getHours();
  const part = hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
  const provenance = simulated ? "Simulated" : source === "live" ? "Live" : source === "fallback" ? "Snapshot" : source === "connecting" ? "Connecting" : "Offline";
  const from = now - (range - 1) * 86_400_000;
  return (
    <header className="fn-greet fl-reveal" style={reveal(0)}>
      <div>
        <h1>
          Good {part}, Controller <span className="fn-sun" aria-hidden />
        </h1>
        <p>
          Here&apos;s your back office at a glance.{" "}
          <span className={`fn-prov ${source}`}>
            <i /> {provenance}
          </span>{" "}
          {lastUpdated ? `updated ${fmtAgo(lastUpdated, now)}` : "no updates yet"}
          {simulated && " · browser simulation, nothing is written"}
        </p>
        <div className="fn-chips">
          <Link href="/cases">{count(ops.casesActive)} active cases</Link>
          <Link href="/agents">{count(ops.tasksActive)} tasks in flight</Link>
          <Link href="/agents">{ops.agentsOnline}/9 agents online</Link>
          <a href="#attention" className={attention.length ? "hot" : "ok"}>
            {attention.length} need attention
          </a>
        </div>
      </div>
      <div className="fn-range" role="group" aria-label="Date range">
        <CalendarDays size={15} />
        <span>
          {dayLabel(from)} – {dayLabel(now)}
        </span>
        <div className="fn-seg">
          {([7, 30] as const).map((r) => (
            <button key={r} className={range === r ? "on" : ""} onClick={() => setRange(r)} aria-pressed={range === r}>
              {r}D
            </button>
          ))}
        </div>
      </div>
    </header>
  );
}

const KPIS: { key: "cash" | "ap" | "ar" | "revenue"; label: string; icon: LucideIcon; tone: string; good: "up" | "down" }[] = [
  { key: "cash", label: "Cash balance", icon: Wallet, tone: "green", good: "up" },
  { key: "ar", label: "Receivables", icon: HandCoins, tone: "violet", good: "up" },
  { key: "ap", label: "Payables", icon: Receipt, tone: "blue", good: "down" },
  { key: "revenue", label: "Revenue MTD", icon: TrendingUp, tone: "pink", good: "up" },
];

function KpiCards({ range }: { range: Range }) {
  const { finance } = useOps();
  const values = { cash: finance.cash, ap: finance.ap, ar: finance.ar, revenue: finance.revenueMtd };
  return (
    <div className="fn-kpis fl-reveal" style={reveal(1)}>
      {KPIS.map((k) => {
        const series = finance.trends[k.key].slice(-range);
        const d = change(series);
        const up = d.abs >= 0;
        const good = (up && k.good === "up") || (!up && k.good === "down");
        return (
          <section key={k.key} className="fn-card fn-kpi" aria-label={k.label}>
            <span className={`fn-icon ${k.tone}`}>
              <k.icon size={18} />
            </span>
            <div className="fn-kpi-label">{k.label}</div>
            <div className="fn-kpi-value">
              <CountUp value={values[k.key]} format={usd} good={k.good} />
            </div>
            <div className={`fn-delta ${d.abs === 0 ? "" : good ? "good" : "bad"}`}>
              {d.abs !== 0 && (up ? <ArrowUp size={13} /> : <ArrowDown size={13} />)}
              {d.pct === null ? usdShort(Math.abs(d.abs)) : `${Math.abs(d.pct * 100).toFixed(1)}%`}
            </div>
            <div className="fn-kpi-foot">vs. {range} days ago</div>
          </section>
        );
      })}
    </div>
  );
}

const TONE: Record<AttentionItem["severity"], string> = { critical: "red", high: "amber", medium: "violet", low: "grey" };

function AttentionList() {
  const { attention, source, now } = useOps();
  const [filter, setFilter] = useState<AttentionItem["kind"] | "all">("all");
  const [all, setAll] = useState(false);
  const kinds = useMemo(() => {
    const m = new Map<AttentionItem["kind"], number>();
    for (const a of attention) m.set(a.kind, (m.get(a.kind) ?? 0) + 1);
    return [...m.entries()];
  }, [attention]);
  const shown = attention.filter((a) => filter === "all" || a.kind === filter);
  const rows = all ? shown : shown.slice(0, 5);
  return (
    <section id="attention" className="fn-card fn-att" aria-label="Needs attention">
      <header className="fn-card-head">
        <h2>
          Needs attention <span className="fn-badge">{attention.length}</span>
        </h2>
        {shown.length > 5 && (
          <button className="fn-more" onClick={() => setAll((v) => !v)}>
            {all ? "Show less" : `View all ${shown.length}`} <ArrowRight size={13} />
          </button>
        )}
      </header>
      {kinds.length > 1 && (
        <div className="fn-filter" role="tablist" aria-label="Filter by kind">
          <button className={filter === "all" ? "on" : ""} onClick={() => setFilter("all")} role="tab" aria-selected={filter === "all"}>
            All
          </button>
          {kinds.map(([k, n]) => (
            <button key={k} className={filter === k ? "on" : ""} onClick={() => setFilter(k)} role="tab" aria-selected={filter === k}>
              {KIND_META[k].tag} <b>{n}</b>
            </button>
          ))}
        </div>
      )}
      {rows.length === 0 ? (
        <div className="fn-empty">
          <Mascot id="validator" size={56} mood={source === "live" || source === "simulation" ? "success" : "sleeping"} />
          <span>{emptyNote(source, "All clear. Nothing is waiting on a person.")}</span>
        </div>
      ) : (
        <ul className="fn-list">
          {rows.map((item) => (
            <li key={item.id} className="fn-att-row">
              <span className="fn-avatar">
                <Mascot id={item.agent || "human"} size={30} title={nick(item.agent)} />
              </span>
              <div className="fn-row-main">
                <Link href={item.caseId ? `/cases/${encodeURIComponent(item.caseId)}` : "/cases"} className="fn-row-title">
                  {titleFor(item)}
                </Link>
                <div className="fn-row-sub">
                  {nick(item.agent)} · {item.caseId || "no case"} · {fmtAgo(item.at, now)}
                </div>
                {item.kind === "approval_pending" && <ApprovalActions item={item} />}
              </div>
              <div className="fn-row-right">
                {item.amount > 0 && <span className="fn-amt">{usd(item.amount)}</span>}
                <span className={`fn-pill ${TONE[item.severity]}`}>{KIND_META[item.kind].tag}</span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function CashFlow() {
  const { finance, source } = useOps();
  const rows = finance.forecast;
  const max = Math.max(1, ...rows.flatMap((r) => [r.inflow, r.outflow]));
  const net = rows.reduce((s, r) => s + r.net, 0);
  return (
    <section className="fn-card fn-flow" aria-label="Cash flow forecast">
      <header className="fn-card-head">
        <h2>Cash flow</h2>
        <div className="fn-legend">
          <span>
            <i className="green" /> Inflow
          </span>
          <span>
            <i className="violet" /> Outflow
          </span>
        </div>
      </header>
      {rows.length === 0 ? (
        <div className="fn-empty">{emptyNote(source, "No forecast yet.")}</div>
      ) : (
        <>
          <div className="fn-bars" role="img" aria-label={`Forecast net ${usdShort(net)}`}>
            {[1, 0.75, 0.5, 0.25, 0].map((f) => (
              <span key={f} className="fn-bars-grid" style={{ bottom: `${f * 100}%` }}>
                {usdShort(max * f)}
              </span>
            ))}
            {rows.map((r) => (
              <div key={r.period} className="fn-bar-col" title={`${periodLabel(r.period)}: in ${usd(r.inflow)}, out ${usd(r.outflow)}, net ${usd(r.net)}`}>
                <div className="fn-bar-pair">
                  <span className="fn-bar green" style={{ height: `${(r.inflow / max) * 100}%` }} />
                  <span className="fn-bar violet" style={{ height: `${(r.outflow / max) * 100}%` }} />
                </div>
                <span className="fn-bar-label">{periodLabel(r.period)}</span>
              </div>
            ))}
          </div>
          <div className="fn-flow-foot">
            Projected net <b className={net >= 0 ? "good" : "bad"}>{usd(net)}</b> over {rows.length} periods
          </div>
        </>
      )}
    </section>
  );
}

function CashPosition({ range }: { range: Range }) {
  const { finance, ops, source } = useOps();
  const [hover, setHover] = useState<number | null>(null);
  const points: TrendPoint[] = finance.trends.cash.slice(-range);
  const d = change(points);
  const at = hover === null ? null : points[hover];
  const stats = [
    { label: "Cases resolved today", value: count(ops.casesResolvedToday), tone: "green" },
    { label: "Throughput", value: `${Math.round(ops.throughputPerHour)}/h`, tone: "violet" },
    { label: "Median resolution", value: fmtDuration(ops.medianResolutionS), tone: "blue" },
    { label: "Clerk time saved", value: `${count(ops.minutesSaved / 60)} h`, tone: "amber" },
    { label: "Handoffs · tool calls", value: `${count(ops.handoffs)} · ${count(ops.toolCalls)}`, tone: "pink" },
  ];
  return (
    <section className="fn-card fn-pos" aria-label="Cash position">
      <div className="fn-pos-chart">
        <CardHead title="Cash position" />
        <div className="fn-pos-top">
          <div className="fn-pos-value">{usd(at ? at.value : finance.cash)}</div>
          <div className="fn-kpi-foot">{at ? dayLabel(at.t) : `Today · last ${range} days`}</div>
          {!at && points.length > 1 && (
            <div className={`fn-delta ${d.abs >= 0 ? "good" : "bad"}`}>
              {d.abs >= 0 ? <ArrowUp size={13} /> : <ArrowDown size={13} />}
              {d.pct === null ? usdShort(Math.abs(d.abs)) : `${Math.abs(d.pct * 100).toFixed(1)}%`}
            </div>
          )}
        </div>
        {points.length < 2 ? (
          <div className="fn-empty">{emptyNote(source, "Not enough history to chart yet.")}</div>
        ) : (
          <LineChart points={points} color="#7c5cfc" height={170} drawKey={`cash-${range}`} label="Cash position" hover={hover} onHover={setHover} />
        )}
      </div>
      <div className="fn-pos-stats">
        <CardHead title="Operations" href="/agents" more="Agents" />
        <ul className="fn-list">
          {stats.map((s) => (
            <li key={s.label} className="fn-stat">
              <span className={`fn-dot ${s.tone}`} />
              <span className="fn-stat-label">{s.label}</span>
              <span className="fn-stat-value">{s.value}</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

const DONUT_TONES = ["#7c5cfc", "#f5a524", "#ef5d6c", "#22c08a"];

function PayablesDonut() {
  const { finance, source } = useOps();
  const held = finance.heldInvoices.amount;
  const overdue = Math.min(finance.overdueInvoices.amount, Math.max(0, finance.outstandingInvoices.amount - held));
  const clear = Math.max(0, finance.outstandingInvoices.amount - held - overdue);
  const segs = [
    { label: "Open, on terms", amount: clear, n: Math.max(0, finance.outstandingInvoices.count - finance.heldInvoices.count) },
    { label: "Overdue 30d+", amount: overdue, n: finance.overdueInvoices.count },
    { label: "On hold", amount: held, n: finance.heldInvoices.count },
    { label: "Payments proposed", amount: finance.paymentsProposed.amount, n: finance.paymentsProposed.count },
  ];
  const total = segs.reduce((s, x) => s + x.amount, 0);
  const R = 52;
  const C = 2 * Math.PI * R;
  let acc = 0;
  return (
    <section className="fn-card fn-donut" aria-label="Payables breakdown">
      <CardHead title="Payables breakdown" href="/cases" />
      {total <= 0 ? (
        <div className="fn-empty">{emptyNote(source, "No open carrier invoices.")}</div>
      ) : (
        <div className="fn-donut-body">
          <div className="fn-donut-ring">
            <svg viewBox="0 0 140 140" aria-hidden>
              <circle cx="70" cy="70" r={R} className="fn-donut-track" />
              {segs.map((s, i) => {
                const len = (s.amount / total) * C;
                const el = (
                  <circle
                    key={s.label}
                    cx="70"
                    cy="70"
                    r={R}
                    stroke={DONUT_TONES[i]}
                    strokeDasharray={`${Math.max(0, len - 3)} ${C}`}
                    strokeDashoffset={-acc}
                    className="fn-donut-seg"
                  />
                );
                acc += len;
                return el;
              })}
            </svg>
            <div className="fn-donut-center">
              <b>{usdShort(total)}</b>
              <span>Committed</span>
            </div>
          </div>
          <ul className="fn-donut-legend">
            {segs.map((s, i) => (
              <li key={s.label}>
                <i style={{ background: DONUT_TONES[i] }} />
                <span>
                  {s.label}
                  <small>{s.n} items</small>
                </span>
                <em>{pct0((s.amount / total) * 100)}</em>
                <b>{usdShort(s.amount)}</b>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function AutomationGoal() {
  const { ops } = useOps();
  const rate = ops.automationRate;
  const onTrack = rate >= GOAL;
  return (
    <section className="fn-card fn-goal" aria-label="Automation goal">
      <header className="fn-card-head">
        <h2>
          <span className="fn-icon violet sm">
            <Target size={15} />
          </span>
          Automation goal
        </h2>
      </header>
      <div className="fn-goal-row">
        <span>Cases closed with no human touch</span>
        <b>
          {pct0(rate * 100)} <small>/ {pct0(GOAL * 100)}</small>
        </b>
      </div>
      <div className="fn-progress" role="progressbar" aria-valuenow={Math.round(rate * 100)} aria-valuemin={0} aria-valuemax={100}>
        <span style={{ width: `${Math.min(100, (rate / GOAL) * 100)}%` }} />
      </div>
      <Link href="/agents" className="fn-tip">
        <Lightbulb size={16} />
        <span>
          {onTrack
            ? `On track: ${count(ops.casesResolved)} of ${count(ops.casesTotal)} cases resolved.`
            : `${count(ops.casesNeedingAttention)} cases need a person; clearing them lifts the rate.`}
        </span>
        <ArrowRight size={14} />
      </Link>
    </section>
  );
}

/** `/?focus=attention` (the top-bar bell) scrolls the queue into view. */
function FocusFromQuery() {
  const focus = useSearchParams().get("focus");
  useEffect(() => {
    if (focus !== "attention") return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timer = setTimeout(() => document.getElementById("attention")?.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" }), 200);
    return () => clearTimeout(timer);
  }, [focus]);
  return null;
}
