"use client";
/**
 * Handoff edges: faint dashed hairlines that appear the first time an edge is
 * used and thicken with the log of traffic over the last five minutes. The
 * focused agent's edges brighten and show their non-zero counts; a selected case draws
 * its own path solid and dims the rest.
 *
 * Lesson lines are separate: a gold line from Settlement Quality Review to
 * every agent it has taught, with a slow flow toward the learner and the
 * agent's lesson count at the midpoint.
 */
import { memo, type CSSProperties } from "react";
import { agentColor, type AgentId, type NodeId } from "@/lib/agents";
import { STAGE_W, edgeCurve, stageHeight } from "./layout";
import { edgeWidth, type EdgeStat } from "./model";

type Props = {
  stats: EdgeStat[];
  focus: NodeId | null;
  path: Set<string> | null;
  hideHuman: boolean;
  /** Layout stretch; curves are recomputed when it changes. */
  stretch: number;
  /** Agents the Validator has taught, with their active lesson counts. */
  lessons: LessonEdge[];
};

export type LessonEdge = { agent: AgentId; count: number };

function EdgesImpl({ stats, focus, path, hideHuman, stretch, lessons }: Props) {
  const visible = stats.filter((s) => !(hideHuman && (s.from === "human" || s.to === "human")));
  return (
    <svg className={`au-edges ${path ? "dimmed" : ""}`} viewBox={`0 0 ${STAGE_W} ${stageHeight()}`} width={STAGE_W} height={stageHeight()} data-stretch={stretch} aria-hidden>
      {visible.map((s) => {
        const c = edgeCurve(s.from, s.to);
        const onPath = path?.has(s.key) ?? false;
        const focused = !path && focus != null && (s.from === focus || s.to === focus);
        const cls = onPath ? "path" : focused ? "focus" : s.recent === 0 ? "cold" : "";
        return (
          <path
            key={s.key}
            d={c.d}
            className={`au-edge ${cls}`}
            style={{ "--c": agentColor(s.from === "intake" ? s.to : s.from), strokeWidth: onPath ? 2 : edgeWidth(s.recent) } as CSSProperties}
          />
        );
      })}
      {lessons.map(({ agent, count }) => {
        const c = edgeCurve("validator", agent);
        const lit = !path && (focus === "validator" || focus === agent);
        return (
          <g key={`lesson:${agent}`} className={`au-lesson ${lit ? "focus" : ""}`}>
            <path d={c.d} className="au-lesson-edge" />
            <path d={c.d} className="au-lesson-flow" />
            <text className="au-lesson-count" x={c.mid.x} y={c.mid.y}>
              {count} {count === 1 ? "lesson" : "lessons"}
            </text>
          </g>
        );
      })}
      {!path &&
        focus &&
        visible
          .filter((s) => (s.from === focus || s.to === focus) && s.recent > 0)
          .map((s) => {
            const c = edgeCurve(s.from, s.to);
            return (
              <text key={`n:${s.key}`} className="au-edge-count" x={c.mid.x} y={c.mid.y}>
                {s.recent}/5m
              </text>
            );
          })}
    </svg>
  );
}

export const Edges = memo(EdgesImpl);
