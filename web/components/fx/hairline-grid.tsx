"use client";
/**
 * Senra-style hairline grid that draws itself in: horizontal lines scaleX from
 * 0, vertical lines scaleY from 0, quint.inOut, staggered (same motion as
 * `components/shell/intro.tsx`). Absolutely positioned, pointer-events:none
 * decoration for panels and empty states; fills its nearest positioned ancestor.
 *
 *   <HairlineGrid cols={6} rows={4} draw />
 *
 * `draw` toggles in/out. Under reduced motion the lines snap to their state.
 */
import { memo, useEffect, useRef, type CSSProperties } from "react";
import gsap from "gsap";

export type HairlineGridProps = {
  cols?: number;
  rows?: number;
  /** true draws the grid in, false retracts it. */
  draw?: boolean;
  /** Include the outer border lines. */
  edges?: boolean;
  /** Line colour; defaults to the theme hairline. */
  color?: string;
  /** Seconds before drawing in. */
  delay?: number;
  /** Seconds per line. */
  duration?: number;
  /** Fade toward the bottom like the intro grid. */
  fade?: boolean;
  className?: string;
  style?: CSSProperties;
};

const Lines = memo(function Lines({ cols, rows, edges, color }: { cols: number; rows: number; edges: boolean; color: string }) {
  const line: CSSProperties = { position: "absolute", background: color, transformOrigin: "0 0" };
  const xs = [];
  const ys = [];
  for (let i = edges ? 0 : 1; i <= (edges ? rows : rows - 1); i++) {
    const last = i === rows ? " - 1px" : "";
    xs.push(<i key={`x${i}`} data-x style={{ ...line, left: 0, right: 0, height: 1, top: `calc(${(i / rows) * 100}%${last})`, transform: "scaleX(0)" }} />);
  }
  for (let i = edges ? 0 : 1; i <= (edges ? cols : cols - 1); i++) {
    const last = i === cols ? " - 1px" : "";
    ys.push(<i key={`y${i}`} data-y style={{ ...line, top: 0, bottom: 0, width: 1, left: `calc(${(i / cols) * 100}%${last})`, transform: "scaleY(0)" }} />);
  }
  return (
    <>
      {xs}
      {ys}
    </>
  );
});

export default function HairlineGrid({
  cols = 8,
  rows = 5,
  draw = true,
  edges = true,
  color = "var(--line, rgba(255, 255, 255, 0.12))",
  delay = 0,
  duration = 1.4,
  fade = false,
  className = "",
  style,
}: HairlineGridProps) {
  const root = useRef<HTMLDivElement>(null);
  const safeCols = Math.max(1, Math.round(cols));
  const safeRows = Math.max(1, Math.round(rows));

  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const xs = el.querySelectorAll<HTMLElement>("i[data-x]");
    const ys = el.querySelectorAll<HTMLElement>("i[data-y]");
    const to = draw ? 1 : 0;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      gsap.set(xs, { scaleX: to });
      gsap.set(ys, { scaleY: to });
      return;
    }
    const d = draw ? duration : duration * 0.5;
    const tl = gsap.timeline({ defaults: { ease: "quint.inOut" }, delay: draw ? delay : 0 });
    tl.to(xs, { scaleX: to, duration: d, stagger: draw ? 0.08 : 0.03 }, 0).to(ys, { scaleY: to, duration: d, stagger: draw ? 0.06 : 0.02 }, draw ? 0.15 : 0);
    return () => {
      tl.kill();
    };
  }, [draw, safeCols, safeRows, edges, delay, duration]);

  const base: CSSProperties = {
    position: "absolute",
    inset: 0,
    pointerEvents: "none",
    overflow: "hidden",
    ...(fade ? { maskImage: "linear-gradient(to bottom, black 0 85%, transparent)", WebkitMaskImage: "linear-gradient(to bottom, black 0 85%, transparent)" } : null),
    ...style,
  };
  return (
    <div ref={root} className={`fl-hairline-grid ${className}`} style={base} aria-hidden>
      <Lines cols={safeCols} rows={safeRows} edges={edges} color={color} />
    </div>
  );
}
