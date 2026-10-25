"use client";
/**
 * WebGPU backdrop (vgpu). One fullscreen WGSL pass per variant (`./shaders.ts`)
 * driven by `./backdrop-runtime.ts`. Any init, pipeline or device error swaps in
 * the CSS fallback, so this never throws into React.
 */
import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { BackdropProps } from "./backdrop";
import BackdropCss from "./backdrop-css";
import { startBackdrop, type BackdropRuntime } from "./backdrop-runtime";

export default function BackdropGpu(props: BackdropProps) {
  const { variant = "field", intensity = 0.6, color = "#22c09b", className = "", style } = props;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const runtime = useRef<BackdropRuntime | null>(null);
  // Latest params for a (re)start without making them effect deps.
  const latest = useRef({ color, intensity });
  latest.current = { color, intensity };
  const [failed, setFailed] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (failed || !canvas) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const rt = startBackdrop(canvas, {
      variant,
      reduced,
      ...latest.current,
      onReady: () => setReady(true),
      onFail: () => setFailed(true),
    });
    runtime.current = rt;
    return () => {
      rt.dispose();
      runtime.current = null;
    };
  }, [variant, failed]);

  useEffect(() => {
    runtime.current?.update({ color, intensity });
  }, [color, intensity]);

  if (failed) return <BackdropCss {...props} />;
  const base: CSSProperties = { position: "absolute", inset: 0, pointerEvents: "none", overflow: "hidden", ...style };
  return (
    <div className={`fl-backdrop fl-backdrop-${variant} fl-backdrop-gpu ${className}`} style={base} aria-hidden>
      <canvas
        ref={canvasRef}
        style={{
          display: "block",
          width: "100%",
          height: "100%",
          opacity: ready ? 1 : 0,
          transition: "opacity 900ms cubic-bezier(0.23, 1, 0.32, 1)",
        }}
      />
    </div>
  );
}
