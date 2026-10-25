"use client";
/**
 * The open agent's mascot rebuilt from particles (Canvas UI Particle Object):
 * the cursor scatters them and they spring back home. three.js is fetched only
 * when a panel first opens. The panel keeps its flat <Mascot> instead when
 * WebGL2 is missing (`useWebGl2()` is false) or the image fails (`onError`).
 */
import dynamic from "next/dynamic";
import { useSyncExternalStore, type CSSProperties } from "react";
import { agentColor, mascotSrc, type NodeId } from "@/lib/agents";

const ParticleObject = dynamic(() => import("@/components/canvasui/ParticleObject"), { ssr: false });

let webgl2: boolean | undefined;
function probeWebGl2() {
  if (webgl2 === undefined) {
    try {
      const gl = document.createElement("canvas").getContext("webgl2");
      webgl2 = Boolean(gl);
      gl?.getExtension("WEBGL_lose_context")?.loseContext();
    } catch {
      webgl2 = false;
    }
  }
  return webgl2;
}
const noSubscribe = () => () => {};

export const useWebGl2 = () => useSyncExternalStore(noSubscribe, probeWebGl2, () => false);

export function MascotPortrait({ id, onError }: { id: NodeId; onError: () => void }) {
  return (
    <div className="au-portrait" style={{ "--c": agentColor(id) } as CSSProperties} aria-hidden>
      <ParticleObject
        src={mascotSrc(id, "idle", true)}
        count={11000}
        size={2.2}
        scale={4}
        radius={70}
        drift={0.5}
        floatIntensity={1}
        rotationIntensity={0.6}
        orbit={false}
        onError={onError}
        style={{ position: "absolute", inset: 0 }}
      />
    </div>
  );
}
