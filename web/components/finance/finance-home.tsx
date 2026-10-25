"use client";
/**
 * Finance home (`/`): the brokerage's financial health and the autonomous
 * back office that keeps it, in real time. Everything renders from `useOps()`
 * (live SpacetimeDB or the labeled browser simulation); nothing polls.
 *
 * Top to bottom: 4 KPI tiles → cash + forecast | needs attention →
 * operations → agent crew → invoices & payments | activity.
 */
import { Suspense, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { useOps } from "@/components/data-provider";
import { AgentCrew } from "./agent-crew";
import { AttentionQueue } from "./attention-queue";
import { ForecastBars, LineChart, Sparkline } from "./charts";
import { CountUp } from "./count-up";
import { change, count, dayLabel, emptyNote, usd, usdShort } from "./format";
import { InvoicesPanel } from "./invoices-table";
import { LiveTicker } from "./live-ticker";
import "./finance.css";

const pct1 = (v: number) => `${v.toFixed(1)}%`;
const perHour = (v: number) => `${Math.round(v)}/h`;
const hours = (v: number) => `${count(v)} h`;
const reveal = (i: number) => ({ "--delay": `${i * 70}ms` }) as CSSProperties;

export default function FinanceHome() {
  return (
    <div className="fl-page fh">
      <KpiTiles />
      <div className="fh-row fh-row-main fl-reveal" style={reveal(1)}>
        <CashPanel />
        <AttentionQueue id="attention" />
      </div>
      <div className="fl-reveal" style={reveal(2)}>
        <OpsStrip />
      </div>
      <div className="fl-reveal" style={reveal(3)}>
        <AgentCrew />
      </div>
      <div className="fh-row fh-row-bottom fl-reveal" style={reveal(4)}>
        <InvoicesPanel />
        <LiveTicker />
      </div>
      <Suspense fallback={null}>
        <FocusFromQuery />
      </Suspense>
    </div>
  );
}

/* ------------------------------ KPI tiles ------------------------------ */

type Tile = {
  key: string;
  label: string;
  /** Used on phones where the long label would truncate. */
  short: string;
  value: number;
  color: string;
  series: number[];
  good: "up" | "down" | "none";
  delta: ReactNode;
  foot?: ReactNode;
};

/** Change over the series: percent when the start is non-zero, else the dollar change. */
function Delta({ change: c, good }: { change: { abs: number; pct: number | null }; good: "up" | "down" | "none" }) {
  const v = c.pct ?? c.abs;
  const eps = c.pct == null ? 0.5 : 0.0005;
  const dir = v > eps ? "up" : v < -eps ? "down" : "flat";
  const tone = good === "none" || dir === "flat" ? "flat" : dir === good ? "ok" : "hot";
  return (
    <span className={`fh-delta ${tone}`}>
      {dir === "up" ? "▲" : dir === "down" ? "▼" : "■"} {c.pct == null ? usdShort(Math.abs(c.abs)) : `${Math.abs(c.pct * 100).toFixed(1)}%`}
      <span className="fh-delta-span"> 30d</span>
    </span>
  );
}

function KpiTiles() {
  const { finance, snapshot, source } = useOps();
  const hasLedger = snapshot.ledger_entry.length > 0;
  const series = useMemo(
    () => ({
      cash: finance.trends.cash.map((p) => p.value),
      ap: finance.trends.ap.map((p) => p.value),
      ar: finance.trends.ar.map((p) => p.value),
      revenue: finance.trends.revenue.map((p) => p.value),
    }),
    [finance.trends],
  );
  const cashChange = change(finance.trends.cash);
  const dso = finance.revenue30d > 0 ? finance.ar / (finance.revenue30d / 30) : 0;
  const tiles: Tile[] = [
    {
      key: "cash",
      label: "Cash",
      short: "Cash",
      value: finance.cash,
      color: "var(--teal)",
      series: series.cash,
      good: "up",
      delta: <Delta change={cashChange} good="up" />,
    },
    {
      key: "ap",
      label: "Accounts payable",
      short: "AP",
      value: finance.ap,
      color: "var(--aqua)",
      series: series.ap,
      good: "none",
      delta: <Delta change={change(finance.trends.ap)} good="none" />,
      foot: (
        <>
          <b className="fl-num">{count(finance.outstandingInvoices.count)}</b> open<span className="fh-long"> invoices</span> · <b className="fl-num">{count(finance.heldInvoices.count)}</b> held
        </>
      ),
    },
    {
      key: "ar",
      label: "Accounts receivable",
      short: "AR",
      value: finance.ar,
      color: "var(--lilac)",
      series: series.ar,
      good: "none",
      delta: <Delta change={change(finance.trends.ar)} good="none" />,
      foot: dso ? (
        <>
          DSO <b className="fl-num">{Math.round(dso)}</b> days
        </>
      ) : undefined,
    },
    {
      key: "revenue",
      label: "Revenue MTD",
      short: "Rev MTD",
      value: finance.revenueMtd,
      color: "var(--yellow)",
      series: series.revenue,
      good: "up",
      delta: <span className="fh-delta flat">30d {usdShort(finance.revenue30d)}</span>,
      foot: (
        <>
          Margin <b className="fl-num">{pct1(finance.grossMarginPct)}</b> · 30 days
        </>
      ),
    },
  ];
  const note = emptyNote(source, "No ledger entries yet");
  return (
    <div className="fh-kpis fl-reveal" style={reveal(0)}>
      {tiles.map((t) => (
        <section key={t.key} className="fh-kpi" aria-label={`${t.label}: ${hasLedger ? usd(t.value) : note}`} style={{ "--c": t.color } as CSSProperties}>
          <div className="fh-kpi-head">
            <span className="fh-kpi-label">
              <span className="fh-long">{t.label}</span>
              <span className="fh-short" aria-hidden>
                {t.short}
              </span>
            </span>
            {hasLedger && t.delta}
          </div>
          <div className="fh-kpi-value fl-num">{hasLedger ? <CountUp value={t.value} format={usd} good={t.good} /> : <span className="fh-mute">—</span>}</div>
          <div className="fh-kpi-spark">{hasLedger ? <Sparkline values={t.series} color={t.color} height={40} /> : <div className="fh-spark-empty" />}</div>
          {hasLedger && t.foot && <div className="fh-kpi-foot">{t.foot}</div>}
        </section>
      ))}
    </div>
  );
}

/* ---------------------------- cash position ---------------------------- */

function CashPanel() {
  const { finance, snapshot, source } = useOps();
  const [range, setRange] = useState<7 | 30>(30);
  const [hover, setHover] = useState<number | null>(null);
  const points = useMemo(() => finance.trends.cash.slice(-(range + 1)), [finance.trends.cash, range]);
  const hasLedger = snapshot.ledger_entry.length > 0;
  const delta = change(points);
  const hovered = hover != null ? points[hover] : null;
  const forecast = finance.forecast.slice(0, 8);

  return (
    <section className="fl-panel fh-cash" aria-label="Cash position">
      <header className="fl-panel-head">
        <span className="fl-section">Cash</span>
        <span className="fh-head-meta">
          {hovered && (
            <span className="fh-cash-hover fl-num">
              <span className="fl-micro">{hover === points.length - 1 ? "Today" : dayLabel(hovered.t)}</span> {usd(hovered.value)}
            </span>
          )}
          <div className="fl-tabs fh-range" role="tablist" aria-label="Range">
            {([7, 30] as const).map((r) => (
              <button
                key={r}
                role="tab"
                aria-selected={range === r}
                className={`fl-tab ${range === r ? "active" : ""}`}
                onClick={() => {
                  setRange(r);
                  setHover(null);
                }}
              >
                {r}D
              </button>
            ))}
          </div>
        </span>
      </header>
      {hasLedger ? (
        <LineChart
          points={points}
          color="var(--teal)"
          drawKey={String(range)}
          hover={hover}
          onHover={setHover}
          label={`Cash position over ${range} days, currently ${usd(finance.cash)}, ${delta.abs >= 0 ? "up" : "down"} ${usd(Math.abs(delta.abs))}`}
        />
      ) : (
        <div className="fh-empty fh-cash-empty">
          <div className="fl-label strong">{emptyNote(source, "No ledger entries yet")}</div>
        </div>
      )}
      <div className="fh-fc-wrap">
        <div className="fh-fc-head">
          <span className="fl-section" style={{ "--accent": "var(--aqua)" } as CSSProperties}>
            Forecast
          </span>
        </div>
        {forecast.length ? <ForecastBars rows={forecast} /> : <div className="fh-empty-sub fh-fc-empty">{emptyNote(source, "No forecast yet")}</div>}
      </div>
    </section>
  );
}

/* ------------------------- autonomous operations ------------------------- */

function OpsStrip() {
  const { ops, caseList, now } = useOps();
  const dayStart = new Date(now).setHours(0, 0, 0, 0);
  const today = useMemo(() => caseList.filter((c) => c.openedAt >= dayStart).length, [caseList, dayStart]);

  const stats: { label: string; value: number; format: (n: number) => string; sub?: string }[] = [
    { label: "Cases today", value: today, format: count },
    { label: "Active cases", value: ops.casesActive, format: count },
    { label: "Automation", value: ops.automationRate * 100, format: pct1, sub: "last 20 min" },
    { label: "Throughput", value: ops.throughputPerHour, format: perHour, sub: "last hour" },
    { label: "Time saved (est.)", value: ops.minutesSaved / 60, format: hours },
  ];
  return (
    <section className="fl-panel fh-ops" aria-label="Operations">
      <header className="fl-panel-head">
        <span className="fl-section" style={{ "--accent": "var(--lilac)" } as CSSProperties}>
          Operations
        </span>
      </header>
      <div className="fh-ops-grid">
        {stats.map((s) => (
          <div key={s.label} className="fh-stat">
            <div className="fh-stat-label">{s.label}</div>
            <div className="fh-stat-value fl-num">
              <CountUp value={s.value} format={s.format} good="none" />
            </div>
            {s.sub && <div className="fh-stat-sub">{s.sub}</div>}
          </div>
        ))}
      </div>
    </section>
  );
}

/* ------------------------------ deep links ------------------------------ */

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
