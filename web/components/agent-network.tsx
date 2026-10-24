"use client";
import { memo, useMemo } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  type NodeProps,
  type Node,
  type Edge,
} from "@xyflow/react";
import { Activity } from "lucide-react";
import { AGENTS, num, str, timestamp, type Row } from "@/lib/data";
import { useLive } from "./live-provider";
import { Badge, useClock } from "./ui";
import "@xyflow/react/dist/style.css";
type AgentData = {
  id: string;
  name: string;
  short: string;
  role: string;
  color: string;
  record?: Row;
};
const AgentNode = memo(function AgentNode({
  data,
}: NodeProps<Node<AgentData>>) {
  const record = data.record;
  return (
    <div className={`agent-node ${data.color}`}>
      <Handle type="target" position={Position.Left} />
      <div className="agent-top">
        <span className={`agent-icon ${data.color}`}>{data.short}</span>
        <Badge value={record?.status ?? "offline"} />
      </div>
      <strong>{data.name}</strong>
      <p>{data.role}</p>
      <div className="agent-counts">
        <span>
          <b>{record ? num(record.active) : "—"}</b> active
        </span>
        <span>
          <b>{record ? num(record.queued) : "—"}</b> queued
        </span>
        <span>
          <b>{record ? num(record.done) : "—"}</b> done
        </span>
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
});
const nodeTypes = { agent: AgentNode };
const positions: Record<string, { x: number; y: number }> = {
  carrier_ap: { x: 0, y: 20 },
  shipper_ar: { x: 0, y: 235 },
  risk: { x: 290, y: 20 },
  verification: { x: 290, y: 235 },
  controller: { x: 580, y: 20 },
  voice_desk: { x: 580, y: 235 },
  cfo: { x: 870, y: 20 },
  validator: { x: 870, y: 235 },
  documents: { x: 290, y: 450 },
};
export default function AgentNetwork({ caseId }: { caseId?: string }) {
  const { data, lastUpdated } = useLive();
  const now = useClock();
  const nodes = useMemo<Node<AgentData>[]>(
    () =>
      AGENTS.map((agent) => ({
        id: agent.id,
        type: "agent",
        position: positions[agent.id],
        data: {
          ...agent,
          record: (() => {
            const row = data.agent.find((row) => row.name === agent.id);
            return row && now - timestamp(row.last_seen) > 60000
              ? { ...row, status: "stale" }
              : row;
          })(),
        },
        draggable: false,
      })),
    [data.agent, now],
  );
  const edges = useMemo<Edge[]>(() => {
    const latest = new Map<string, Row>();
    for (const edge of data.delegation_edge)
      if (!caseId || edge.case_id === caseId) {
        const key = `${edge.from_agent}:${edge.to_agent}`;
        if (timestamp(edge.ts) >= timestamp(latest.get(key)?.ts))
          latest.set(key, edge);
      }
    return [...latest]
      .filter(
        ([, edge]) =>
          positions[str(edge.from_agent)] && positions[str(edge.to_agent)],
      )
      .map(([id, edge]) => ({
        id,
        source: str(edge.from_agent),
        target: str(edge.to_agent),
        type: "smoothstep",
        animated: lastUpdated - timestamp(edge.ts) < 30000,
        style: { stroke: "#38a88b", strokeWidth: 1.8 },
        label: str(edge.skill, "delegation"),
        labelStyle: { fontSize: 10, fill: "#677d77" },
        labelBgStyle: { fill: "#fafcfc" },
      }));
  }, [data.delegation_edge, caseId, lastUpdated]);
  return (
    <>
      <div className="network">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          minZoom={0.35}
          maxZoom={1.2}
          nodesConnectable={false}
          proOptions={{ hideAttribution: true }}
          aria-label="Live agent delegation network"
        >
          <Background color="#d7e2de" gap={22} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <div className="network-footer">
        <span>
          <i className="dot teal" /> Delegation recorded in SpacetimeDB
        </span>
        <span>
          <Activity size={13} />{" "}
          {
            data.delegation_edge.filter(
              (edge) => !caseId || edge.case_id === caseId,
            ).length
          }{" "}
          recorded handoffs
        </span>
      </div>
    </>
  );
}
