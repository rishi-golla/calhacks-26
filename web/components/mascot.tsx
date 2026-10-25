"use client";
/**
 * Agent mascot: the generated 3D blob character for a node, with CSS-driven
 * moods. Falls back to a flat blob in the agent's color if an image is missing
 * (e.g. the `working` / `alert` variant has not been generated yet).
 *
 * Moods map to behavior, not to image files alone:
 *   idle     — gentle breathing bob
 *   working  — busier bob + squash, optional sparkles, "working" image if present
 *   talking  — wobble with a speech bubble
 *   alert    — shake once, "alert" image if present, sweat drop
 *   success  — hop + ring
 *   sleeping — slow bob, dimmed, "zzz"
 */
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { agentColor, mascotSrc, type NodeId } from "@/lib/agents";
import "./mascot.css";

export type Mood = "idle" | "working" | "talking" | "alert" | "success" | "sleeping";

type Manifest = { characters: Record<string, Partial<Record<"idle" | "working" | "alert", string>>> };
let manifestPromise: Promise<Manifest | null> | null = null;
const loadManifest = () => {
  if (!manifestPromise)
    manifestPromise = fetch("/mascots/manifest.json", { cache: "no-store" })
      .then((r) => (r.ok ? (r.json() as Promise<Manifest>) : null))
      .catch(() => null);
  return manifestPromise;
};

/** Which image variants exist for each character (from the manifest). */
export function useMascotManifest() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  useEffect(() => {
    let alive = true;
    loadManifest().then((m) => alive && setManifest(m));
    return () => {
      alive = false;
    };
  }, []);
  return manifest;
}

export function Mascot({
  id,
  mood = "idle",
  size = 72,
  bubble,
  className = "",
  style,
  flip = false,
  title,
}: {
  id: NodeId | string;
  mood?: Mood;
  size?: number;
  /** Speech bubble text (shown for any mood). */
  bubble?: ReactNode;
  className?: string;
  style?: CSSProperties;
  flip?: boolean;
  title?: string;
}) {
  const manifest = useMascotManifest();
  const variants = manifest?.characters?.[id] ?? null;
  const variant: "idle" | "working" | "alert" =
    mood === "working" && variants?.working ? "working" : mood === "alert" && variants?.alert ? "alert" : "idle";
  // The human controller has no generated character; everything else is
  // assumed to exist until the manifest says otherwise.
  const hasImage = id !== "human" && (!manifest || Boolean(variants?.idle));
  const color = agentColor(id);
  const [broken, setBroken] = useState(false);
  useEffect(() => setBroken(false), [id, variant]);
  return (
    <span
      className={`mascot mood-${mood} ${className}`}
      style={{ width: size, height: size, "--mascot-color": color, ...style } as CSSProperties}
      title={title}
      data-agent={id}
    >
      <span className="mascot-body" style={{ transform: flip ? "scaleX(-1)" : undefined }}>
        {hasImage && !broken ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={mascotSrc(id, variant, size <= 160)} alt="" draggable={false} onError={() => setBroken(true)} />
        ) : (
          <svg viewBox="0 0 100 100" aria-hidden>
            <path d="M50 8 C72 8 86 40 86 62 C86 84 70 94 50 94 C30 94 14 84 14 62 C14 40 28 8 50 8Z" fill={color} />
            <ellipse cx="41" cy="48" rx="3.6" ry="6.5" fill="#15161a" />
            <ellipse cx="59" cy="48" rx="3.6" ry="6.5" fill="#15161a" />
            <ellipse cx="50" cy="94" rx="30" ry="4" fill="rgba(0,0,0,.25)" />
          </svg>
        )}
      </span>
      {mood === "working" && (
        <span className="mascot-sparkles" aria-hidden>
          <i />
          <i />
          <i />
        </span>
      )}
      {mood === "alert" && <span className="mascot-drop" aria-hidden />}
      {mood === "success" && <span className="mascot-ring" aria-hidden />}
      {mood === "sleeping" && (
        <span className="mascot-zzz" aria-hidden>
          z<i>z</i>
          <i>z</i>
        </span>
      )}
      {bubble != null && bubble !== "" && <span className="mascot-bubble">{bubble}</span>}
    </span>
  );
}

/** Mood for an agent from its workload status. */
export const moodForStatus = (status: "working" | "idle" | "offline" | "stale" | "error", hasError = false): Mood =>
  hasError || status === "error" ? "alert" : status === "working" ? "working" : status === "idle" ? "idle" : "sleeping";
