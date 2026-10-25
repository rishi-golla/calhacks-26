"use client";

import { AgentCrew } from "./agent-crew";
import { OpsStrip } from "./finance-home";
import { PageHead } from "./page-head";
import "./finance.css";

export default function OperationsPage() {
  return (
    <div className="fl-page fh">
      <PageHead title="Operations" sub="Throughput, automation rate and what each specialist is doing right now." />
      <div className="fl-reveal">
        <OpsStrip />
      </div>
      <div className="fl-reveal" style={{ "--delay": "120ms" } as React.CSSProperties}>
        <AgentCrew />
      </div>
    </div>
  );
}
