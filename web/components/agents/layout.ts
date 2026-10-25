/**
 * Fixed geometry for the agent universe.
 *
 * Positions live in a 1200×700 logical canvas that the page scales to fit.
 * They are hand-tuned and stable on purpose: an operator learns where each
 * agent sits, so nothing reflows when traffic changes. Columns follow the work:
 * Intake → frontline → investigators → support → profitability / review, with
 * the Human controller on the right edge when an approval is waiting.
 */
import type { NodeId } from "@/lib/agents";

export const STAGE_W = 1200;
export const STAGE_H = 700;

export type Point = { x: number; y: number };

/** Mascot centers. Labels and chips hang below. */
export const NODE_POS: Record<NodeId, Point> = {
  intake: { x: 104, y: 340 },
  carrier_ap: { x: 322, y: 196 },
  shipper_ar: { x: 322, y: 486 },
  risk: { x: 572, y: 92 },
  documents: { x: 572, y: 330 },
  verification: { x: 572, y: 562 },
  controller: { x: 822, y: 196 },
  voice_desk: { x: 822, y: 486 },
  cfo: { x: 1030, y: 140 },
  validator: { x: 1030, y: 430 },
  human: { x: 1134, y: 316 },
};

/** Text column width per node (logical px). */
export const NODE_WIDTH: Partial<Record<NodeId, number>> = { cfo: 176, validator: 176, human: 150, intake: 190 };

export const MASCOT_SIZE: Record<NodeId, number> = {
  intake: 92,
  carrier_ap: 84,
  shipper_ar: 84,
  risk: 80,
  documents: 80,
  verification: 80,
  controller: 80,
  voice_desk: 80,
  cfo: 76,
  validator: 76,
  human: 60,
};

/** Where brand-new cases fly in from (off the left edge). */
const ENTRY: Point = { x: -40, y: 340 };

/*
 * Vertical stretch: on screens with spare height the rows spread apart (the
 * columns and the relative layout never change), so the map fills the
 * viewport instead of floating in a band.
 */
let stretch = 1;

/** Returns true when the value changed (curves are recomputed). */
export function setStretch(next: number): boolean {
  const v = Math.round(next * 100) / 100;
  if (v === stretch) return false;
  stretch = v;
  cache.clear();
  return true;
}

export const stageHeight = () => STAGE_H * stretch;

export function nodePos(id: NodeId | "entry"): Point {
  const p = id === "entry" ? ENTRY : NODE_POS[id];
  return { x: p.x, y: p.y * stretch };
}

export const NODE_ORDER: NodeId[] = [
  "intake",
  "carrier_ap",
  "shipper_ar",
  "risk",
  "documents",
  "verification",
  "controller",
  "voice_desk",
  "cfo",
  "validator",
  "human",
];

/* ------------------------------ curves ------------------------------ */

export type Curve = { p0: Point; c1: Point; c2: Point; p3: Point; d: string; mid: Point };

const bez = (a: number, b: number, c: number, d: number, t: number) => {
  const u = 1 - t;
  return u * u * u * a + 3 * u * u * t * b + 3 * u * t * t * c + t * t * t * d;
};

export function curveAt(c: Curve, t: number): Point {
  return { x: bez(c.p0.x, c.c1.x, c.c2.x, c.p3.x, t), y: bez(c.p0.y, c.c1.y, c.c2.y, c.p3.y, t) };
}

/** Tangent angle in degrees at t (for tilting packets along the path). */
export function curveAngle(c: Curve, t: number): number {
  const u = 1 - t;
  const dx = 3 * u * u * (c.c1.x - c.p0.x) + 6 * u * t * (c.c2.x - c.c1.x) + 3 * t * t * (c.p3.x - c.c2.x);
  const dy = 3 * u * u * (c.c1.y - c.p0.y) + 6 * u * t * (c.c2.y - c.c1.y) + 3 * t * t * (c.p3.y - c.c2.y);
  return (Math.atan2(dy, dx) * 180) / Math.PI;
}

function build(p0: Point, p3: Point, bend: number): Curve {
  const dx = p3.x - p0.x;
  const dy = p3.y - p0.y;
  const len = Math.hypot(dx, dy) || 1;
  // Perpendicular offset: a gentle arc, mirrored for the reverse direction so
  // A→B and B→A traffic never share a line.
  const nx = (-dy / len) * len * bend;
  const ny = (dx / len) * len * bend;
  const c1 = { x: p0.x + dx * 0.3 + nx, y: p0.y + dy * 0.3 + ny };
  const c2 = { x: p0.x + dx * 0.7 + nx, y: p0.y + dy * 0.7 + ny };
  const curve: Curve = {
    p0,
    c1,
    c2,
    p3,
    d: `M${p0.x.toFixed(1)} ${p0.y.toFixed(1)} C${c1.x.toFixed(1)} ${c1.y.toFixed(1)} ${c2.x.toFixed(1)} ${c2.y.toFixed(1)} ${p3.x.toFixed(1)} ${p3.y.toFixed(1)}`,
    mid: { x: 0, y: 0 },
  };
  curve.mid = curveAt(curve, 0.5);
  return curve;
}

/** Smallest distance from the curve (sampled) to any node other than its ends. */
function clearance(c: Curve, skip: Set<string>): number {
  let best = Infinity;
  for (const id of NODE_ORDER) {
    if (skip.has(id)) continue;
    const n = nodePos(id);
    for (let i = 2; i <= 18; i++) {
      const p = curveAt(c, i / 20);
      const d = Math.hypot(p.x - n.x, p.y - n.y);
      if (d < best) best = d;
    }
  }
  return best;
}

const cache = new Map<string, Curve>();

/**
 * Curve between two nodes (or from the off-canvas entry when `from` is
 * "entry"). The arc bends just enough to clear every other mascot.
 */
export function edgeCurve(from: NodeId | "entry", to: NodeId): Curve {
  const key = `${from}>${to}`;
  const hit = cache.get(key);
  if (hit) return hit;
  const p0 = nodePos(from);
  const p3 = nodePos(to);
  const skip = new Set<string>([from, to]);
  let curve = build(p0, p3, 0.12);
  if (from !== "entry") {
    let clear = clearance(curve, skip);
    for (const bend of [0.2, 0.28, 0.36, -0.2, -0.3]) {
      if (clear > 78) break;
      const c = build(p0, p3, bend);
      const cl = clearance(c, skip);
      if (cl > clear) {
        curve = c;
        clear = cl;
      }
    }
  }
  cache.set(key, curve);
  return curve;
}

export const edgeKey = (from: string, to: string) => `${from}>${to}`;
