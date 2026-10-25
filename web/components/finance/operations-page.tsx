"use client";

import { AgentCrew } from "./agent-crew";
import { OpsStrip } from "./finance-home";
import "./finance.css";

export default function OperationsPage() {
  return (
    <div className="fl-page fh">
      <div className="fl-reveal">
        <OpsStrip />
      </div>
      <div className="fl-reveal" style={{ "--delay": "120ms" } as React.CSSProperties}>
        <AgentCrew />
      </div>
    </div>
  );
}
