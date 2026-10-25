"use client";
/**
 * Finance home (`/`): the brokerage's financial health and the autonomous
 * back office that keeps it, in real time. Everything renders from `useOps()`
 * (live SpacetimeDB or the labeled browser simulation); nothing polls.
 *
 * Top to bottom: header → 4 KPI tiles → cash position + forecast | needs
 * attention → autonomous operations → agent crew → invoices & payments | live
 * ledger ticker.
 */
import { Suspense, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { useOps } from "@/components/data-provider";
import { AGENT_IDS } from "@/lib/agents";
import { fmtAgo, fmtDuration } from "@/lib/ops";
import { AgentCrew } from "./agent-crew";
import { AttentionQueue } from "./attention-queue";
import { ForecastBars, LineChart, MiniBars, Sparkline } from "./charts";
import { CountUp } from "./count-up";
import { change, count, dayLabel, emptyNote, signedShort, usd, usdShort } from "./format";
import { InvoicesPanel } from "./invoices-table";
import { LiveTicker } from "./live-ticker";
import "./finance.css";
import { DotWave } from "./dot-wave";

const pct1 = (v: number) => `${v.toFixed(1)}%`;
const perHour = (v: number) => `${Math.round(v)}/h`;
const hours = (v: number) => `${count(v)} h`;
const reveal = (i: number) => ({ "--delay": `${i * 70}ms` }) as CSSProperties;

export default function FinanceHome() {
  return (
    <div className="fl-page fh">
      <DotWave />
      <Header />
      <KpiTiles />
      <div className="fh-row fh-row-main fl-reveal" style={reveal(2)}>
        <CashPanel />
        <AttentionQueue id="attention" />
      </div>
      <div className="fl-reveal" style={reveal(3)}>
        <OpsStrip />
      </div>
      <div className="fl-reveal" style={reveal(4)}>
        <AgentCrew />
      </div>
      <div className="fh-row fh-row-bottom fl-reveal" style={reveal(5)}>
        <InvoicesPanel />
        <LiveTicker />
      </div>
      <Suspense fallback={null}>
        <FocusFromQuery />
      </Suspense>
    </div>
  );
}

/* ------------------------------- header ------------------------------- */

function Header() {
  const { source, simulated, ops, attention, now, lastUpdated } = useOps();
  const total = AGENT_IDS.length;
  const online = Math.min(total, ops.agentsOnline);
  const provenance = simulated ? "Simulated" : source === "live" ? "Live" : source === "fallback" ? "Snapshot" : source === "connecting" ? "Connecting" : "Offline";
  return (
    <header className="fh-header fl-reveal" style={reveal(0)}>
      <div className="fh-header-id">
        <div className="fl-section" style={{ "--accent": simulated ? "var(--lilac)" : "var(--teal)" } as CSSProperties}>
          Financial health · {provenance}
        </div>
        <h1 className="fl-display fh-wordmark">Golden Gate Freight</h1>
        <div className="fh-header-sub">
          Autonomous back office · <span className="fl-num">{count(ops.casesActive)}</span> open cases ·{" "}
          <span className="fl-num">{count(ops.tasksActive)}</span> tasks in flight
        </div>
      </div>
      <div className="fh-header-status">
        <div className="fh-chips">
          {simulated && (
            <span className="fl-chip agent outline" title="Rows are generated in this browser and are not recorded anywhere">
              Simulated
            </span>
          )}
          <span className={`fl-chip outline ${online === total ? "ok" : online > 0 ? "warn" : ""}`}>
            <i className={`fl-dot ${online ? "pulse" : ""}`} />
            {online}/{total} agents online
          </span>
          <a href="#attention" className={`fl-chip outline ${attention.length ? "hot" : source === "live" || simulated ? "ok" : ""}`}>
            {attention.length} need attention
          </a>
        </div>
        <div className="fl-micro" suppressHydrationWarning>
          {lastUpdated ? `Updated ${fmtAgo(lastUpdated, now)}` : source === "connecting" ? "Connecting…" : "No updates yet"}
        </div>
      </div>
    </header>
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
  foot: ReactNode;
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
      foot: (
        <>
          <span className={cashChange.abs >= 0 ? "fh-ok" : "fh-hot"}>{signedShort(cashChange.abs)}</span> vs 30 days ago
        </>
      ),
    },
    {
      key: "ap",
      label: "Accounts payable",
      short: "Payables",
      value: finance.ap,
      color: "var(--aqua)",
      series: series.ap,
      good: "none",
      delta: <Delta change={change(finance.trends.ap)} good="none" />,
      foot: (
        <>
          <b className="fl-num">{count(finance.outstandingInvoices.count)}</b> open invoices · <b className="fl-num">{count(finance.heldInvoices.count)}</b> held
          {finance.overdueInvoices.count > 0 && (
            <span className="fh-hot">
              {" "}
              · {count(finance.overdueInvoices.count)} overdue
            </span>
          )}
        </>
      ),
    },
    {
      key: "ar",
      label: "Accounts receivable",
      short: "Receivables",
      value: finance.ar,
      color: "var(--lilac)",
      series: series.ar,
      good: "none",
      delta: <Delta change={change(finance.trends.ar)} good="none" />,
      foot: dso ? (
        <>
          <b className="fl-num">{Math.round(dso)}</b> days sales outstanding
        </>
      ) : (
        "No revenue in the last 30 days"
      ),
    },
    {
      key: "revenue",
      label: "Revenue MTD",
      short: "Rev MTD",
      value: finance.revenueMtd,
      color: "var(--coral)",
      series: series.revenue,
      good: "up",
      delta: <span className="fh-delta flat">30d {usdShort(finance.revenue30d)}</span>,
      foot: (
        <>
          Gross margin <b className="fl-num">{pct1(finance.grossMarginPct)}</b> · 30 days
        </>
      ),
    },
  ];
  const note = emptyNote(source, "No ledger entries yet");
  return (
    <div className="fh-kpis fl-reveal" style={reveal(1)}>
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
          <div className="fh-kpi-foot">{hasLedger ? t.foot : note}</div>
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
  const sevenDayNet = forecast.slice(0, 1)[0]?.net ?? 0;

  return (
    <section className="fl-panel fh-cash" aria-label="Cash position">
      <header className="fl-panel-head">
        <span className="fl-section">Cash position · {range} days</span>
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
      </header>
      {hasLedger ? (
        <>
          <div className="fh-cash-top">
            <div className="fh-cash-value fl-num">
              <span className={hovered ? "fh-hidden" : ""}>
                <CountUp value={finance.cash} format={usd} good="up" />
              </span>
              {hovered && <span className="fh-cash-hover">{usd(hovered.value)}</span>}
            </div>
            <div className="fh-cash-sub">
              {hovered ? (
                <span className="fl-micro">{hover === points.length - 1 ? "Today" : dayLabel(hovered.t)} · closing balance</span>
              ) : (
                <>
                  <span className={delta.abs >= 0 ? "fh-ok" : "fh-hot"}>
                    {delta.abs >= 0 ? "▲" : "▼"} {signedShort(delta.abs)}
                    {delta.pct != null && ` (${Math.abs(delta.pct * 100).toFixed(1)}%)`}
                  </span>
                  <span className="fl-micro">past {range} days</span>
                </>
              )}
            </div>
          </div>
          <LineChart
            points={points}
            color="var(--teal)"
            drawKey={String(range)}
            hover={hover}
            onHover={setHover}
            label={`Cash position over ${range} days, currently ${usd(finance.cash)}, ${delta.abs >= 0 ? "up" : "down"} ${usd(Math.abs(delta.abs))}`}
          />
        </>
      ) : (
        <div className="fh-empty fh-cash-empty">
          <div>
            <div className="fl-label strong">{emptyNote(source, "No ledger entries yet")}</div>
            <div className="fh-empty-sub">
              {source === "offline" ? "Reconnect from the source menu in the top bar." : "The cash line starts with the first posted journal entry."}
            </div>
          </div>
        </div>
      )}
      <div className="fh-fc-wrap">
        <div className="fh-fc-head">
          <span className="fl-section" style={{ "--accent": "var(--aqua)" } as CSSProperties}>
            Cash forecast
          </span>
          {forecast.length > 0 && (
            <span className="fh-legend">
              <span>
                <i className="in" /> Inflow
              </span>
              <span>
                <i className="out" /> Outflow
              </span>
              <span>
                <i className="net" /> Net
              </span>
              <span className="fh-legend-note">Fade = lower confidence</span>
            </span>
          )}
        </div>
        {forecast.length ? (
          <>
            <ForecastBars rows={forecast} />
            <div className="fh-fc-note">
              Next period nets <b className={`fl-num ${sevenDayNet >= 0 ? "fh-ok" : "fh-hot"}`}>{signedShort(sevenDayNet)}</b> · {forecast.length} periods forecast
            </div>
          </>
        ) : (
          <div className="fh-empty-sub fh-fc-empty">
            {source === "offline" || source === "connecting" ? emptyNote(source, "") : "No forecast rows yet. Load Profitability (Margin) writes them as cases settle."}
          </div>
        )}
      </div>
    </section>
  );
}

/* ------------------------- autonomous operations ------------------------- */

const BUCKET_MS = 600_000;
const BUCKETS = 12;

function OpsStrip() {
  const { ops, caseList, now } = useOps();
  const bucketEnd = (Math.floor(now / BUCKET_MS) + 1) * BUCKET_MS;
  const dayStart = new Date(now).setHours(0, 0, 0, 0);
  const { buckets, today } = useMemo(() => {
    const b = new Array<number>(BUCKETS).fill(0);
    let opened = 0;
    for (const c of caseList) {
      if (c.openedAt >= dayStart) opened++;
      if (!c.resolvedAt) continue;
      const i = BUCKETS - 1 - Math.floor((bucketEnd - 1 - c.resolvedAt) / BUCKET_MS);
      if (i >= 0 && i < BUCKETS) b[i]++;
    }
    return { buckets: b, today: opened };
  }, [caseList, bucketEnd, dayStart]);

  const stats: { label: string; value: number; format: (n: number) => string; sub: string }[] = [
    { label: "Cases today", value: today, format: count, sub: `${count(ops.casesTotal)} all time` },
    { label: "Automation", value: ops.automationRate * 100, format: pct1, sub: "no human touch · last 20 min" },
    { label: "Active cases", value: ops.casesActive, format: count, sub: `${count(ops.tasksActive)} tasks in flight` },
    { label: "Throughput", value: ops.throughputPerHour, format: perHour, sub: "resolved, last hour" },
    { label: "Time saved", value: ops.minutesSaved / 60, format: hours, sub: "est. 18 min per case" },
    { label: "Handoffs", value: ops.handoffs, format: count, sub: "agent → agent" },
    { label: "Tool calls", value: ops.toolCalls, format: count, sub: "completed" },
    { label: "Median resolution", value: ops.medianResolutionS, format: fmtDuration, sub: "opened → resolved" },
  ];
  return (
    <section className="fl-panel fh-ops" aria-label="Autonomous operations">
      <header className="fl-panel-head">
        <span className="fl-section" style={{ "--accent": "var(--lilac)" } as CSSProperties}>
          Autonomous operations
        </span>
        <span className="fl-micro">
          {count(ops.approvalsPending)} approvals pending · {count(ops.holdsActive)} holds active
        </span>
      </header>
      <div className="fh-ops-grid">
        <div className="fh-ops-hero">
          <div className="fh-stat-label">Cases resolved</div>
          <div className="fh-stat-value fl-num">
            <CountUp value={ops.casesResolved} format={count} />
          </div>
          <div className="fh-stat-sub">
            {count(ops.casesResolvedToday)} today · per 10 min below
          </div>
          <div role="img" aria-label={`Cases resolved per 10 minutes over the last 2 hours: ${buckets.join(", ")}`}>
            <MiniBars values={buckets} color="var(--lilac)" />
            <div className="fh-minibars-axis">
              <span>−2h</span>
              <span>−1h</span>
              <span>{buckets[BUCKETS - 1]} now</span>
            </div>
          </div>
        </div>
        {stats.map((s) => (
          <div key={s.label} className="fh-stat">
            <div className="fh-stat-label">{s.label}</div>
            <div className="fh-stat-value fl-num">
              <CountUp value={s.value} format={s.format} good="none" />
            </div>
            <div className="fh-stat-sub">{s.sub}</div>
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
