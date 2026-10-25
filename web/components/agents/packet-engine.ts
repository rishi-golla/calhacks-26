/**
 * Imperative packet layer for the agent universe.
 *
 * Every handoff flies a tiny document card along the edge between two nodes.
 * At hundreds of events a minute this must not touch React: the engine owns
 * one absolutely positioned layer, a fixed pool of packet elements, and a
 * single requestAnimationFrame loop that writes only `transform` and
 * `opacity`. Events that arrive together are batched per edge into one packet
 * with a ×N badge, and concurrent packets are capped.
 *
 * The engine also owns two bits of per-node DOM that change on every event
 * and would otherwise re-render the page: the "received" counter (which ticks
 * up when a packet lands, not when the row appears) and the activity ticker.
 */
import { agentColor, skillLabel, type NodeId } from "@/lib/agents";
import { curveAngle, curveAt, edgeCurve, edgeKey, type Curve } from "./layout";

export type PacketKind = "doc" | "invoice" | "phone" | "check" | "case" | "stamp";

export type PacketSpec = {
  from: NodeId | "entry";
  to: NodeId;
  skill: string;
  caseId: string;
  /** Tag text override (defaults to the skill label). */
  label?: string;
  kind?: PacketKind;
  /** False when the arrival does not correspond to a "received" count (e.g. an approval decision going back). */
  counted?: boolean;
};

export type Visibility = "show" | "dim" | "hide";

type NodeHandle = {
  flash?: HTMLElement | null;
  bump?: HTMLElement | null;
  count?: HTMLElement | null;
  ticker?: HTMLElement | null;
};

type Packet = {
  el: HTMLDivElement;
  card: HTMLDivElement;
  tag: HTMLSpanElement;
  badge: HTMLSpanElement;
  kind: PacketKind | "";
  curve: Curve;
  edge: string;
  to: NodeId;
  start: number;
  dur: number;
  count: number;
  counted: number;
  arrived: boolean;
  arrivedAt: number;
  dim: boolean;
  caseIds: Set<string>;
};

const quintOut = (t: number) => 1 - Math.pow(1 - t, 5);
/** After landing, a packet is absorbed into the node over this long. */
const ABSORB_MS = 240;
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

const ICON: Record<PacketKind, string> = {
  doc: "<i></i><i></i><i></i>",
  case: "<i></i><i></i><i></i><b class=\"au-pk-new\"></b>",
  invoice: "<i></i><i></i><em>$</em>",
  phone:
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.2 2.5h2.1l1 3-1.4 1a7.2 7.2 0 0 0 3.6 3.6l1-1.4 3 1v2.1a1 1 0 0 1-1 1A10.8 10.8 0 0 1 3.2 3.5a1 1 0 0 1 1-1z"/></svg>',
  check: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.4l3 3 6-7"/></svg>',
  stamp: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 2.5h4l-.8 5h2.8v2.5H4V7.5h2.8zM3.5 12.5h9"/></svg>',
};

export function packetKindForSkill(skill: string): PacketKind {
  if (/call/.test(skill)) return "phone";
  if (/^(verify|review|check|screen|approve)/.test(skill)) return "check";
  if (/(invoice|payment|reconcile|overdue|credit|financial|forecast)/.test(skill)) return "invoice";
  return "doc";
}

export class PacketEngine {
  /** Maximum packets in flight; extra events merge into ×N badges. */
  cap = 40;
  /** Flight time in ms. */
  duration = 1250;

  private layer: HTMLDivElement | null = null;
  private pool: Packet[] = [];
  private live: Packet[] = [];
  private nodes = new Map<NodeId, NodeHandle>();
  private base = new Map<NodeId, number>();
  private inflight = new Map<NodeId, number>();
  private written = new Map<NodeId, string>();
  private countsDirty = false;
  private lastFlash = new Map<NodeId, number>();
  private lastTicker = new Map<NodeId, number>();
  private raf = 0;
  private reduced = false;
  private visibility: (caseId: string) => Visibility = () => "show";

  attach(layer: HTMLDivElement) {
    this.layer = layer;
    this.reduced = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.countsDirty = true;
    this.schedule();
  }

  detach() {
    cancelAnimationFrame(this.raf);
    this.raf = 0;
    for (const p of this.live) this.release(p);
    this.live = [];
    for (const p of this.pool) p.el.remove();
    this.pool = [];
    this.layer = null;
    this.inflight.clear();
  }

  registerNode(id: NodeId, handle: NodeHandle) {
    this.nodes.set(id, handle);
    this.written.delete(id);
    this.countsDirty = true;
    this.schedule();
  }

  unregisterNode(id: NodeId) {
    this.nodes.delete(id);
    this.written.delete(id);
  }

  /** Received-handoff totals from the snapshot; the display lags by what is still in flight. */
  setBase(counts: Partial<Record<NodeId, number>>) {
    for (const [id, n] of Object.entries(counts) as [NodeId, number][]) {
      if (this.base.get(id) !== n) {
        this.base.set(id, n);
        this.countsDirty = true;
      }
    }
    if (this.countsDirty) this.schedule();
  }

  setVisibility(fn: (caseId: string) => Visibility) {
    this.visibility = fn;
    for (const p of this.live) {
      let best: Visibility = "hide";
      for (const id of p.caseIds) {
        const v = fn(id);
        if (v === "show") {
          best = "show";
          break;
        }
        if (v === "dim") best = "dim";
      }
      p.dim = best !== "show";
    }
  }

  /** Short activity line under a node (latest tool call, document, outcome). */
  ticker(id: NodeId, text: string) {
    const el = this.nodes.get(id)?.ticker;
    if (!el) return;
    const now = performance.now();
    if (now - (this.lastTicker.get(id) ?? 0) < 280) return;
    this.lastTicker.set(id, now);
    el.textContent = text;
    if (!this.reduced) el.animate([{ opacity: 0.25, transform: "translateY(3px)" }, { opacity: 1, transform: "none" }], { duration: 320, easing: "cubic-bezier(.23,1,.32,1)" });
  }

  /** Light up a node without a packet (e.g. a task finished in place). */
  pulse(id: NodeId, strong = false) {
    this.flash(id, strong);
  }

  emit(specs: PacketSpec[]) {
    if (!specs.length) return;
    const animate = Boolean(this.layer) && !this.reduced && !(typeof document !== "undefined" && document.hidden);
    // Group the batch by edge so a burst becomes one packet with a ×N badge.
    const groups = new Map<string, { spec: PacketSpec; n: number; counted: number; cases: Set<string>; vis: Visibility; skills: Map<string, number> }>();
    for (const s of specs) {
      const vis = this.visibility(s.caseId);
      if (vis === "hide" || !animate) {
        if (!animate && vis !== "hide") this.flash(s.to, false);
        continue;
      }
      const key = `${edgeKey(s.from, s.to)}|${s.kind ?? packetKindForSkill(s.skill)}`;
      const g = groups.get(key);
      if (g) {
        g.n += 1;
        if (s.counted !== false) g.counted += 1;
        g.cases.add(s.caseId);
        g.skills.set(s.skill, (g.skills.get(s.skill) ?? 0) + 1);
        if (vis === "show") g.vis = "show";
      } else
        groups.set(key, {
          spec: s,
          n: 1,
          counted: s.counted === false ? 0 : 1,
          cases: new Set([s.caseId]),
          vis,
          skills: new Map([[s.skill, 1]]),
        });
    }
    const now = performance.now();
    let i = 0;
    for (const g of groups.values()) {
      const s = g.spec;
      const edge = edgeKey(s.from, s.to);
      if (this.live.length >= this.cap) {
        // Over the cap: fold into a young packet on the same edge, else count without animating.
        const host = this.live.find((p) => p.edge === edge && !p.arrived && now - p.start < p.dur * 0.35);
        if (host) {
          host.count += g.n;
          host.counted += g.counted;
          for (const c of g.cases) host.caseIds.add(c);
          if (g.vis === "show") host.dim = false;
          this.addInflight(s.to, g.counted);
          host.badge.textContent = `×${host.count}`;
          host.badge.style.display = "";
        }
        continue;
      }
      const p = this.acquire();
      if (!p) continue;
      const skill = [...g.skills.entries()].sort((a, b) => b[1] - a[1])[0][0];
      const kind = s.kind ?? packetKindForSkill(skill);
      p.curve = edgeCurve(s.from, s.to);
      p.edge = edge;
      p.to = s.to;
      p.start = now + i * 55;
      p.dur = this.duration + (i % 4) * 60;
      p.count = g.n;
      p.counted = g.counted;
      p.arrived = false;
      p.arrivedAt = 0;
      p.dim = g.vis !== "show";
      p.caseIds = g.cases;
      if (p.kind !== kind) {
        p.kind = kind;
        p.card.className = `au-pk-card k-${kind}`;
        p.card.innerHTML = ICON[kind];
      }
      p.card.style.setProperty("--from", s.from === "entry" ? "#ecedf4" : agentColor(s.from));
      p.el.style.setProperty("--to", agentColor(s.to));
      p.tag.textContent = g.skills.size > 1 && !s.label ? `${skillLabel(skill)} +${g.skills.size - 1}` : (s.label ?? skillLabel(skill));
      // Dense traffic: drop the text tags and keep the cards, so the map stays readable.
      p.tag.style.display = this.live.length > 14 ? "none" : "";
      p.badge.textContent = `×${g.n}`;
      p.badge.style.display = g.n > 1 ? "" : "none";
      p.el.style.opacity = "0";
      this.live.push(p);
      this.addInflight(s.to, g.counted);
      i += 1;
    }
    this.schedule();
  }

  /* ------------------------------ internals ------------------------------ */

  private addInflight(id: NodeId, n: number) {
    if (!n) return;
    this.inflight.set(id, (this.inflight.get(id) ?? 0) + n);
    this.countsDirty = true;
  }

  private acquire(): Packet | null {
    if (!this.layer) return null;
    const pooled = this.pool.pop();
    if (pooled) {
      pooled.el.style.visibility = "visible";
      return pooled;
    }
    const el = document.createElement("div");
    el.className = "au-pk";
    const card = document.createElement("div");
    card.className = "au-pk-card";
    const tag = document.createElement("span");
    tag.className = "au-pk-tag";
    const badge = document.createElement("span");
    badge.className = "au-pk-n";
    el.append(card, tag, badge);
    this.layer.append(el);
    return { el, card, tag, badge, kind: "", curve: edgeCurve("intake", "carrier_ap"), edge: "", to: "intake", start: 0, dur: 1, count: 1, counted: 0, arrived: false, arrivedAt: 0, dim: false, caseIds: new Set() };
  }

  private release(p: Packet) {
    p.el.style.opacity = "0";
    p.el.style.visibility = "hidden";
    p.el.style.transform = "translate3d(-200px,-200px,0)";
    this.pool.push(p);
  }

  private arrive(p: Packet, now: number) {
    p.arrived = true;
    p.arrivedAt = now;
    if (p.counted) {
      this.inflight.set(p.to, Math.max(0, (this.inflight.get(p.to) ?? 0) - p.counted));
      this.countsDirty = true;
    }
    if (!p.dim) this.flash(p.to, p.count > 1);
  }

  private flash(id: NodeId, strong: boolean) {
    if (this.reduced) return;
    const h = this.nodes.get(id);
    if (!h) return;
    const now = performance.now();
    if (now - (this.lastFlash.get(id) ?? 0) < 180) return;
    this.lastFlash.set(id, now);
    const ease = "cubic-bezier(.23,1,.32,1)";
    h.flash?.animate(
      [
        { transform: "scale(.72)", opacity: strong ? 1 : 0.85 },
        { transform: `scale(${strong ? 1.75 : 1.5})`, opacity: 0 },
      ],
      { duration: 760, easing: ease },
    );
    h.bump?.animate([{ transform: "scale(1)" }, { transform: "scale(1.07)", offset: 0.35 }, { transform: "scale(1)" }], { duration: 420, easing: ease });
    h.count?.animate([{ transform: "translateY(-3px)", opacity: 0.4 }, { transform: "none", opacity: 1 }], { duration: 360, easing: ease });
  }

  private writeCounts() {
    this.countsDirty = false;
    for (const [id, h] of this.nodes) {
      if (!h.count) continue;
      const v = Math.max(0, (this.base.get(id) ?? 0) - (this.inflight.get(id) ?? 0));
      const text = v.toLocaleString("en-US");
      if (this.written.get(id) !== text) {
        h.count.textContent = text;
        this.written.set(id, text);
      }
    }
  }

  private schedule() {
    if (!this.raf && this.layer) this.raf = requestAnimationFrame(this.frame);
  }

  private frame = (now: number) => {
    this.raf = 0;
    let keep = 0;
    for (let k = 0; k < this.live.length; k++) {
      const p = this.live[k];
      const t = (now - p.start) / p.dur;
      if (t < 0) {
        this.live[keep++] = p;
        continue;
      }
      if (t >= 1) {
        if (!p.arrived) this.arrive(p, now);
        this.release(p);
        continue;
      }
      const e = quintOut(t);
      if (!p.arrived && e > 0.975) this.arrive(p, now);
      let opacity = clamp(t / 0.06, 0, 1);
      let scale = 1;
      if (p.arrived) {
        // Absorbed into the node: shrink and fade instead of idling on the quint tail.
        const k2 = (now - p.arrivedAt) / ABSORB_MS;
        if (k2 >= 1) {
          this.release(p);
          continue;
        }
        opacity *= 1 - k2;
        scale = 1 - 0.6 * k2;
      }
      const pt = curveAt(p.curve, e);
      const tilt = clamp(curveAngle(p.curve, e) * 0.12, -12, 12) * (1 - e);
      p.el.style.transform = `translate3d(${pt.x.toFixed(1)}px,${pt.y.toFixed(1)}px,0) rotate(${tilt.toFixed(1)}deg) scale(${scale.toFixed(3)})`;
      p.el.style.opacity = (opacity * (p.dim ? 0.18 : 1)).toFixed(3);
      this.live[keep++] = p;
    }
    this.live.length = keep;
    if (this.countsDirty) this.writeCounts();
    if (this.live.length) this.schedule();
  };
}
