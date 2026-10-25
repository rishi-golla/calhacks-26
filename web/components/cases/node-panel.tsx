"use client";
/**
 * Right-hand drawer for the trace: full details of one node (trigger,
 * documents, tool calls, outputs, communication, audit trail) or the case log.
 * Filtered by the replay cursor like the graph.
 */
import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";
import { ArrowDownRight, ArrowUpLeft, Phone, X } from "lucide-react";
import { ApifyFinding, ApifyMark } from "@/components/apify";
import { EvidenceChip } from "@/components/evidence";
import { Mascot } from "@/components/mascot";
import { CheckList, Workings } from "@/components/workings";
import { agentColor, agentLabel, skillLabel } from "@/lib/agents";
import { apifyEvidence, APIFY_TOOLS } from "@/lib/apify";
import { json, num, str, timestamp, type Row } from "@/lib/data";
import { idList, learnedLines, memoryNote, reviewRefs, type MemoryNote } from "@/lib/lessons";
import { fmtMoney, type TimelineEvent } from "@/lib/ops";
import {
  approvalRequestedAt,
  clock,
  dispatchEventType,
  eventLabel,
  isMilestone,
  maskPhone,
  nodeStatus,
  routingReason,
  shortDur,
  toolLabel,
  toolsAt,
  type CaseTrace,
  type NodeStatus,
  type TraceNode,
} from "./model";

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
        {count !== undefined && count > 1 && <span className="fl-mono fl-mute">{count}</span>}
      </h3>
      {children}
    </section>
  );
}

/** Pretty-printed JSON, or null for an empty value. */
function pretty(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "string" ? json<unknown>(value, value) : value;
  if (parsed && typeof parsed === "object" && Object.keys(parsed).length === 0) return null;
  return typeof parsed === "string" ? parsed : JSON.stringify(parsed, null, 2);
}

export function Json({ value, label }: { value: unknown; label: string }) {
  const text = pretty(value);
  if (text === null) return null;
  return (
    <details className="ct-json">
      <summary>
        <span className="fl-micro">{label}</span>
      </summary>
      <pre>{text}</pre>
    </details>
  );
}

/** A row that opens to show its payload; a plain row when there is nothing to show. */
function Expandable({ line, payload }: { line: ReactNode; payload: [string, unknown][] }) {
  const parts = payload.map(([k, v]) => [k, pretty(v)] as const).filter((p): p is readonly [string, string] => p[1] !== null);
  if (!parts.length) return <div className="ct-line">{line}</div>;
  return (
    <details className="ct-json ct-expand">
      <summary className="ct-line">{line}</summary>
      {parts.map(([k, text]) => (
        <pre key={k} data-label={parts.length > 1 ? k : undefined}>
          {text}
        </pre>
      ))}
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
  n.kind === "task" ? `${agentLabel(n.lane)} · ${skillLabel(n.task?.skill)}` : n.kind === "intake" ? "Routed" : "Human approval";

type PanelProps = { trace: CaseTrace; node: TraceNode; cursor: number; onSelect: (id: string) => void; onClose: () => void };

export function NodeDetails({ trace, node, cursor, onSelect, onClose }: PanelProps) {
  const status = nodeStatus(node, cursor);
  return (
    <div className="ct-details" style={{ "--agent": agentColor(node.lane) } as CSSProperties}>
      <div className="ct-panel-head">
        <span className="fl-section" style={{ "--accent": agentColor(node.lane) } as CSSProperties}>
          {agentLabel(node.lane)}
        </span>
        <button className="fl-btn quiet small" onClick={onClose} aria-label="Close details">
          <X size={14} />
        </button>
      </div>
      <div className="ct-details-hero">
        <Mascot id={node.lane} size={52} mood={status === "working" ? (node.lane === "voice_desk" ? "talking" : "working") : status === "waiting" || status === "pending" || status === "failed" ? "alert" : "idle"} />
        <div style={{ minWidth: 0 }}>
          <div className="ct-details-title" title={node.kind === "task" ? node.task?.displayId : undefined}>
            {node.kind === "task" ? skillLabel(node.task?.skill) : nodeTitle(node)}
          </div>
          {node.kind === "task" && status && <TaskMeta node={node} cursor={cursor} />}
          {status && status !== "completed" && (
            <span className={`fl-chip outline ${TONE[status]}`} style={{ marginTop: 6 }}>
              {status === "waiting" ? "waiting for human" : status}
            </span>
          )}
        </div>
      </div>
      {!status ? (
        <p className="ct-empty-note">Not reached yet.</p>
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

/** One line under the title: when it ran, how long, which model, how sure. */
function TaskMeta({ node, cursor }: { node: TraceNode; cursor: number }) {
  const t = node.task!;
  const done = node.end > 0 && node.end <= cursor;
  const took = shortDur((done ? node.end : Math.min(cursor, Date.now())) - t.startedAt);
  return (
    <div className="fl-micro ct-details-sub" title={done ? `${clock(t.startedAt)} → ${clock(node.end)}` : `started ${clock(t.startedAt)}`}>
      {[clock(t.startedAt), done ? took : `${took} so far`, t.model, done && t.confidence ? `${Math.round(t.confidence * 100)}% sure` : ""].filter(Boolean).join(" · ")}
    </div>
  );
}

function TaskDetails({ trace, node, cursor, onSelect }: PanelProps) {
  const t = node.task!;
  const status = nodeStatus(node, cursor)!;
  const done = node.end > 0 && node.end <= cursor;
  const incoming = trace.edges.find((e) => e.to === node.id && e.kind !== "decision");
  const outgoing = trace.edges.filter((e) => e.from === node.id && e.at <= cursor && e.kind !== "approval");
  const audit = (trace.auditByTask.get(node.id) ?? []).filter((a) => timestamp(a.ts) <= cursor);
  // Tool calls already have their own section.
  const auditShown = audit.filter((a) => a.kind !== "tool_call");
  const outboxAudit = audit.find((a) => a.reason_code === "REVIEW_OUTBOX" || a.reason_code === "TASK_RETRY");
  const tools = toolsAt(node, cursor);
  const docs = node.docs.filter((d) => d.at <= cursor);
  const calls = trace.slice.call.filter((c) => c.task_id === node.id && timestamp(c.started_at) <= cursor);
  const approvals = trace.slice.approval.filter((a) => a.task_id === node.id && approvalRequestedAt(a, trace.slice.audit_event) <= cursor);
  const reviewsOf = trace.slice.review.filter((r) => r.target_task_id === node.id && timestamp(r.ts) <= cursor);
  const ownReview = t.agent === "validator" ? trace.slice.review.find((r) => str(r.id) === `REV-${node.id}` && timestamp(r.ts) <= cursor) : undefined;
  const artifact = done ? t.artifact : null;
  const memory = new Map(trace.slice.agent_memory.map((m) => [str(m.id, ""), m]));
  const loaded = idList(t.row.memory_ids);
  const used = loaded.flatMap((id) => (memory.has(id) ? [memoryNote(memory.get(id)!)] : []));
  const instrVersion = num(t.row.instr_version);
  // What this task's review taught: lessons and instruction versions pointing at its review (live) or the task (sim).
  const teachRefs = reviewRefs(node.id, t.artifact);
  const taught =
    t.agent === "validator" ? trace.slice.agent_memory.filter((m) => teachRefs.has(str(m.source_ref, "")) && timestamp(m.created_at) <= cursor).map(memoryNote) : [];
  const published = t.agent === "validator" ? trace.slice.instruction_version.filter((v) => teachRefs.has(str(v.source_ref, "")) && timestamp(v.created_at) <= cursor) : [];

  return (
    <>
      <Section title="Trigger">
        {incoming ? (
          <p className="ct-line">
            <ArrowUpLeft size={13} />
            <NodeLink trace={trace} id={incoming.from} onSelect={onSelect}>
              {agentLabel(trace.nodeById.get(incoming.from)?.lane)}
            </NodeLink>
            {incoming.kind === "dispatch"
              ? " routed this"
              : incoming.kind === "review"
                ? " sent its output for review"
                : incoming.kind === "retry"
                  ? " failed; retried"
                  : " delegated this"}{" "}
            <span className="fl-mono fl-mute">{clock(incoming.at)}</span>
          </p>
        ) : outboxAudit ? (
          <p className="ct-line">
            <ArrowUpLeft size={13} /> {str(outboxAudit.summary)} <span className="fl-mono fl-mute">{clock(timestamp(outboxAudit.ts))}</span>
          </p>
        ) : (
          <p className="ct-line fl-mute">No trigger recorded.</p>
        )}
        {incoming?.kind === "dispatch" && routingReason(str(trace.nodeById.get(incoming.from)?.dispatch?.reason, "")) && (
          <p className="ct-quote">{routingReason(str(trace.nodeById.get(incoming.from)?.dispatch?.reason, ""))}</p>
        )}
      </Section>

      {loaded.length > 0 && (
        <Section title="Lessons used" count={loaded.length}>
          {instrVersion > 0 && <p className="ct-line fl-mute">Ran on instructions v{instrVersion}.</p>}
          <ul className="ct-lessons">
            {used.map((m) => (
              <MemoryItem key={m.id} note={m} caseId={trace.caseId} />
            ))}
          </ul>
          {loaded.length > used.length && (
            <p className="ct-line fl-mute">
              {loaded.length - used.length} more {loaded.length - used.length === 1 ? "memory is" : "memories are"} no longer recorded.
            </p>
          )}
        </Section>
      )}

      {(taught.length > 0 || published.length > 0) && (
        <Section title="Lessons written" count={taught.length}>
          <ul className="ct-lessons">
            {taught.map((m) => (
              <MemoryItem key={m.id} note={m} caseId={trace.caseId} to={m.agent} />
            ))}
            {published.map((v) => (
              <li key={str(v.id)} style={{ "--agent": agentColor(str(v.agent)) } as CSSProperties}>
                <div className="who">
                  → {agentLabel(str(v.agent))} · instructions v{num(v.version)}
                </div>
                {learnedLines(v.diff).map((line, i) => (
                  <p key={i}>{line}</p>
                ))}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {docs.length > 0 && (
        <Section title="Documents" count={docs.length}>
          <ul className="ct-list">
            {docs.map((d) => (
              <li key={d.id}>
                <div className="ct-line">
                  {d.docRef ? <EvidenceChip evidenceRef={d.docRef} className="ct-doc-ref" /> : <span className="fl-mono fl-mute">unreferenced</span>}
                  {(d.page > 1 || d.span) && (
                    <span className="fl-micro">
                      {d.page > 1 ? `p${d.page}` : ""}
                      {d.page > 1 && d.span ? " · " : ""}
                      {d.span}
                    </span>
                  )}
                </div>
                <Json value={d.extracted} label="fields" />
              </li>
            ))}
          </ul>
        </Section>
      )}

      {tools.length > 0 && (
        <Section title="Actions" count={tools.length}>
          <ul className="ct-list actions">
            {tools.map(({ call, status: s }) => {
              const apify = apifyEvidence({ ...call, status: s === "running" ? "started" : call.status });
              return (
              <li key={call.id} className={apify ? "apify" : undefined}>
                <Expandable
                  line={
                    <>
                      <span className={`ct-tool-glyph ${s}`}>{s === "running" ? "…" : s === "completed" ? "✓" : "✗"}</span>
                      {apify && <ApifyMark size={12} title="Called Apify" />}
                      <span title={call.name}>{toolLabel(call.name)}</span>
                      <span className="fl-micro" style={{ marginLeft: "auto" }}>
                        {s === "running" ? "running" : call.durationMs ? shortDur(call.durationMs) : s === "completed" ? "" : s}
                      </span>
                    </>
                  }
                  payload={[
                    ["args", call.arguments],
                    ["result", s === "running" ? null : call.result],
                  ]}
                />
                {apify && <ApifyFinding evidence={apify} />}
              </li>
              );
            })}
          </ul>
        </Section>
      )}

      <Section title="Output">
        {done || status === "waiting" ? (
          <>
            <span className={`fl-chip outline ${TONE[status]}`}>{(t.outcome || t.businessStatus || status).replaceAll("_", " ")}</span>
            {str(t.artifact?.summary, "") && <p className="ct-quote">{str(t.artifact?.summary)}</p>}
            {artifact && <Workings items={(artifact.findings as Row | undefined)?.workings} />}
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
                  <EvidenceChip key={r} evidenceRef={r} />
                ))}
              </div>
            )}
            {artifact && <Json value={artifact} label="raw output" />}
          </>
        ) : (
          <p className="ct-line fl-mute">Still working.</p>
        )}
      </Section>

      {(outgoing.length > 0 || approvals.length > 0 || calls.length > 0 || reviewsOf.length > 0 || ownReview) && (
      <Section title="Handoffs">
        <ul className="ct-list compact">
          {handoffRows(outgoing, trace, cursor).map(({ e, child, s, ids }) => (
            <li key={e.id} className="ct-line">
              <ArrowDownRight size={13} /> to{" "}
              <NodeLink trace={trace} id={e.to} onSelect={onSelect}>
                {agentLabel(child.lane)}
              </NodeLink>
              {e.kind === "retry" ? <span className="fl-mute">retry</span> : e.kind !== "review" && <span className="fl-mute">{skillLabel(e.skill)}</span>}
              {/* Folded handoffs: one numbered link per task so each stays reachable. */}
              {ids.length > 1 &&
                ids.map((id, i) => (
                  <NodeLink key={id} trace={trace} id={id} onSelect={onSelect}>
                    <span className="fl-mono" title={`Open handoff ${i + 1} of ${ids.length}`}>
                      {i + 1}
                    </span>
                  </NodeLink>
                ))}
              {s && s !== "completed" && <span className={`fl-chip outline ${TONE[s]} ct-mini-chip`}>{s}</span>}
            </li>
          ))}
          {approvals.map((a) => (
            <li key={str(a.id)} className="ct-line">
              <ArrowDownRight size={13} /> asked{" "}
              <NodeLink trace={trace} id={`human:${str(a.id)}`} onSelect={onSelect}>
                a human
              </NodeLink>
              to approve {str(a.action).replaceAll("_", " ")} <span className="fl-num">{fmtMoney(num(a.amount))}</span>
            </li>
          ))}
        </ul>
        {calls.map((c) => (
          <CallRecord key={str(c.id)} call={c} cursor={cursor} />
        ))}
        {[...reviewsOf, ...(ownReview ? [ownReview] : [])].map((r) => (
          <ReviewRecord key={str(r.id)} review={r} trace={trace} onSelect={onSelect} own={r === ownReview} />
        ))}
      </Section>
      )}

      {auditShown.length > 0 && (
        <section className="ct-sec">
          <details className="ct-json ct-audit-fold">
            <summary>
              <span className="fl-micro">audit · {auditShown.length}</span>
            </summary>
            <ol className="ct-audit">
              {auditShown.map((a) => {
                const code = str(a.reason_code, str(a.kind));
                const text = auditText(a);
                return (
                  <li key={str(a.id)}>
                    <span className="fl-mono fl-mute">{clock(timestamp(a.ts))}</span>
                    <span className="ct-audit-text" title={code}>
                      {text === code ? text.replaceAll("_", " ") : text}
                    </span>
                  </li>
                );
              })}
            </ol>
          </details>
        </section>
      )}
    </>
  );
}

/** Outgoing edges, identical ones (same agent, kind, skill and status) folded into one row that keeps every target task. */
function handoffRows(edges: CaseTrace["edges"], trace: CaseTrace, cursor: number) {
  const rows = new Map<string, { e: CaseTrace["edges"][number]; child: TraceNode; s: NodeStatus | null; ids: string[] }>();
  for (const e of edges) {
    const child = trace.nodeById.get(e.to)!;
    const s = nodeStatus(child, cursor);
    const key = `${child.lane}|${e.kind}|${e.skill ?? ""}|${s}`;
    const row = rows.get(key);
    if (row) row.ids.push(e.to);
    else rows.set(key, { e, child, s, ids: [e.to] });
  }
  return [...rows.values()];
}

function auditText(a: Row) {
  return str(a.summary, str(a.kind));
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
          [
            "To",
            <span key="to" className="fl-mono" title={str(call.intended_to, "") ? `intended ${maskPhone(str(call.intended_to))}` : undefined}>
              {str(call.to_masked, "") || maskPhone(str(call.intended_to, ""))}
            </span>,
          ],
          ["Routed to", str(call.routed_to, "")],
          ["Persona", str(call.persona, "")],
          [
            "Time",
            <span key="t" className="fl-mono">
              {clock(timestamp(call.started_at))}
              {finished ? ` → ${clock(ended)}` : ""}
            </span>,
          ],
        ]}
      />
      {Boolean(call.disclosure_given) && <span className="fl-micro">AI disclosed</span>}
      {finished && <Json value={call.findings_json} label="findings" />}
      {finished && str(call.transcript_redacted, "") && <p className="ct-transcript">{str(call.transcript_redacted)}</p>}
    </div>
  );
}

/** A memory the task loaded (or, with `to`, a lesson this review wrote for that agent). */
function MemoryItem({ note: m, caseId, to }: { note: MemoryNote; caseId: string; to?: string }) {
  return (
    <li style={{ "--agent": agentColor(to ?? (m.byValidator ? "validator" : "human")) } as CSSProperties}>
      <div className="who">
        {to ? `→ ${agentLabel(to)}` : m.byValidator ? "Lesson from Settlement Quality Review" : "Note from a person"}
        {!m.active && " · since retired"}
      </div>
      <p>{m.text}</p>
      {m.scope.length > 0 && (
        <div className="scope">
          {m.scope.map((c) => (
            <span key={c}>{c}</span>
          ))}
        </div>
      )}
      {!to && m.sourceCase && m.sourceCase !== caseId && (
        <div className="foot">
          learned on{" "}
          <Link href={`/cases/${encodeURIComponent(m.sourceCase)}`}>{m.sourceCase.replace(/^CASE-/, "")}</Link> · {clock(m.at)}
        </div>
      )}
    </li>
  );
}

function ReviewRecord({ review, trace, onSelect, own }: { review: Row; trace: CaseTrace; onSelect: (id: string) => void; own: boolean }) {
  const friction = json<Row[]>(review.friction_json, []);
  const verdict = str(review.verdict);
  return (
    <div className="ct-record">
      <div className="ct-line">
        <strong>Review</strong>
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
        ) : null}
      </div>
      <CheckList checks={review.checks_json} />
      {Array.isArray(friction) && friction.length > 0 && <Json value={friction} label="friction" />}
    </div>
  );
}

function IntakeDetails({ trace, node, onSelect }: PanelProps) {
  const d = node.dispatch!;
  const target = str(d.task_id, "");
  const reason = routingReason(str(d.reason, ""));
  const meta = [str(d.status, ""), d.queue_ms !== undefined ? `queued ${num(d.queue_ms)} ms` : "", str(d.policy_version, "")].filter(Boolean).join(" · ");
  return (
    <Section title="Routing">
      <p className="ct-line">
        <ArrowDownRight size={13} />
        <NodeLink trace={trace} id={target} onSelect={onSelect}>
          {agentLabel(str(d.agent))}
        </NodeLink>
        <span className="fl-mute">{skillLabel(str(d.skill))}</span>
        <span className="fl-mono fl-mute">{clock(node.start)}</span>
      </p>
      {reason && <p className="ct-quote">{reason}</p>}
      {meta && <p className="fl-micro" style={{ marginTop: 8 }}>{meta}</p>}
    </Section>
  );
}

function HumanDetails({ trace, node, cursor, onSelect }: PanelProps) {
  const a = node.approval!;
  const decided = node.end > 0 && node.end <= cursor;
  return (
    <>
      <Section title="Trigger">
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
            [
              "Decision",
              decided ? (
                <span>
                  {str(a.decision)} {str(a.approver_name || a.approver, "") && `by ${str(a.approver_name || a.approver)}`}{" "}
                  <span className="fl-mono fl-mute">{clock(node.end)}</span>
                </span>
              ) : (
                "pending"
              ),
            ],
            ["Code", decided ? (a.code_ok ? "verified" : "not verified") : ""],
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

const apifyTool = (e: TimelineEvent) =>
  e.kind === "tool_call" && str(json<Row>(e.row.summary, {}).tool_name, "") in APIFY_TOOLS;

/** "Run workflow · 8.6s", or "… failed" when it did not complete. */
function toolLogTitle(row: Row) {
  const call = json<Row>(row.summary, {});
  const status = str(call.status, "");
  const dur = call.duration_ms ? shortDur(num(call.duration_ms)) : "";
  return [toolLabel(str(call.tool_name, "tool")), status === "completed" ? "" : status, dur].filter(Boolean).join(" · ");
}

export function CaseLog({ trace, cursor, onSelect, onClose }: { trace: CaseTrace; cursor: number; onSelect: (id: string) => void; onClose: () => void }) {
  const edgeTasks = new Set(trace.edges.filter((e) => e.kind === "delegation").map((e) => e.to));
  // A review's verdict shows on its task's completion row, once the cursor has reached it.
  const reviewed = new Set(trace.timeline.filter((e) => e.kind === "task_completed" && e.at <= cursor).map((e) => e.taskId));
  const events = trace.timeline.filter((e) => {
    if (e.at > cursor) return false;
    if (e.kind === "delegation" && e.id.startsWith("audit:") && edgeTasks.has(e.taskId)) return false;
    if (e.kind === "tool_call" && / · started$/.test(e.title)) return false;
    // Bookkeeping the log already shows another way.
    if (e.id.startsWith("audit:") && e.row.kind === "add_review" && reviewed.has(e.taskId)) return false;
    if (e.id.startsWith("audit:") && e.row.kind === "upsert_source_record") return false;
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
          // A lifecycle event the router only logged reads as "Load delivered · logged", not as a routing.
          const milestone = e.kind === "dispatch" && isMilestone(e.row);
          const title = milestone
            ? `${eventLabel(dispatchEventType(str(e.row.reason, "")))} · logged`
            : e.kind === "task_started"
              ? `started ${skillLabel(e.row.skill)}`
              : e.kind === "tool_call"
                ? toolLogTitle(e.row)
                : e.title.replaceAll("_", " ");
          const detail = milestone || e.kind === "task_started" || e.kind === "tool_call" ? "" : e.kind === "dispatch" ? routingReason(e.detail) : e.detail;
          const body = (
            <>
              <span className="fl-mono fl-mute ct-log-time">{clock(e.at)}</span>
              <span className="ct-log-glyph" style={{ color: agentColor(e.agent) }}>
                {milestone ? "·" : apifyTool(e) ? <ApifyMark size={11} title="Apify" /> : (KIND_GLYPH[e.kind] ?? "·")}
              </span>
              <span className="ct-log-body">
                <span className="ct-log-agent" style={{ color: agentColor(e.agent) }}>
                  {agentLabel(e.agent)}
                </span>
                <span className="ct-log-title">{title}</span>
                {detail && <span className="ct-log-detail">{detail}</span>}
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
