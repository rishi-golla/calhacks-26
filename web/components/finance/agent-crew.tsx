"use client";
/**
 * The crew: nine agent mascots in a lineup. Mood follows the agent's live
 * workload (`moodForStatus`), a recent failure makes it alert, and live events
 * pop a short speech bubble over whoever just did something (one at a time:
 * a bubble is never replaced before it expires).
 */
import Link from "next/link";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useLiveEvents, useOps } from "@/components/data-provider";
import { Mascot, moodForStatus } from "@/components/mascot";
import { AGENT_LIST, isAgentId, skillLabel, type AgentId } from "@/lib/agents";
import type { LiveEvent } from "@/lib/ops";
import { nick } from "./format";

const BUBBLE_MS = 2600;
const FAILED_ALERT_MS = 120_000;

/** A few words per event, attributed to the agent who acted. */
function bubbleFor(e: LiveEvent): [string, string] | null {
  switch (e.type) {
    case "task_started":
      return [e.agent, skillLabel(e.skill)];
    case "task_finished":
      return e.state === "failed" ? [e.agent, "Failed"] : e.state === "input-required" ? [e.agent, "Need you"] : [e.agent, "Done"];
    case "delegation":
      return [e.from, `→ ${nick(e.to)}`];
    case "hold":
      return [e.agent || "controller", e.status === "placed" ? "Hold" : "Released"];
    case "approval":
      return e.decision === "pending" ? [e.agent || "controller", "Need OK"] : null;
    case "call":
      return e.status === "in_progress" ? ["voice_desk", "Calling"] : null;
    case "review":
      return ["validator", e.verdict === "pass" ? "Looks good" : "Issue"];
    default:
      return null;
  }
}

export function AgentCrew() {
  const { workloads, attention, now } = useOps();
  const [bubbles, setBubbles] = useState<Partial<Record<AgentId, { text: string; until: number }>>>({});
  const shownUntil = useRef<Partial<Record<AgentId, number>>>({});

  useLiveEvents((events) => {
    const t = Date.now();
    const next: Partial<Record<AgentId, { text: string; until: number }>> = {};
    for (const e of events) {
      if (e.at && t - e.at > 15_000) continue; // history, not news
      const b = bubbleFor(e);
      if (!b || !isAgentId(b[0])) continue;
      const id = b[0];
      // One bubble at a time: let it be read before another replaces it.
      if ((shownUntil.current[id] ?? 0) > t || next[id]) continue;
      next[id] = { text: b[1], until: t + BUBBLE_MS };
      shownUntil.current[id] = t + BUBBLE_MS;
    }
    if (Object.keys(next).length) setBubbles((prev) => ({ ...prev, ...next }));
  });
  // Expire bubbles.
  useEffect(() => {
    const timer = setInterval(() => {
      const t = Date.now();
      setBubbles((prev) => {
        const live = Object.entries(prev).filter(([, b]) => b && b.until > t);
        return live.length === Object.keys(prev).length ? prev : Object.fromEntries(live);
      });
    }, 400);
    return () => clearInterval(timer);
  }, []);

  const recentlyFailed = new Set(
    attention.filter((a) => a.kind === "task_failed" && now - a.at < FAILED_ALERT_MS).map((a) => a.agent),
  );

  return (
    <section className="fl-panel fh-crew-panel" aria-label="Agent crew">
      <header className="fl-panel-head">
        <span className="fl-section" style={{ "--accent": "var(--lilac)" } as CSSProperties}>
          Agent crew
        </span>
        <Link href="/agents" className="fl-btn small quiet">
          Open map →
        </Link>
      </header>
      <ul className="fh-crew">
        {AGENT_LIST.map((a, i) => {
          const w = workloads[a.id];
          const status = w?.status ?? "offline";
          const failed = recentlyFailed.has(a.id);
          return (
            <li key={a.id} style={{ "--i": i, "--agent": a.color } as CSSProperties}>
              <Link
                href="/agents"
                className="fh-crew-item"
                aria-label={`${a.name} (${a.nick}): ${status}, ${w?.active ?? 0} active, ${w?.queued ?? 0} queued${w?.stalled ? `, ${w.stalled} stalled` : ""}`}
                title={`${a.name} · ${a.role}`}
              >
                <Mascot id={a.id} size={64} mood={moodForStatus(status, failed)} bubble={bubbles[a.id]?.text} />
                <span className="fh-crew-nick">{a.nick}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
