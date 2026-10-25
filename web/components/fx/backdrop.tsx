"use client";
/**
 * Full-bleed animated backdrop.
 *
 * API contract (keep stable; pages import this):
 *   <Backdrop variant="field" | "flow" | "glow" intensity={0..1} className style />
 * Renders an absolutely positioned, pointer-events:none layer that fills its
 * nearest positioned ancestor. The WebGPU implementation (vgpu) lives in
 * `./backdrop-gpu.tsx` (a still frame under `prefers-reduced-motion`); this
 * wrapper falls back to the CSS-only `./backdrop-css.tsx` when WebGPU is
 * unavailable, and the GPU path falls back to it on any runtime error.
 */
import dynamic from "next/dynamic";
import { useEffect, useState, type CSSProperties } from "react";
import BackdropCss from "./backdrop-css";

export type BackdropVariant = "field" | "flow" | "glow";
export type BackdropProps = {
  variant?: BackdropVariant;
  /** 0..1, scales particle density / brightness. */
  intensity?: number;
  /** Accent color in hex, defaults to teal. */
  color?: string;
  className?: string;
  style?: CSSProperties;
};

const Gpu = dynamic(() => import("./backdrop-gpu"), { ssr: false, loading: () => null });

/** null until checked on the client, then whether `navigator.gpu` exists. */
export function useWebGpuAvailable() {
  const [ok, setOk] = useState<boolean | null>(null);
  useEffect(() => {
    setOk(typeof navigator !== "undefined" && "gpu" in navigator);
  }, []);
  return ok;
}

export default function Backdrop(props: BackdropProps) {
  const gpu = useWebGpuAvailable();
  if (gpu) return <Gpu {...props} />;
  if (gpu === false) return <BackdropCss {...props} />;
  // Not detected yet (SSR and first client render): an empty layer, so the CSS
  // version does not flash before the GPU canvas fades in.
  const { variant = "field", className = "", style } = props;
  const base: CSSProperties = { position: "absolute", inset: 0, pointerEvents: "none", overflow: "hidden", ...style };
  return <div className={`fl-backdrop fl-backdrop-${variant} ${className}`} style={base} aria-hidden />;
}
