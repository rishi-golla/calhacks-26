"use client";
import { useEffect, useState } from "react";
import { RotateCcw, ShieldCheck, Check, X, BookOpen } from "lucide-react";
import {
  AGENTS,
  agentName,
  date,
  json,
  money,
  newest,
  num,
  str,
  type Row,
} from "@/lib/data";
import { useLive } from "./live-provider";
import {
  Badge,
  DataTable,
  Detail,
  Dialog,
  Empty,
  FlagButton,
  JsonDetail,
  Panel,
  Submit,
  statusColumn,
  timeColumn,
  type FlagHandler,
} from "./ui";
import type { Notify } from "./operations";
function approvalError(value: unknown): string {
  if (value === "invalid_code")
    return "The one-time code is incorrect. Try again or request a fresh code from your administrator.";
  if (value === "code_locked")
    return "This approval code is locked after five failed attempts. Ask your administrator for a fresh code.";
  return str(
    value,
    "The code was rejected. Request a fresh code from your administrator.",
  );
}
export function InboxView({
  onFlag,
  notify,
}: {
  onFlag: FlagHandler;
  notify: Notify;
}) {
  const { data, mutate, state, identity } = useLive();
  const [approvalId, setApprovalId] = useState("");
  const [code, setCode] = useState("");
  const [decision, setDecision] = useState("approved");
  const [busy, setBusy] = useState(false);
  const [decisionError, setDecisionError] = useState("");
  const [awaiting, setAwaiting] = useState<{
    id: string;
    decision: string;
    attempts: number;
  } | null>(null);
  const approval = data.approval.find((row) => row.id === approvalId);
  const pending = data.approval.filter(
    (row) => row.decision === "pending" || !row.decision,
  );
  useEffect(() => {
    if (!awaiting) return;
    const result = data.approval.find((row) => row.id === awaiting.id);
    if (result?.decision === awaiting.decision) {
      notify(
        `Approval ${awaiting.id} ${awaiting.decision}. Decision is recorded in SpacetimeDB.`,
      );
      setAwaiting(null);
      setBusy(false);
      setApprovalId("");
      setCode("");
    } else if (result && num(result.failed_attempts) > awaiting.attempts) {
      setDecisionError(approvalError(result.last_error));
      setAwaiting(null);
      setBusy(false);
      setCode("");
    }
  }, [data.approval, awaiting, notify]);
  useEffect(() => {
    if (!awaiting) return;
    const timer = setTimeout(() => {
      setBusy(false);
      setAwaiting(null);
      setDecisionError(
        "No decision was confirmed yet. Check the approval's live status before trying again.",
      );
    }, 12000);
    return () => clearTimeout(timer);
  }, [awaiting, notify]);
  const decide = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!approval) return;
    setDecisionError("");
    setBusy(true);
    setAwaiting({
      id: approvalId,
      decision,
      attempts: num(approval.failed_attempts),
    });
    try {
      await mutate("decide_approval", { id: approvalId, decision, code });
    } catch (error) {
      setAwaiting(null);
      setBusy(false);
      setDecisionError(String(error).replace(/^Error: /, ""));
    }
  };
  return (
    <div className="stack">
      <div className="notice">
        <ShieldCheck size={18} />
        <div>
          <strong>Human judgment, recorded.</strong>
          <p>
            Use the one-time code delivered by your administrator. Only the
            designated human can decide; the requester cannot approve their own
            action.
          </p>
        </div>
      </div>
      <Panel
        title="Approvals awaiting a decision"
        action={<span className="count">{pending.length}</span>}
      >
        {pending.length ? (
          <div className="approval-list">
            {pending.map((row) => (
              <article className="approval-card" key={str(row.id)}>
                <div className="approval-amount">
                  {money(row.amount)}
                  <span>{str(row.action).replaceAll("_", " ")}</span>
                </div>
                <div className="approval-body">
                  <strong>
                    {str(row.case_id)}{" "}
                    <span className="muted">/ {str(row.id)}</span>
                  </strong>
                  <p>
                    Requested by {str(row.requested_by)} · target{" "}
                    {str(row.target_id)}
                  </p>
                  <small>Expires {date(row.expires_at)}</small>
                  {row.last_error ? (
                    <p className="error-text">
                      {str(row.last_error)} · {num(row.failed_attempts)} failed
                      attempts
                    </p>
                  ) : null}
                </div>
                <div className="inline">
                  <FlagButton
                    onFlag={onFlag}
                    target={{
                      ref: `approval:${str(row.id)}`,
                      caseId: str(row.case_id),
                    }}
                  />
                  <button
                    className="button primary"
                    disabled={
                      state !== "live" ||
                      (Boolean(row.approver) && row.approver !== identity)
                    }
                    onClick={() => {
                      setApprovalId(str(row.id));
                      setCode("");
                      setDecisionError(
                        row.last_error ? approvalError(row.last_error) : "",
                      );
                    }}
                  >
                    Review decision
                  </button>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <Empty title="You're all caught up">
            New financial approvals appear here when the Controller needs human
            judgment.
          </Empty>
        )}
      </Panel>
      <Panel
        title="Flags & feedback"
        eyebrow="Persistent work queue to the Validator"
      >
        <DataTable
          rows={newest(data.flag)}
          table="flag"
          onFlag={onFlag}
          columns={[
            { key: "target_ref", title: "Target" },
            { key: "kind" },
            { key: "note" },
            statusColumn(),
            { key: "resolution_ref", title: "Resolution / change" },
            {
              key: "id",
              title: "Validator delivery",
              format: (value) => {
                const work = data.routing_outbox.find((row) =>
                  str(row.payload_json ?? row.payload, "").includes(str(value)),
                );
                return work ? (
                  <Badge value={work.status} />
                ) : (
                  <span className="muted">Awaiting router</span>
                );
              },
            },
            timeColumn(),
          ]}
          emptyTitle="No human flags yet"
          emptyText="Use a flag beside any task, cell, call, decision or memory. Feedback queues a Validator review."
        />
      </Panel>
      <Panel title="Decision history">
        <DataTable
          rows={newest(
            data.approval.filter(
              (row) => row.decision && row.decision !== "pending",
            ),
            "decided_at",
          )}
          table="approval"
          onFlag={onFlag}
          columns={[
            { key: "id" },
            { key: "case_id", title: "Case" },
            { key: "action" },
            statusColumn("decision", "Decision"),
            { key: "approver", title: "Human identity" },
            timeColumn("decided_at", "Decided"),
          ]}
        />
      </Panel>
      {approval && (
        <Dialog
          title={`Decide ${approvalId}`}
          onClose={() => {
            if (!busy) setApprovalId("");
          }}
        >
          <form onSubmit={decide} className="stack">
            <div className="detail-grid">
              <Detail label="Amount">{money(approval.amount)}</Detail>
              <Detail label="Action">{str(approval.action)}</Detail>
              <Detail label="Requested by">{str(approval.requested_by)}</Detail>
              <Detail label="Case">{str(approval.case_id)}</Detail>
            </div>
            <label>
              Decision
              <select
                value={decision}
                onChange={(event) => setDecision(event.target.value)}
              >
                <option value="approved">Approve</option>
                <option value="rejected">Reject</option>
              </select>
            </label>
            <label>
              One-time code
              <input
                autoComplete="one-time-code"
                type="password"
                minLength={6}
                maxLength={64}
                value={code}
                required
                aria-invalid={Boolean(decisionError)}
                aria-describedby={
                  decisionError ? "approval-decision-error" : undefined
                }
                onChange={(event) => setCode(event.target.value)}
                placeholder="Enter the privately delivered code"
              />
            </label>
            {decisionError && (
              <p
                id="approval-decision-error"
                className="notice warning"
                role="alert"
              >
                {decisionError}{" "}
                {num(approval.failed_attempts) > 0
                  ? `Failed attempts: ${num(approval.failed_attempts)} of 5.`
                  : ""}
              </p>
            )}
            <p className="muted small">
              Codes expire and lock after five failed attempts. A recorded
              decision does not itself execute the financial action.
            </p>
            <Submit busy={busy} disabled={state !== "live"}>
              {decision === "approved" ? <Check size={16} /> : <X size={16} />}{" "}
              Record {decision === "approved" ? "approval" : "rejection"}
            </Submit>
          </form>
        </Dialog>
      )}
    </div>
  );
}
export function LearningView({
  onFlag,
  notify,
}: {
  onFlag: FlagHandler;
  notify: Notify;
}) {
  const { data, mutate, state } = useLive();
  const [agent, setAgent] = useState("");
  const [review, setReview] = useState<Row | null>(null);
  const [revert, setRevert] = useState<Row | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [memory, setMemory] = useState<Row | null>(null);
  const [modalError, setModalError] = useState("");
  const versions = newest(
    data.instruction_version.filter((row) => !agent || row.agent === agent),
    "created_at",
  );
  const memories = newest(
    data.agent_memory.filter((row) => !agent || row.agent === agent),
    "created_at",
  );
  const doRevert = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!revert) return;
    setModalError("");
    setBusy(true);
    try {
      await mutate("revert_instruction_version", {
        agent: revert.agent,
        version: num(revert.version),
        expected_version: num(revert.expected_version),
        reason,
      });
      notify("Instruction restored through a new compensating version.");
      setRevert(null);
      setReason("");
    } catch (error) {
      setModalError(String(error).replace(/^Error: /, ""));
    } finally {
      setBusy(false);
    }
  };
  const toggleMemory = async (row: Row) => {
    setBusy(true);
    try {
      await mutate("upsert_memory", {
        id: row.id,
        agent: row.agent,
        scope: json(row.scope, {}),
        text: row.text,
        source_ref: str(row.source_ref, ""),
        active: !row.active,
      });
      notify(
        row.active
          ? "Memory deactivated. The previous version remains in history."
          : "Memory reactivated in a new version.",
      );
    } catch (error) {
      notify(String(error), true);
    } finally {
      setBusy(false);
    }
  };
  const restoreMemory = async (row: Row) => {
    setModalError("");
    setBusy(true);
    try {
      await mutate("upsert_memory", {
        id: row.memory_id ?? memory?.id,
        agent: row.agent ?? memory?.agent,
        scope: json(row.scope, {}),
        text: row.text,
        source_ref: `memory_revision:${str(row.id)}`,
        active: true,
      });
      notify("Memory restored as a new revision.");
      setMemory(null);
    } catch (error) {
      setModalError(String(error).replace(/^Error: /, ""));
    } finally {
      setBusy(false);
    }
  };
  const taskAgent = new Map(data.task.map((row) => [row.task_id, row.agent]));
  return (
    <div className="stack">
      <div className="toolbar">
        <select
          aria-label="Filter learning by agent"
          value={agent}
          onChange={(event) => setAgent(event.target.value)}
        >
          <option value="">All agents</option>
          {AGENTS.map((item) => (
            <option value={item.id} key={item.id}>
              {item.name}
            </option>
          ))}
        </select>
        <span className="muted small">
          Lessons can change instructions and memory. Policy and permissions
          remain fixed.
        </span>
      </div>
      <Panel title="Validator reviews">
        <DataTable
          rows={newest(
            data.review.filter(
              (row) => !agent || taskAgent.get(row.target_task_id) === agent,
            ),
          )}
          table="review"
          onFlag={onFlag}
          onRow={setReview}
          columns={[
            { key: "id", title: "Review" },
            { key: "target_task_id", title: "Task" },
            { key: "case_id", title: "Case" },
            statusColumn("verdict", "Verdict"),
            {
              key: "friction_json",
              title: "Friction",
              format: (value) => `${json<unknown[]>(value, []).length} items`,
            },
            timeColumn(),
          ]}
          emptyTitle="Waiting for the first review"
          emptyText="Completed AP, AR, Controller and CFO outputs queue a deterministic and ensemble review."
        />
      </Panel>
      {review && (
        <Panel
          title={`Review ${str(review.id)}`}
          action={
            <button className="text-button" onClick={() => setReview(null)}>
              Close
            </button>
          }
        >
          <div className="padded stack">
            <DataTable
              rows={json<Row[]>(review.checks_json, [])}
              columns={[
                { key: "name", title: "Check" },
                {
                  key: "ok",
                  title: "Result",
                  format: (value) => <Badge value={value ? "pass" : "issue"} />,
                },
                { key: "expected" },
                { key: "actual" },
              ]}
            />
            <h3>Friction & lessons</h3>
            <DataTable
              rows={json<Row[]>(review.friction_json, [])}
              columns={[
                { key: "kind" },
                { key: "detail" },
                { key: "cost_s", title: "Cost (seconds)" },
              ]}
              emptyTitle="No friction reported"
            />
            <Detail label="Actions">{str(review.actions)}</Detail>
          </div>
        </Panel>
      )}
      <Panel
        title="Agent memory"
        eyebrow="Scoped lessons, loaded into future tasks"
      >
        {memories.length ? (
          <div className="memory-list">
            {memories.map((row) => (
              <article className="memory-card" key={str(row.id)}>
                <div className="panel-head">
                  <div className="inline">
                    <BookOpen size={17} />
                    <strong>{agentName(row.agent)}</strong>
                    <span className="subtle-badge">v{str(row.version)}</span>
                    <Badge value={row.active ? "active" : "inactive"} />
                  </div>
                  <div className="inline">
                    <FlagButton
                      onFlag={onFlag}
                      target={{ ref: `memory:${str(row.id)}` }}
                    />
                    <button
                      className="text-button"
                      onClick={() => {
                        setModalError("");
                        setMemory(row);
                      }}
                    >
                      History
                    </button>
                    <button
                      className="button"
                      disabled={state !== "live" || busy}
                      onClick={() => void toggleMemory(row)}
                    >
                      {row.active ? "Deactivate" : "Reactivate"}
                    </button>
                  </div>
                </div>
                <p>{str(row.text)}</p>
                <div className="muted small">
                  Scope {str(row.scope)} · {str(row.source)} ·{" "}
                  {date(row.created_at)}
                </div>
              </article>
            ))}
          </div>
        ) : (
          <Empty title="No learned memories yet">
            Validated lessons will appear with their scope, source and version.
          </Empty>
        )}
      </Panel>
      <Panel title="Instruction history">
        <div className="instruction-list">
          {versions.length ? (
            versions.map((row) => (
              <article
                className="instruction-card"
                key={`${row.agent}:${row.version}`}
              >
                <div className="panel-head">
                  <div className="inline">
                    <strong>{agentName(row.agent)}</strong>
                    <span className="subtle-badge">v{str(row.version)}</span>
                    {row.active ? <Badge value="active" /> : null}
                  </div>
                  <div className="inline">
                    {row.active ? (
                      <button className="button" disabled={state !== "live" || busy}
                        onClick={() => {
                          setRevert({ ...row, version: 0, expected_version: num(row.version) });
                          setModalError("");
                          setReason("");
                        }}>
                        Restore original instructions
                      </button>
                    ) : null}
                    <FlagButton
                      onFlag={onFlag}
                      target={{
                        ref: `instruction:${str(row.agent)}:${str(row.version)}`,
                      }}
                    />
                    <button
                      className="button"
                      disabled={Boolean(row.active) || state !== "live"}
                      onClick={() => {
                        setRevert({ ...row, expected_version: Math.max(0, ...data.instruction_version
                          .filter((item) => item.agent === row.agent).map((item) => num(item.version))) });
                        setModalError("");
                        setReason("");
                      }}
                    >
                      <RotateCcw size={13} /> Restore this version
                    </button>
                  </div>
                </div>
                <p>{str(row.reason)}</p>
                <pre className="diff-code">{str(row.diff)}</pre>
                <small className="muted">
                  By {str(row.author)} · {date(row.created_at)} ·{" "}
                  {str(row.source_ref)}
                </small>
              </article>
            ))
          ) : (
            <Empty title="No instruction patches yet">
              The Validator publishes versioned diffs here. Every restore
              creates a new version.
            </Empty>
          )}
        </div>
      </Panel>
      {revert && (
        <Dialog
          title={`Restore ${agentName(revert.agent)} v${str(revert.version)}`}
          onClose={() => setRevert(null)}
        >
          <form className="stack" onSubmit={doRevert}>
            <p>
              A new version will restore this instruction state. Existing
              history stays intact.
            </p>
            <label>
              Reason
              <textarea
                required
                minLength={3}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Why should this version be restored?"
              />
            </label>
            {modalError && (
              <p className="notice warning" role="alert">
                {modalError}
              </p>
            )}
            <Submit busy={busy}>Restore instruction version</Submit>
          </form>
        </Dialog>
      )}
      {memory && (
        <Dialog
          title={`Memory history · ${agentName(memory.agent)}`}
          onClose={() => setMemory(null)}
        >
          <div className="stack">
            {modalError && (
              <p className="notice warning" role="alert">
                {modalError}
              </p>
            )}
            {newest(
              data.memory_revision.filter((row) => row.memory_id === memory.id),
              "created_at",
            ).map((row) => (
              <article className="memory-card" key={str(row.id)}>
                <div className="panel-head">
                  <strong>Version {str(row.version)}</strong>
                  <button
                    className="button"
                    disabled={
                      busy || state !== "live" || row.version === memory.version
                    }
                    onClick={() => void restoreMemory(row)}
                  >
                    <RotateCcw size={13} /> Restore
                  </button>
                </div>
                <p>{str(row.text)}</p>
                <div className="muted small">
                  {date(row.created_at)} · {str(row.source)}
                </div>
                <details>
                  <summary>Scope & provenance</summary>
                  <JsonDetail value={row} />
                </details>
              </article>
            ))}
          </div>
        </Dialog>
      )}
    </div>
  );
}
