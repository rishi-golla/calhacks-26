/**
 * CSS-only backdrop: slow-drifting dot field plus a soft glow. Used when
 * WebGPU is missing or the GPU path fails. The drift stops under reduced motion.
 */
import type { CSSProperties } from "react";
import type { BackdropProps } from "./backdrop";

export default function BackdropCss({ variant = "field", intensity = 0.6, color = "#22c09b", className = "", style }: BackdropProps) {
  const base: CSSProperties = { position: "absolute", inset: 0, pointerEvents: "none", overflow: "hidden", ...style };
  return (
    <div className={`fl-backdrop fl-backdrop-${variant} ${className}`} style={base} aria-hidden>
      <div
        className="fl-backdrop-drift"
        style={{
          position: "absolute",
          inset: "-10%",
          backgroundImage: `radial-gradient(${color}22 1px, transparent 1.5px)`,
          backgroundSize: "26px 26px",
          opacity: 0.35 + intensity * 0.4,
          animation: "fl-backdrop-drift 60s linear infinite",
        }}
      />
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: `radial-gradient(60% 50% at 50% 40%, ${color}1f, transparent 70%)`,
          opacity: intensity,
        }}
      />
      <style>{`@keyframes fl-backdrop-drift{to{transform:translate3d(-26px,-26px,0)}}@media (prefers-reduced-motion: reduce){.fl-backdrop-drift{animation:none!important}}`}</style>
    </div>
  );
}
