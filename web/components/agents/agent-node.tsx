"use client";
/**
 * One node on the agent map: mascot, status ring, one line of counts, an
 * activity ticker (owned by the packet engine) and a short queue of case chips. Memoized; props are primitives so a 250 ms data
 * tick only re-renders the nodes whose numbers actually moved.
 */
import { memo, useEffect, useRef, type CSSProperties } from "react";
import { Mascot, type Mood } from "@/components/mascot";
import { AGENT_META, NODE_META, isAgentId, type NodeId } from "@/lib/agents";
import { MASCOT_SIZE, NODE_WIDTH, nodePos } from "./layout";
import type { PacketEngine } from "./packet-engine";

export type NodeTone = "working" | "idle" | "sleeping" | "alert";

export type NodeView = {
  id: NodeId;
  mood: Mood;
  bubble?: string;
  tone: NodeTone;
  /** Counters as [value, label] pairs flattened, e.g. "3|active|128|done". */
  counts: string;
  /** Case chips: "CASE-4831~P1|CASE-4829~P3". */
  chips: string;
  more: number;
};

type Props = NodeView & {
  engine: PacketEngine;
  /** Layout stretch; only here so nodes re-render when the rows spread. */
  stretch: number;
  state: "" | "on" | "dim" | "hidden";
  focus: boolean;
  order: number;
  selectedCase: string;
  onHover: (id: NodeId | null) => void;
  onSelect: (id: NodeId) => void;
  onCase: (caseId: string) => void;
};

export const PRIORITY_COLOR: Record<string, string> = { P0: "#e40014", P1: "#ff631d", P2: "#d1d11a", P3: "#828b98" };

export const shortCase = (caseId: string) => caseId.replace(/^CASE-/, "");

/** Stable per-agent phase into the resting breath so the map doesn't breathe in unison. */
const breathOffset = (id: string) => {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return h % 5200;
};

function AgentNodeImpl({ id, mood, bubble, tone, counts, chips, more, engine, stretch, state, focus, order, selectedCase, onHover, onSelect, onCase }: Props) {
  const meta = isAgentId(id) ? AGENT_META[id] : NODE_META[id as "intake" | "human"];
  const pos = nodePos(id);
  const size = MASCOT_SIZE[id];
  const flash = useRef<HTMLSpanElement>(null);
  const bump = useRef<HTMLSpanElement>(null);
  const ticker = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    engine.registerNode(id, { flash: flash.current, bump: bump.current, ticker: ticker.current });
    return () => engine.unregisterNode(id);
  }, [engine, id]);

  const parts = counts.split("|");
  const cells: [string, string][] = [];
  for (let i = 0; i < parts.length; i += 2) cells.push([parts[i], parts[i + 1]]);
  const chipList = chips ? chips.split("|").map((c) => c.split("~") as [string, string]) : [];
  const style = {
    left: pos.x,
    top: pos.y - size / 2,
    "--c": meta.color,
    "--m": `${size}px`,
    "--w": `${NODE_WIDTH[id] ?? 200}px`,
    "--breath-delay": `${-breathOffset(id)}ms`,
  } as CSSProperties;

  return (
    <div className={`au-node ${tone} ${state} ${focus ? "focus" : ""} ${id === "human" ? "appear" : ""}`} style={style} data-node={id} data-stretch={stretch}>
      <button
        type="button"
        className="au-hit"
        aria-label={`${meta.name}: ${cells.map(([v, k]) => `${v} ${k}`).join(", ")}`}
        onPointerEnter={() => onHover(id)}
        onPointerLeave={() => onHover(null)}
        onFocus={() => onHover(id)}
        onBlur={() => onHover(null)}
        onClick={() => onSelect(id)}
      >
        <span className="au-fig" ref={bump}>
          <span className="au-glow" aria-hidden />
          <span className="au-spin" aria-hidden />
          <span className="au-ring" aria-hidden />
          <span className="au-flash" ref={flash} aria-hidden />
          <span className="au-ticker" ref={ticker} aria-hidden />
          <Mascot id={id} mood={mood} size={size} bubble={bubble} />
          <span className="au-nick">{meta.nick}</span>
          {order > 0 && <span className="au-order">{order}</span>}
        </span>
        <span className="au-name">{meta.name}</span>
        <span className="au-meta">
          {cells.map(([v, k]) => (
            <span key={k}>
              <b>{v}</b> {k}
            </span>
          ))}
        </span>
      </button>
      <div className="au-chips">
        {chipList.map(([caseId, priority]) => (
          <button
            key={caseId}
            type="button"
            className={`au-chip ${caseId === selectedCase ? "sel" : ""}`}
            style={{ "--p": PRIORITY_COLOR[priority] ?? PRIORITY_COLOR.P3 } as CSSProperties}
            onClick={() => onCase(caseId)}
            title={`${caseId} · ${priority}`}
          >
            {shortCase(caseId)}
          </button>
        ))}
        {more > 0 && <span className="au-chip more">+{more}</span>}
      </div>
    </div>
  );
}

export const AgentNode = memo(AgentNodeImpl);
