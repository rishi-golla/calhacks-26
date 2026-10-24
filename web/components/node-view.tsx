"use client";
import { useMemo } from "react";
import { LiveProvider, useLive } from "./live-provider";
import { clock, feedOf, scope, stepById } from "./workflow-steps";
import "./workflow.css";

function View({ id, caseId }: { id: string; caseId: string }) {
  const { data, state } = useLive();
  const step = stepById(id);
  const lines = useMemo(
    () => (step ? feedOf(step, scope(data, caseId)) : []),
    [step, data, caseId],
  );
  if (!step) return <main className="nv">Unknown step: {id}</main>;
  return (
    <main className="nv">
      <header>
        <div>
          <span className="wf-agent">{step.operator}</span>
          <h1>{step.title}</h1>
          <p>
            {step.subtitle}
            {caseId ? ` · ${caseId}` : ""}
          </p>
        </div>
        <span className={`wf-conn ${state}`}>{lines.length} events</span>
      </header>
      {lines.length === 0 && <p className="wf-empty">Waiting for events…</p>}
      <ol>
        {lines.map((l) => (
          <li key={l.key} className={Date.now() - l.ts < 60_000 ? "fresh" : ""}>
            <time>{clock(l.ts)}</time>
            <span>{l.text}</span>
            {l.meta && <em>{l.meta}</em>}
          </li>
        ))}
      </ol>
    </main>
  );
}

export default function NodeView(props: { id: string; caseId: string }) {
  return (
    <LiveProvider>
      <View {...props} />
    </LiveProvider>
  );
}
