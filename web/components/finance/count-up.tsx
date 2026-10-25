"use client";
/**
 * Animated number. Tweens from the previously shown value to the new one
 * (quint-out) by writing text directly, so a ticking page does not re-render
 * per frame, and pulses a tinted overlay (opacity only) on every change.
 * Reduced motion: jumps straight to the value, no pulse.
 */
import { useLayoutEffect, useRef, useState } from "react";

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

export function CountUp({
  value,
  format,
  duration = 800,
  good = "up",
  className = "",
}: {
  value: number;
  /** Must be a stable (module-level) function. */
  format: (n: number) => string;
  duration?: number;
  /** Which direction tints teal; the other tints orange. "none" pulses neutral. */
  good?: "up" | "down" | "none";
  className?: string;
}) {
  const text = useRef<HTMLSpanElement>(null);
  const pulse = useRef<HTMLSpanElement>(null);
  const shown = useRef(value);
  const [initial] = useState(() => format(value));

  useLayoutEffect(() => {
    const el = text.current;
    if (!el) return;
    const from = shown.current;
    const to = value;
    if (from === to || reducedMotion()) {
      shown.current = to;
      el.textContent = format(to);
      return;
    }
    const tone = good === "none" ? "var(--text)" : (to > from) === (good === "up") ? "var(--teal)" : "var(--orange)";
    if (pulse.current && from !== 0) {
      pulse.current.style.setProperty("--pulse", tone);
      pulse.current.animate([{ opacity: 0.9 }, { opacity: 0 }], { duration: 900, easing: "cubic-bezier(.23,1,.32,1)" });
    }
    let raf = 0;
    const start = performance.now();
    const step = (t: number) => {
      const p = Math.min(1, (t - start) / duration);
      const eased = 1 - Math.pow(1 - p, 5);
      const v = p >= 1 ? to : from + (to - from) * eased;
      shown.current = v;
      el.textContent = format(v);
      if (p < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, duration, format, good]);

  return (
    <span className={`fh-count ${className}`}>
      <span className="fh-count-pulse" ref={pulse} aria-hidden />
      <span ref={text}>{initial}</span>
    </span>
  );
}
