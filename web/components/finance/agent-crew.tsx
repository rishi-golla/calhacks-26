"use client";
/**
 * The crew: nine agent mascots in a lineup. Mood follows the agent's live
 * workload (`moodForStatus`), a recent failure makes it alert, and live events
 * pop a short speech bubble over whoever just did something.
 */
import Link from "next/link";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useLiveEvents, useOps } from "@/components/data-provider";
import { Mascot, moodForStatus } from "@/components/mascot";
import { AGENT_LIST, isAgentId, skillLabel, type AgentId } from "@/lib/agents";
import type { LiveEvent } from "@/lib/ops";
import { nick, usdShort } from "./format";

const STATUS_COLOR: Record<string, string> = {
  working: "var(--teal)",
  idle: "var(--text-mute)",
  stale: "var(--yellow)",
  offline: "var(--text-mute)",
  error: "var(--red)",
};
const BUBBLE_MS = 2600;
const FAILED_ALERT_MS = 120_000;

/** One short line per event, attributed to the agent who acted. */
function bubbleFor(e: LiveEvent): [string, string] | null {
  switch (e.type) {
    case "task_started":
      return [e.agent, `${skillLabel(e.skill)}…`];
    case "task_finished":
      return e.state === "failed" ? [e.agent, "Uh oh, that failed"] : e.state === "input-required" ? [e.agent, "Need a human!"] : [e.agent, "Done ✓"];
    case "delegation":
      return [e.from, `→ ${nick(e.to)}`];
    case "tool_call":
      return e.status === "completed" ? [e.agent, `${e.name}()`] : null;
    case "hold":
      return [e.agent || "controller", e.status === "placed" ? "Hold placed" : "Hold released"];
    case "approval":
      return e.decision === "pending" ? [e.agent || "controller", e.amount ? `Need OK on ${usdShort(e.amount)}` : "Need approval"] : null;
    case "call":
      return ["voice_desk", e.status === "in_progress" ? "Dialing…" : e.status.replaceAll("_", " ")];
    case "review":
      return ["validator", e.verdict === "pass" ? "Looks good" : "Found an issue"];
    case "doc_read":
      return [e.agent, "Reading…"];
    default:
      return null;
  }
}

export function AgentCrew() {
  const { workloads, attention, now, ops } = useOps();
  const [bubbles, setBubbles] = useState<Partial<Record<AgentId, { text: string; until: number }>>>({});
  const lastBubble = useRef<Partial<Record<AgentId, number>>>({});

  useLiveEvents((events) => {
    const t = Date.now();
    const next: Partial<Record<AgentId, { text: string; until: number }>> = {};
    for (const e of events) {
      if (e.at && t - e.at > 15_000) continue; // history, not news
      const b = bubbleFor(e);
      if (!b || !isAgentId(b[0])) continue;
      const id = b[0];
      // Give each bubble time to be read before replacing it.
      if (t - (lastBubble.current[id] ?? 0) < 1_800 && !next[id]) continue;
      next[id] = { text: b[1], until: t + BUBBLE_MS };
      lastBubble.current[id] = t;
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
  const busy = AGENT_LIST.filter((a) => workloads[a.id]?.status === "working").length;

  return (
    <section className="fl-panel fh-crew-panel" aria-label="Agent crew">
      <header className="fl-panel-head">
        <span className="fl-section" style={{ "--accent": "var(--lilac)" } as CSSProperties}>
          Agent crew
        </span>
        <span className="fh-head-meta">
          <span className="fl-micro">
            {busy}/{AGENT_LIST.length} busy · {ops.tasksActive} tasks
          </span>
          <Link href="/agents" className="fl-btn small quiet">
            Agent universe →
          </Link>
        </span>
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
                aria-label={`${a.name} (${a.nick}): ${status}, ${w?.active ?? 0} active, ${w?.queued ?? 0} queued`}
                title={`${a.name} · ${a.role}`}
              >
                <Mascot id={a.id} size={64} mood={moodForStatus(status, failed)} bubble={bubbles[a.id]?.text} />
                <span className="fh-crew-nick">{a.nick}</span>
                <span className="fh-crew-role">{a.short} · {a.name}</span>
                <span className="fh-crew-stat">
                  <i className="fl-dot" style={{ "--accent": STATUS_COLOR[status] } as CSSProperties} />
                  <span className="fl-num">{w?.active ?? 0}</span> active
                  {(w?.queued ?? 0) > 0 && <span className="fh-crew-q">+{w?.queued} queued</span>}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
