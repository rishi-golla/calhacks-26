/**
 * Formatting helpers for the finance home. Formatters are created once: the
 * count-up animation calls them every frame.
 */
import { AGENT_META, NODE_META, isAgentId } from "@/lib/agents";
import type { TrendPoint } from "@/lib/ops";

const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const usdCompact = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});
const usdAxis = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 0,
});
const int = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

/** Whole dollars; never renders "-$0". */
export const usd = (v: number) => usd0.format(Math.round(v) || 0);
/** Compact above $10k ("$38.2K"), whole dollars below. */
export const usdShort = (v: number) => (Math.abs(v) >= 10_000 ? usdCompact.format(v) : usd(v));
export const usdTick = (v: number) => usdAxis.format(v);
export const signedShort = (v: number) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${usdShort(Math.abs(v))}`;
export const count = (v: number) => int.format(Math.round(v) || 0);

/** Change between the first and last point of a series. */
export function change(points: TrendPoint[]) {
  const first = points[0]?.value ?? 0;
  const last = points.at(-1)?.value ?? 0;
  // A series that starts at zero has no meaningful percentage change.
  return { abs: last - first, pct: Math.abs(first) >= 1 ? (last - first) / Math.abs(first) : null };
}

/**
 * Friendly agent nickname ("Haul", "Stamp"). The human controller is "Human",
 * not "You": on the simulation (or another operator's session) the viewer did
 * not make that decision.
 */
export const nick = (id: string) =>
  isAgentId(id) ? AGENT_META[id].nick : id === "intake" ? NODE_META.intake.nick : id === "human" ? "Human" : id || "System";

const dayFmt = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });
const monthFmt = new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric" });
const clockFmt = new Intl.DateTimeFormat("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

export const dayLabel = (t: number) => dayFmt.format(t);
export const clock = (t: number) => clockFmt.format(t);

/** Forecast periods are ISO dates (week start) or months; anything else is shown as-is. */
export function periodLabel(period: string) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(period)) return dayFmt.format(new Date(`${period}T12:00:00`));
  if (/^\d{4}-\d{2}$/.test(period)) return monthFmt.format(new Date(`${period}-15T12:00:00`));
  return period || "—";
}

export const humanize = (s: string) => s.replaceAll("_", " ");

/** Empty-state wording that never passes off "no connection" as "nothing happened". */
export const emptyNote = (source: string, whenEmpty: string) =>
  source === "connecting" ? "Connecting…" : source === "offline" ? "Database offline" : whenEmpty;
