"use client";
/**
 * Inline-SVG charts for the finance home. No chart library: sparklines are a
 * single path, the cash chart is three stacked layers (axes, a line layer
 * revealed with `fl-draw`, and a hover/live-dot overlay), and bar charts are
 * divs scaled with `transform` so updates animate on the compositor.
 */
import { useEffect, useId, useMemo, useRef, useState, type CSSProperties, type PointerEvent } from "react";
import type { FinanceMetrics, TrendPoint } from "@/lib/ops";
import { dayLabel, periodLabel, signedShort, usdTick } from "./format";

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

const svgId = (id: string) => id.replace(/[^a-zA-Z0-9_-]/g, "");

function extent(values: number[], pad: number): [number, number] {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (!Number.isFinite(lo)) return [0, 1];
  if (lo === hi) {
    const p = Math.abs(lo) * 0.05 || 1;
    return [lo - p, hi + p];
  }
  const p = (hi - lo) * pad;
  return [lo - p, hi + p];
}

function niceTicks(lo: number, hi: number, target: number) {
  const raw = (hi - lo) / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  const step = (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
  const ticks: number[] = [];
  const first = Math.ceil(lo / step);
  // Multiply instead of accumulating so ticks stay exact (no "-$0").
  for (let i = first; i * step <= hi; i++) ticks.push(i * step === 0 ? 0 : Number((i * step).toPrecision(12)));
  return ticks;
}

/* ------------------------------ sparkline ------------------------------ */

export function Sparkline({ values, color, height = 40 }: { values: number[]; color: string; height?: number }) {
  const gid = svgId(useId());
  const { line, area } = useMemo(() => {
    if (values.length < 2) return { line: "", area: "" };
    const [lo, hi] = extent(values, 0.08);
    const step = 100 / (values.length - 1);
    const pts = values.map((v, i) => `${(i * step).toFixed(2)},${(height - ((v - lo) / (hi - lo)) * height).toFixed(2)}`);
    const d = `M${pts.join("L")}`;
    return { line: d, area: `${d}L100,${height}L0,${height}Z` };
  }, [values, height]);
  return (
    <svg className="fh-spark" viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" height={height} aria-hidden>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity="0.26" />
          <stop offset="1" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {area && <path d={area} fill={`url(#${gid})`} />}
      {line && <path d={line} fill="none" stroke={color} strokeWidth="1.5" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />}
    </svg>
  );
}

/* ------------------------------ cash chart ------------------------------ */

const PAD_R = 60;
const PAD_T = 12;
const PAD_B = 24;

/**
 * 30-day line with gridlines, date axis, a live dot on the latest point and a
 * hover crosshair. `onHover` reports the hovered index (or null) so the panel
 * header can show that day's value, Robinhood-style.
 */
export function LineChart({
  points,
  color,
  height = 232,
  drawKey,
  label,
  hover,
  onHover,
}: {
  points: TrendPoint[];
  color: string;
  height?: number;
  /** Changing this replays the draw-in reveal (e.g. on range change). */
  drawKey: string;
  label: string;
  hover: number | null;
  onHover: (index: number | null) => void;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const gid = svgId(useId());
  const geo = useMemo(() => {
    if (!width || points.length < 2) return null;
    const values = points.map((p) => p.value);
    const [lo0, hi0] = extent(values, 0.14);
    const ticks = niceTicks(lo0, hi0, 4);
    const lo = Math.min(lo0, ticks[0] ?? lo0);
    const hi = Math.max(hi0, ticks.at(-1) ?? hi0);
    const plotW = Math.max(40, width - PAD_R);
    const base = height - PAD_B;
    const x = (i: number) => (i / (points.length - 1)) * plotW;
    const y = (v: number) => PAD_T + (1 - (v - lo) / (hi - lo)) * (base - PAD_T);
    const xy = points.map((p, i) => [x(i), y(p.value)] as const);
    const line = `M${xy.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join("L")}`;
    const every = Math.max(1, Math.ceil(points.length / Math.max(2, Math.floor(plotW / 90))));
    const xLabels: { x: number; text: string; anchor: "start" | "middle" | "end" }[] = [];
    for (let i = 0; i < points.length; i += every)
      if (points.length - 1 - i >= every * 0.6 || i === 0)
        xLabels.push({ x: x(i), text: dayLabel(points[i].t), anchor: i === 0 ? "start" : "middle" });
    xLabels.push({ x: plotW, text: "Today", anchor: "end" });
    return { xy, line, area: `${line}L${plotW},${base}L0,${base}Z`, ticks: ticks.map((v) => ({ v, y: y(v) })), plotW, base, xLabels };
  }, [width, points, height]);

  const move = (e: PointerEvent<HTMLDivElement>) => {
    if (!geo) return;
    const px = e.clientX - e.currentTarget.getBoundingClientRect().left;
    const i = Math.round((px / geo.plotW) * (points.length - 1));
    onHover(Math.max(0, Math.min(points.length - 1, i)));
  };
  const last = geo?.xy.at(-1);
  const hp = geo && hover != null ? geo.xy[hover] : null;

  return (
    <div
      ref={ref}
      className="fh-chart"
      style={{ height }}
      role="img"
      aria-label={label}
      onPointerMove={move}
      onPointerDown={move}
      onPointerLeave={() => onHover(null)}
    >
      {geo && (
        <>
          <svg className="fh-chart-layer" width={width} height={height} aria-hidden>
            {geo.ticks.map((t) => (
              <g key={t.v}>
                <line x1={0} x2={geo.plotW} y1={t.y} y2={t.y} className="fh-grid" />
                <text x={geo.plotW + 10} y={t.y + 4} className="fh-axis">
                  {usdTick(t.v)}
                </text>
              </g>
            ))}
            <line x1={0} x2={geo.plotW} y1={geo.base} y2={geo.base} className="fh-baseline" />
            {geo.xLabels.map((l) => (
              <text key={`${l.text}-${l.x}`} x={l.x} y={height - 6} textAnchor={l.anchor} className="fh-axis">
                {l.text}
              </text>
            ))}
          </svg>
          <div className="fh-chart-layer fl-draw" key={drawKey}>
            <svg width={width} height={height} aria-hidden>
              <defs>
                <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stopColor={color} stopOpacity="0.22" />
                  <stop offset="1" stopColor={color} stopOpacity="0" />
                </linearGradient>
              </defs>
              <path d={geo.area} fill={`url(#${gid})`} />
              <path d={geo.line} fill="none" stroke={color} strokeWidth={1.75} strokeLinejoin="round" strokeLinecap="round" />
            </svg>
          </div>
          <svg className="fh-chart-layer" width={width} height={height} aria-hidden>
            {last && !hp && (
              <g className="fh-live-dot" key={drawKey} style={{ "--c": color } as CSSProperties}>
                <circle cx={last[0]} cy={last[1]} r={9} className="fh-live-ring" />
                <circle cx={last[0]} cy={last[1]} r={3.5} fill={color} />
              </g>
            )}
            {hp && (
              <g>
                <line x1={hp[0]} x2={hp[0]} y1={PAD_T - 4} y2={geo.base} className="fh-cross" />
                <circle cx={hp[0]} cy={hp[1]} r={4} fill="var(--bg-0)" stroke={color} strokeWidth={2} />
              </g>
            )}
          </svg>
        </>
      )}
    </div>
  );
}

/* ---------------------------- forecast bars ---------------------------- */

/** Inflow up, outflow down from a zero line; bar opacity follows confidence. */
export function ForecastBars({ rows }: { rows: FinanceMetrics["forecast"] }) {
  const max = Math.max(1, ...rows.flatMap((r) => [r.inflow, r.outflow]));
  const label = `Cash forecast by period: ${rows.map((r) => `${periodLabel(r.period)} net ${signedShort(r.net)}`).join(", ")}`;
  return (
    <div className="fh-fc" role="img" aria-label={label}>
      {rows.map((r, i) => {
        const alpha = 0.35 + 0.65 * Math.max(0, Math.min(1, r.confidence));
        return (
          <div className="fh-fc-col" key={r.period} style={{ "--i": i } as CSSProperties}>
            <div className="fh-fc-plot">
              <div className="fh-fc-half up">
                <i className="fh-bar in" style={{ transform: `scaleY(${r.inflow / max})`, opacity: alpha }} />
              </div>
              <div className="fh-fc-half down">
                <i className="fh-bar out" style={{ transform: `scaleY(${r.outflow / max})`, opacity: alpha }} />
              </div>
              <i className="fh-fc-net" style={{ top: `${50 - (r.net / max) * 50}%` }} />
            </div>
            <div className={`fh-fc-value fl-num ${r.net >= 0 ? "ok" : "hot"}`}>{signedShort(r.net)}</div>
            <div className="fh-fc-meta">
              <span>{periodLabel(r.period)}</span>
              <span>{Math.round(r.confidence * 100)}%</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------ mini bars ------------------------------ */

/** Small column chart (e.g. cases resolved per 10-minute bucket). Oldest first. */
export function MiniBars({ values, color, height = 56 }: { values: number[]; color: string; height?: number }) {
  const max = Math.max(1, ...values);
  return (
    <div className="fh-minibars" style={{ height }} aria-hidden>
      {values.map((v, i) => (
        <span key={i} className="fh-minibar-slot">
          <i
            className="fh-bar"
            style={{ transform: `scaleY(${Math.max(v ? 0.04 : 0.015, v / max)})`, background: color, opacity: i === values.length - 1 ? 1 : 0.55 }}
          />
        </span>
      ))}
    </div>
  );
}
