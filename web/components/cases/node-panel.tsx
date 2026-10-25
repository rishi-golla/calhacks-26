"use client";
/**
 * Right-hand drawer for the trace: full details of one node (trigger,
 * documents, tool calls, outputs, communication, audit trail) or the case log.
 * Filtered by the replay cursor like the graph.
 */
import type { CSSProperties, ReactNode } from "react";
import { ArrowDownRight, ArrowUpLeft, Phone, X } from "lucide-react";
import { Mascot } from "@/components/mascot";
import { agentColor, agentLabel, skillLabel } from "@/lib/agents";
import { json, num, str, timestamp, type Row } from "@/lib/data";
import { fmtMoney, type TimelineEvent } from "@/lib/ops";
import { approvalRequestedAt, clock, maskPhone, nodeStatus, shortDur, toolsAt, type CaseTrace, type NodeStatus, type TraceNode } from "./model";

const TONE: Record<NodeStatus, string> = {
  completed: "ok",
  approved: "ok",
  failed: "bad",
  rejected: "bad",
  waiting: "hot",
  pending: "hot",
  working: "warn",
  queued: "",
  canceled: "",
};

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="ct-sec">
      <h3 className="ct-sec-title">
        {title}
        {count !== undefined && <span className="fl-mono fl-mute">{count}</span>}
      </h3>
      {children}
    </section>
  );
}

export function Json({ value, label }: { value: unknown; label?: string }) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "string" ? json<unknown>(value, value) : value;
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && Object.keys(parsed).length === 0) return null;
  const pretty = typeof parsed === "string" ? parsed : JSON.stringify(parsed, null, 2);
  const preview = typeof parsed === "string" ? parsed : JSON.stringify(parsed);
  return (
    <details className="ct-json">
      <summary>
        {label && <span className="fl-micro">{label}</span>}
        <code>{preview.length > 72 ? `${preview.slice(0, 72)}…` : preview}</code>
      </summary>
      <pre>{pretty}</pre>
    </details>
  );
}

function NodeLink({ trace, id, onSelect, children }: { trace: CaseTrace; id: string; onSelect: (id: string) => void; children: ReactNode }) {
  const node = trace.nodeById.get(id);
  if (!node) return <>{children}</>;
  return (
    <button className="ct-link" onClick={() => onSelect(id)} style={{ "--agent": agentColor(node.lane) } as CSSProperties}>
      {children}
    </button>
  );
}

const nodeTitle = (n: TraceNode) =>
  n.kind === "task" ? `${agentLabel(n.lane)} · ${skillLabel(n.task?.skill)}` : n.kind === "intake" ? "Intake & Routing" : "Human approval";

type PanelProps = { trace: CaseTrace; node: TraceNode; cursor: number; onSelect: (id: string) => void; onClose: () => void };

export function NodeDetails({ trace, node, cursor, onSelect, onClose }: PanelProps) {
  const status = nodeStatus(node, cursor);
  return (
    <div className="ct-details" style={{ "--agent": agentColor(node.lane) } as CSSProperties}>
      <div className="ct-panel-head">
        <span className="fl-section" style={{ "--accent": agentColor(node.lane) } as CSSProperties}>
          Node · {agentLabel(node.lane)}
        </span>
        <button className="fl-btn quiet small" onClick={onClose} aria-label="Close details">
          <X size={14} />
        </button>
      </div>
      <div className="ct-details-hero">
        <Mascot id={node.lane} size={52} mood={status === "working" ? (node.lane === "voice_desk" ? "talking" : "working") : status === "waiting" || status === "pending" || status === "failed" ? "alert" : "idle"} />
        <div style={{ minWidth: 0 }}>
          <div className="ct-details-title">{node.kind === "task" ? skillLabel(node.task?.skill) : nodeTitle(node)}</div>
          <div className="fl-micro ct-details-sub">
            {node.kind === "task" ? `${node.task?.displayId} · ${node.id}` : node.id}
          </div>
          {status && (
            <span className={`fl-chip outline ${TONE[status]}`} style={{ marginTop: 6 }}>
              {status === "waiting" ? "waiting for human" : status}
            </span>
          )}
        </div>
      </div>
      {!status ? (
        <p className="ct-empty-note">This step had not happened yet at the replay cursor.</p>
      ) : node.kind === "task" ? (
        <TaskDetails {...{ trace, node, cursor, onSelect, onClose }} />
      ) : node.kind === "intake" ? (
        <IntakeDetails {...{ trace, node, cursor, onSelect, onClose }} />
      ) : (
        <HumanDetails {...{ trace, node, cursor, onSelect, onClose }} />
      )}
    </div>
  );
}

function KV({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="ct-kv">
      {rows
        .filter(([, v]) => v !== null && v !== undefined && v !== "" && v !== false)
        .map(([k, v]) => (
          <div key={k}>
            <dt className="fl-micro">{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
    </dl>
  );
}

function TaskDetails({ trace, node, cursor, onSelect }: PanelProps) {
  const t = node.task!;
  const status = nodeStatus(node, cursor)!;
  const done = node.end > 0 && node.end <= cursor;
  const incoming = trace.edges.find((e) => e.to === node.id && e.kind !== "decision");
  const outgoing = trace.edges.filter((e) => e.from === node.id && e.at <= cursor && e.kind !== "approval");
  const audit = (trace.auditByTask.get(node.id) ?? []).filter((a) => timestamp(a.ts) <= cursor);
  const outboxAudit = audit.find((a) => a.reason_code === "REVIEW_OUTBOX" || a.reason_code === "TASK_RETRY");
  const tools = toolsAt(node, cursor);
  const docs = node.docs.filter((d) => d.at <= cursor);
  const calls = trace.slice.call.filter((c) => c.task_id === node.id && timestamp(c.started_at) <= cursor);
  const approvals = trace.slice.approval.filter((a) => a.task_id === node.id && approvalRequestedAt(a, trace.slice.audit_event) <= cursor);
  const reviewsOf = trace.slice.review.filter((r) => r.target_task_id === node.id && timestamp(r.ts) <= cursor);
  const ownReview = t.agent === "validator" ? trace.slice.review.find((r) => str(r.id) === `REV-${node.id}` && timestamp(r.ts) <= cursor) : undefined;
  const artifact = done ? t.artifact : null;

  return (
    <>
      <Section title="Triggered by">
        {incoming ? (
          <p className="ct-line">
            <ArrowUpLeft size={13} />
            <NodeLink trace={trace} id={incoming.from} onSelect={onSelect}>
              {agentLabel(trace.nodeById.get(incoming.from)?.lane)}
            </NodeLink>
            {incoming.kind === "dispatch"
              ? ` routed ${skillLabel(incoming.skill)}`
              : incoming.kind === "review"
                ? " output went to quality review"
                : incoming.kind === "retry"
                  ? " failed; retried"
                  : ` delegated ${skillLabel(incoming.skill)}`}{" "}
            <span className="fl-mono fl-mute">{clock(incoming.at)}</span>
          </p>
        ) : outboxAudit ? (
          <p className="ct-line">
            <ArrowUpLeft size={13} /> {str(outboxAudit.summary)} <span className="fl-mono fl-mute">{clock(timestamp(outboxAudit.ts))}</span>
          </p>
        ) : (
          <p className="ct-line fl-mute">No recorded trigger (task row without dispatch or delegation).</p>
        )}
        {incoming?.kind === "dispatch" && (
          <p className="ct-quote">{str(trace.nodeById.get(incoming.from)?.dispatch?.reason, "")}</p>
        )}
      </Section>

      <Section title="Timings">
        <KV
          rows={[
            ["Started", <span key="s" className="fl-mono">{clock(t.startedAt)}</span>],
            ["Ended", done ? <span className="fl-mono">{clock(node.end)}</span> : "—"],
            ["Took", <span key="d" className="fl-mono">{shortDur((done ? node.end : Math.min(cursor, Date.now())) - t.startedAt)}{done ? "" : " so far"}</span>],
            ["Model", t.model ? <span className="fl-mono">{t.model}</span> : "—"],
            ["Confidence", done && t.confidence ? <span className="fl-mono">{t.confidence.toFixed(2)}</span> : "—"],
            ["Depth", <span key="dp" className="fl-mono">{t.depth}</span>],
          ]}
        />
      </Section>

      <Section title="Documents used" count={docs.length}>
        {docs.length ? (
          <ul className="ct-list">
            {docs.map((d) => (
              <li key={d.id}>
                <div className="ct-line">
                  <span className="fl-mono ct-doc-ref">{d.docRef}</span>
                  <span className="fl-micro">
                    p{d.page || 1}
                    {d.span ? ` · ${d.span}` : ""}
                  </span>
                </div>
                <Json value={d.extracted} label="extracted" />
              </li>
            ))}
          </ul>
        ) : (
          <p className="ct-line fl-mute">No documents read{done ? "" : " yet"}.</p>
        )}
      </Section>

      <Section title="Actions" count={tools.length}>
        {tools.length ? (
          <ul className="ct-list">
            {tools.map(({ call, status: s }) => (
              <li key={call.id}>
                <div className="ct-line">
                  <span className={`ct-tool-glyph ${s}`}>{s === "running" ? "…" : s === "completed" ? "✓" : "✗"}</span>
                  <span className="fl-mono">{call.name}</span>
                  <span className="fl-micro" style={{ marginLeft: "auto" }}>
                    {s === "running" ? "running" : call.durationMs ? shortDur(call.durationMs) : s}
                  </span>
                </div>
                <Json value={call.arguments} label="args" />
                {s !== "running" && <Json value={call.result} label="result" />}
              </li>
            ))}
          </ul>
        ) : (
          <p className="ct-line fl-mute">No tool calls{done ? "" : " yet"}.</p>
        )}
      </Section>

      <Section title="Output">
        {done || status === "waiting" ? (
          <>
            <KV
              rows={[
                ["Outcome", t.outcome ? <span className={`fl-chip outline ${TONE[status]}`}>{t.outcome.replaceAll("_", " ")}</span> : status],
                ["Status", t.businessStatus.replaceAll("_", " ")],
              ]}
            />
            {str(t.artifact?.summary, "") && <p className="ct-quote">{str(t.artifact?.summary)}</p>}
            {artifact && <Json value={artifact.findings ?? artifact.checks} label="findings" />}
            {t.policyIds.length > 0 && (
              <div className="ct-chips">
                {t.policyIds.map((p) => (
                  <span key={p} className="fl-chip outline info">
                    {p}
                  </span>
                ))}
              </div>
            )}
            {t.evidenceRefs.length > 0 && (
              <div className="ct-evidence">
                {t.evidenceRefs.map((r) => (
                  <span key={r} className="fl-mono">
                    {r}
                  </span>
                ))}
              </div>
            )}
            {artifact && <Json value={artifact} label="artifact" />}
          </>
        ) : (
          <p className="ct-line fl-mute">Still working; the artifact lands when the task completes.</p>
        )}
      </Section>

      <Section title="Communication">
        <ul className="ct-list compact">
          {incoming && (
            <li className="ct-line">
              <ArrowUpLeft size={13} /> from{" "}
              <NodeLink trace={trace} id={incoming.from} onSelect={onSelect}>
                {agentLabel(trace.nodeById.get(incoming.from)?.lane)}
              </NodeLink>
            </li>
          )}
          {outgoing.map((e) => {
            const child = trace.nodeById.get(e.to)!;
            const s = nodeStatus(child, cursor);
            return (
              <li key={e.id} className="ct-line">
                <ArrowDownRight size={13} /> to{" "}
                <NodeLink trace={trace} id={e.to} onSelect={onSelect}>
                  {agentLabel(child.lane)}
                </NodeLink>
                <span className="fl-mute">{e.kind === "review" ? "review" : e.kind === "retry" ? "retry" : skillLabel(e.skill)}</span>
                {s && <span className={`fl-chip outline ${TONE[s]} ct-mini-chip`}>{s}</span>}
              </li>
            );
          })}
          {approvals.map((a) => (
            <li key={str(a.id)} className="ct-line">
              <ArrowDownRight size={13} /> asked{" "}
              <NodeLink trace={trace} id={`human:${str(a.id)}`} onSelect={onSelect}>
                a human
              </NodeLink>
              to approve {str(a.action).replaceAll("_", " ")} <span className="fl-num">{fmtMoney(num(a.amount))}</span>
            </li>
          ))}
          {!incoming && !outgoing.length && !approvals.length && !calls.length && !reviewsOf.length && !ownReview && (
            <li className="ct-line fl-mute">No messages exchanged.</li>
          )}
        </ul>
        {calls.map((c) => (
          <CallRecord key={str(c.id)} call={c} cursor={cursor} />
        ))}
        {[...reviewsOf, ...(ownReview ? [ownReview] : [])].map((r) => (
          <ReviewRecord key={str(r.id)} review={r} trace={trace} onSelect={onSelect} own={r === ownReview} />
        ))}
      </Section>

      <Section title="Audit trail" count={audit.length}>
        <ol className="ct-audit">
          {audit.map((a) => (
            <li key={str(a.id)}>
              <span className="fl-mono fl-mute">{clock(timestamp(a.ts))}</span>
              <span className="fl-mono ct-audit-code">{str(a.reason_code, str(a.kind))}</span>
              <span className="ct-audit-text">{auditText(a)}</span>
              {str(a.hash, "") && <span className="fl-mono fl-mute ct-hash">#{str(a.hash).replace(/^sim/, "").slice(0, 8)}</span>}
            </li>
          ))}
        </ol>
      </Section>
    </>
  );
}

function auditText(a: Row) {
  if (a.kind !== "tool_call") return str(a.summary, str(a.kind));
  const body = json<Row>(a.summary, {});
  return `${str(body.tool_name, "tool")} ${str(body.status, "")}${body.duration_ms ? ` · ${shortDur(num(body.duration_ms))}` : ""}`;
}

function CallRecord({ call, cursor }: { call: Row; cursor: number }) {
  const ended = timestamp(call.ended_at);
  const finished = ended && ended <= cursor;
  const status = finished ? str(call.status) : cursor === Infinity ? str(call.status) : "in_progress";
  return (
    <div className="ct-record">
      <div className="ct-line">
        <Phone size={13} />
        <strong>{str(call.direction, "outbound")} call</strong>
        <span className={`fl-chip outline ${status === "completed" ? "ok" : status === "no_answer" ? "warn" : "info"} ct-mini-chip`}>{status.replaceAll("_", " ")}</span>
      </div>
      <KV
        rows={[
          ["To", <span key="to" className="fl-mono">{str(call.to_masked, "") || maskPhone(str(call.intended_to, ""))}</span>],
          ["Intended", str(call.intended_to, "") ? <span className="fl-mono">{maskPhone(str(call.intended_to))}</span> : ""],
          ["Routed to", str(call.routed_to, "")],
          ["Persona", str(call.persona, "")],
          ["Disclosure", call.disclosure_given ? "AI disclosure given" : ""],
          ["Started", <span key="st" className="fl-mono">{clock(timestamp(call.started_at))}</span>],
          ["Ended", finished ? <span className="fl-mono">{clock(ended)}</span> : ""],
        ]}
      />
      {finished && <Json value={call.findings_json} label="findings" />}
      {finished && str(call.transcript_redacted, "") && <p className="ct-transcript">{str(call.transcript_redacted)}</p>}
    </div>
  );
}

function ReviewRecord({ review, trace, onSelect, own }: { review: Row; trace: CaseTrace; onSelect: (id: string) => void; own: boolean }) {
  const checks = json<Row[]>(review.checks_json, []);
  const friction = json<Row[]>(review.friction_json, []);
  const verdict = str(review.verdict);
  return (
    <div className="ct-record">
      <div className="ct-line">
        <strong>Review verdict</strong>
        <span className={`fl-chip outline ${verdict === "pass" ? "ok" : "warn"} ct-mini-chip`}>{verdict}</span>
        {own ? (
          trace.nodeById.has(str(review.target_task_id)) && (
            <span className="fl-mute">
              on{" "}
              <NodeLink trace={trace} id={str(review.target_task_id)} onSelect={onSelect}>
                {agentLabel(trace.nodeById.get(str(review.target_task_id))?.lane)}
              </NodeLink>
            </span>
          )
        ) : (
          <span className="fl-mute">by Settlement Quality Review</span>
        )}
      </div>
      {Array.isArray(checks) && checks.length > 0 && (
        <ul className="ct-checks">
          {checks.map((c, i) => (
            <li key={i} className={c.ok ? "ok" : "bad"}>
              {c.ok ? "✓" : "✗"} <span className="fl-mono">{str(c.name)}</span>
            </li>
          ))}
        </ul>
      )}
      {Array.isArray(friction) && friction.length > 0 && <Json value={friction} label="friction" />}
    </div>
  );
}

function IntakeDetails({ trace, node, onSelect }: PanelProps) {
  const d = node.dispatch!;
  const target = str(d.task_id, "");
  const caseRow = trace.slice.case[0];
  return (
    <>
      <Section title="Triggered by">
        <p className="ct-line">
          <ArrowUpLeft size={13} /> New event for {trace.caseId}
          {caseRow && <span className="fl-mono fl-mute"> opened {clock(timestamp(caseRow.opened_at))}</span>}
        </p>
      </Section>
      <Section title="Routing decision">
        <KV
          rows={[
            ["Skill", skillLabel(str(d.skill))],
            ["Agent", agentLabel(str(d.agent))],
            ["Status", str(d.status, "")],
            ["Queue", d.queue_ms !== undefined ? <span className="fl-mono">{num(d.queue_ms)} ms</span> : ""],
            ["Policy", str(d.policy_version, "")],
            ["Event", <span key="ev" className="fl-mono">{str(d.event_id)}</span>],
            ["At", <span key="at" className="fl-mono">{clock(node.start)}</span>],
          ]}
        />
        {str(d.reason, "") && <p className="ct-quote">{str(d.reason)}</p>}
      </Section>
      <Section title="Communication">
        <p className="ct-line">
          <ArrowDownRight size={13} /> to{" "}
          <NodeLink trace={trace} id={target} onSelect={onSelect}>
            {agentLabel(str(d.agent))}
          </NodeLink>
          <span className="fl-mute">{skillLabel(str(d.skill))}</span>
        </p>
      </Section>
    </>
  );
}

function HumanDetails({ trace, node, cursor, onSelect }: PanelProps) {
  const a = node.approval!;
  const decided = node.end > 0 && node.end <= cursor;
  return (
    <>
      <Section title="Triggered by">
        <p className="ct-line">
          <ArrowUpLeft size={13} />
          <NodeLink trace={trace} id={str(a.task_id)} onSelect={onSelect}>
            {agentLabel(str(a.requested_by, "controller"))}
          </NodeLink>
          requested approval <span className="fl-mono fl-mute">{clock(node.start)}</span>
        </p>
      </Section>
      <Section title="Approval">
        <KV
          rows={[
            ["Action", str(a.action).replaceAll("_", " ")],
            ["Amount", <span key="amt" className="fl-num">{fmtMoney(num(a.amount))}</span>],
            ["Target", <span key="tg" className="fl-mono">{str(a.target_id || a.invoice_id, "—")}</span>],
            ["Expires", <span key="ex" className="fl-mono">{clock(timestamp(a.expires_at))}</span>],
            ["Decision", decided ? str(a.decision) : "pending"],
            ["Decided", decided ? <span className="fl-mono">{clock(node.end)}</span> : ""],
            ["Approver", decided ? str(a.approver_name || a.approver, "") : ""],
            ["Code check", decided ? (a.code_ok ? "verified" : "not verified") : ""],
          ]}
        />
      </Section>
    </>
  );
}

/* ------------------------------ case log ------------------------------ */

const KIND_GLYPH: Partial<Record<TimelineEvent["kind"], string>> = {
  dispatch: "→",
  task_started: "●",
  task_completed: "✓",
  task_failed: "✗",
  task_waiting: "◆",
  delegation: "↳",
  tool_call: "▪",
  doc_read: "▫",
  hold: "⏸",
  payment: "$",
  approval: "◆",
  journal: "≡",
  call: "☎",
  review: "✓",
  policy: "⛔",
};

export function CaseLog({ trace, cursor, onSelect, onClose }: { trace: CaseTrace; cursor: number; onSelect: (id: string) => void; onClose: () => void }) {
  const edgeTasks = new Set(trace.edges.filter((e) => e.kind === "delegation").map((e) => e.to));
  const events = trace.timeline.filter((e) => {
    if (e.at > cursor) return false;
    if (e.kind === "delegation" && e.id.startsWith("audit:") && edgeTasks.has(e.taskId)) return false;
    if (e.kind === "tool_call" && / · started$/.test(e.title)) return false;
    return true;
  });
  const target = (e: TimelineEvent) => {
    if (e.kind === "dispatch") return trace.nodes.find((n) => n.kind === "intake" && n.dispatch && str(n.dispatch.event_id) === str(e.row.event_id))?.id;
    if (e.kind === "approval" && e.row.kind === "decide_approval") {
      const a = trace.slice.approval.find((x) => x.task_id === e.taskId);
      if (a) return `human:${str(a.id)}`;
    }
    return trace.nodeById.has(e.taskId) ? e.taskId : undefined;
  };
  return (
    <div className="ct-details">
      <div className="ct-panel-head">
        <span className="fl-section">Case log · {events.length} events</span>
        <button className="fl-btn quiet small" onClick={onClose} aria-label="Close log">
          <X size={14} />
        </button>
      </div>
      <ol className="ct-log">
        {[...events].reverse().map((e) => {
          const id = target(e);
          const body = (
            <>
              <span className="fl-mono fl-mute ct-log-time">{clock(e.at)}</span>
              <span className="ct-log-glyph" style={{ color: agentColor(e.agent) }}>
                {KIND_GLYPH[e.kind] ?? "·"}
              </span>
              <span className="ct-log-body">
                <span className="ct-log-agent" style={{ color: agentColor(e.agent) }}>
                  {agentLabel(e.agent)}
                </span>
                <span className="ct-log-title">{e.kind === "task_started" ? `started ${skillLabel(e.row.skill)}` : e.title.replaceAll("_", " ")}</span>
                {e.detail && e.kind !== "task_started" && <span className="ct-log-detail">{e.detail}</span>}
              </span>
            </>
          );
          return (
            <li key={e.id} className="fl-reveal">
              {id ? (
                <button className="ct-log-row" onClick={() => onSelect(id)}>
                  {body}
                </button>
              ) : (
                <div className="ct-log-row">{body}</div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
