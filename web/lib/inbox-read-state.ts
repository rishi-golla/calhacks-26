import { AGENTS } from "./data";
import { inboxActivityFingerprint, type AgentMessage } from "./agent-inbox";

export type ReadMarkers = Record<string, string[]>;

/** Keep read markers in a bounded, deterministic window of current records.
 * Visibility is the caller's responsibility; only the supplied visible subset
 * is marked, and full message bodies are never retained.
 */
export function markMessagesRead(
  previous: ReadMarkers,
  visibleMessages: AgentMessage[],
  allMessages: AgentMessage[],
  selected = "all",
  limit = 2000,
): ReadMarkers {
  const maximum = Number.isFinite(limit)
    ? Math.max(0, Math.floor(limit))
    : 2000;
  const agents = AGENTS.filter(
    (agent) => selected === "all" || agent.id === selected,
  );
  const chronological = [...allMessages].sort(
    (a, b) => a.at - b.at || a.id.localeCompare(b.id),
  );
  const fingerprints = new Map<AgentMessage, string>();
  const fingerprint = (message: AgentMessage) => {
    let value = fingerprints.get(message);
    if (value === undefined) {
      value = inboxActivityFingerprint(message);
      fingerprints.set(message, value);
    }
    return value;
  };
  let next = previous;
  for (const agent of agents) {
    const related = (message: AgentMessage) =>
      message.agentId === agent.id || message.recipientId === agent.id;
    const candidates = [
      ...new Set(chronological.filter(related).map(fingerprint)),
    ];
    const window = maximum ? candidates.slice(-maximum) : [];
    const seen = new Set(previous[agent.id] || []);
    const visible = new Set(visibleMessages.filter(related).map(fingerprint));
    const canonical = window.filter(
      (value) => seen.has(value) || visible.has(value),
    );
    const old = previous[agent.id] || [];
    if (
      old.length === canonical.length &&
      old.every((value, index) => value === canonical[index])
    )
      continue;
    if (next === previous) next = { ...previous };
    next[agent.id] = canonical;
  }
  return next;
}
