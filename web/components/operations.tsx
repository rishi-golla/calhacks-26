"use client";
import dynamic from "next/dynamic";
import { useState } from "react";
import { ArrowRight, ShieldCheck, Phone, GitBranch, Hand } from "lucide-react";
import {
  AGENTS,
  agentName,
  date,
  json,
  money,
  newest,
  num,
  str,
  timestamp,
  type Row,
} from "@/lib/data";
import { useLive } from "./live-provider";
import {
  Badge,
  DataTable,
  Detail,
  Empty,
  ExternalLink,
  FlagButton,
  JsonDetail,
  Panel,
  agentColumn,
  moneyColumn,
  statusColumn,
  timeColumn,
  type FlagHandler,
  useClock,
} from "./ui";
const AgentNetwork = dynamic(() => import("./agent-network"), {
  ssr: false,
  loading: () => <div className="network loading">Loading live network…</div>,
});
export type Notify = (message: string, error?: boolean) => void;
export function Scoreboard() {
  const { data, state } = useLive();
  const metrics = new Map(
    data.metric.map((row) => [str(row.name), num(row.value)]),
  );
  const value = (names: string[]) =>
    names.map((name) => metrics.get(name)).find((item) => item !== undefined);
  const specs = [
    {
      label: "Precision",
      value: value(["precision"]),
      format: (x: number) => `${(x * 100).toFixed(1)}%`,
      note: "Correct findings / all findings",
    },
    {
      label: "Recall",
      value: value(["recall"]),
      format: (x: number) => `${(x * 100).toFixed(1)}%`,
      note: "Planted errors identified",
    },
    {
      label: "Dollars caught",
      value: value(["dollars_caught", "caught_usd", "amount_caught"]),
      format: money,
      note: "Verified against the answer key",
    },
    {
      label: "False holds",
      value: value(["false_holds"]),
      format: String,
      note: "Clean invoices held incorrectly",
    },
    {
      label: "Median resolution",
      value: value([
        "median_resolution_s",
        "median_time_to_resolution",
        "median_resolution_seconds",
      ]),
      format: (x: number) => `${Math.round(x)}s`,
      note: "From case open to resolution",
    },
    {
      label: "Calls placed",
      value: value(["calls_placed"]) ?? (data.call.length || undefined),
      format: String,
      note: "Demo destinations only",
    },
  ];
  return (
    <div className="scoreboard">
      {specs.map((spec) => (
        <div className="score" key={spec.label}>
          <span>{spec.label}</span>
          <strong>
            {spec.value === undefined || state === "offline"
              ? "—"
              : spec.format(spec.value)}
          </strong>
          <p>
            {spec.value === undefined ? "Awaiting evaluator data" : spec.note}
          </p>
        </div>
      ))}
    </div>
  );
}
export function Overview({
  onFlag,
  openCase,
  navigate,
}: {
  onFlag: FlagHandler;
  openCase: (id: string) => void;
  navigate: (view: string) => void;
}) {
  const { data, state } = useLive();
  const now = useClock();
  const tasks = newest(data.task, "updated_at").slice(0, 6);
  const pending = data.approval.filter((row) =>
    ["pending", "requested", ""].includes(str(row.decision, "")),
  );
  const activeCases = data.case.filter(
    (row) => !["resolved", "rejected", "failed"].includes(str(row.status)),
  );
  const activeHolds = data.hold.filter(
    (row) => row.status === "active" || row.status === "held",
  );
  return (
    <div className="stack">
      <div className="hero-strip">
        <div>
          <span className="eyebrow">
            Golden Gate Freight · autonomous back office
          </span>
          <h2>Your operations, in sync.</h2>
          <p>Every agent, decision and dollar. One live source of truth.</p>
        </div>
        <span className="hero-mark">
          <GitBranch size={52} strokeWidth={1.1} />
        </span>
      </div>
      <div className="summary-grid">
        <div className="summary-card">
          <span>Open cases</span>
          <strong>{state === "offline" ? "—" : activeCases.length}</strong>
          <small>
            {data.case.filter((row) => row.status === "human_owned").length}{" "}
            under human control
          </small>
        </div>
        <div className="summary-card">
          <span>Agents online</span>
          <strong>
            {
              data.agent.filter(
                (row) =>
                  !["offline", "error", "stopped"].includes(str(row.status)) &&
                  now - timestamp(row.last_seen) < 60000,
              ).length
            }
            <em> / 8</em>
          </strong>
          <small>Independent A2A services</small>
        </div>
        <button
          className="summary-card clickable"
          onClick={() => navigate("inbox")}
        >
          <span>
            Needs your attention <ArrowRight size={14} />
          </span>
          <strong>{pending.length}</strong>
          <small>Pending human approvals</small>
        </button>
        <div className="summary-card">
          <span>Funds on hold</span>
          <strong>
            {state === "offline"
              ? "—"
              : money(
                  activeHolds.reduce(
                    (sum, row) =>
                      sum +
                      Math.max(0, num(row.amount) - num(row.released_amount)),
                    0,
                  ),
                )}
          </strong>
          <small>{activeHolds.length} active controls</small>
        </div>
      </div>
      <Panel
        title="The agent network"
        eyebrow="Coordinated in real time"
        action={
          <span className="subtle-badge">
            <i className={`dot ${state === "live" ? "teal" : "gray"}`} />
            {state === "live" ? "Live subscriptions" : "Waiting for live data"}
          </span>
        }
      >
        <AgentNetwork />
      </Panel>
      <div className="overview-bottom">
        <Panel
          title="Latest work"
          action={
            <button className="text-button" onClick={() => navigate("cases")}>
              All cases <ArrowRight size={14} />
            </button>
          }
        >
          <DataTable
            rows={tasks}
            table="task"
            onFlag={onFlag}
            onRow={(row) => openCase(str(row.case_id))}
            columns={[
              {
                key: "display_id",
                title: "Task",
                format: (value, row) => str(value, str(row.task_id)),
              },
              agentColumn,
              { key: "skill", title: "Work" },
              statusColumn("business_status"),
              timeColumn("updated_at", "Updated"),
            ]}
            emptyTitle="Ready for the first case"
            emptyText="Run the simulator or ingest a case. Agent work will stream into this feed automatically."
          />
        </Panel>
        <Panel title="Built-in controls">
          <div className="controls-list">
            <div>
              <ShieldCheck size={18} />
              <span>
                <strong>Separation of duties</strong>
                <small>Requester and approver stay independent.</small>
              </span>
            </div>
            <div>
              <GitBranch size={18} />
              <span>
                <strong>Traceable changes</strong>
                <small>Financial changes and audit commit together.</small>
              </span>
            </div>
            <div>
              <Phone size={18} />
              <span>
                <strong>Demo outreach only</strong>
                <small>Real contacts, team-controlled destinations.</small>
              </span>
            </div>
          </div>
        </Panel>
      </div>
      <Panel title="Outcomes, measured" eyebrow="Live evaluation">
        <Scoreboard />
      </Panel>
    </div>
  );
}
export function CasesView({
  selected,
  setSelected,
  query,
  onFlag,
  notify,
}: {
  selected: string;
  setSelected: (id: string) => void;
  query: string;
  onFlag: FlagHandler;
  notify: Notify;
}) {
  const { data, mutate, state } = useLive();
  const [agent, setAgent] = useState("");
  const [taskId, setTaskId] = useState("");
  const [busy, setBusy] = useState(false);
  const current = data.case.find((row) => row.case_id === selected);
  const matches = (row: Row) =>
    !query || JSON.stringify(row).toLowerCase().includes(query.toLowerCase());
  const tasks = newest(
    data.task.filter(
      (row) =>
        (!selected || row.case_id === selected) &&
        (!agent || row.agent === agent) &&
        matches(row),
    ),
    "updated_at",
  );
  const task = data.task.find(
    (row) => row.task_id === taskId && (!selected || row.case_id === selected),
  );
  const caseTasks = data.task.filter((row) => row.case_id === selected);
  const depth = (row: Row) => {
    let parent = row.parent_task_id;
    let count = 0;
    const seen = new Set();
    while (parent && !seen.has(parent) && count < 8) {
      seen.add(parent);
      count++;
      parent = caseTasks.find(
        (item) => item.task_id === parent,
      )?.parent_task_id;
    }
    return count;
  };
  const takeOver = async () => {
    if (!current) return;
    setBusy(true);
    try {
      await mutate("update_case", {
        case_id: current.case_id,
        status:
          current.status === "human_owned" ? "in_progress" : "human_owned",
        reason:
          current.status === "human_owned"
            ? "Human returned case to agents"
            : "Human took ownership from dashboard",
      });
      notify(
        current.status === "human_owned"
          ? "Case returned to agents."
          : "Case marked human-owned. Agents must now assist only.",
      );
    } catch (error) {
      notify(String(error), true);
    } finally {
      setBusy(false);
    }
  };
  const events = newest(
    data.audit_event.filter((row) => row.case_id === selected),
  );
  const calls = newest(
    data.call.filter((row) => row.case_id === selected),
    "started_at",
  );
  return (
    <div className="stack">
      <div className="toolbar">
        <select
          aria-label="Select case"
          value={selected}
          onChange={(event) => {
            setSelected(event.target.value);
            setTaskId("");
          }}
        >
          <option value="">All cases</option>
          {newest(data.case, "opened_at").map((row) => (
            <option key={str(row.case_id)} value={str(row.case_id)}>
              {str(row.case_id)} · {str(row.title)}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by agent"
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
        {selected && (
          <button className="text-button" onClick={() => setSelected("")}>
            Clear case filter
          </button>
        )}
      </div>
      {current && (
        <Panel
          title={str(current.title)}
          eyebrow={str(current.case_id)}
          action={
            <div className="inline">
              <FlagButton
                onFlag={onFlag}
                target={{ ref: `case:${selected}`, caseId: selected }}
              />
              <button
                className="button"
                disabled={busy || state !== "live"}
                onClick={takeOver}
              >
                <Hand size={14} />
                {current.status === "human_owned"
                  ? "Return to agents"
                  : "Take over"}
              </button>
            </div>
          }
        >
          <div className="detail-grid padded">
            <Detail label="Status">
              <Badge value={current.status} />
            </Detail>
            <Detail label="Load">{str(current.load_id)}</Detail>
            <Detail label="Exposure">{money(current.exposure_usd)}</Detail>
            <Detail label="Priority">{str(current.priority)}</Detail>
            <Detail label="Opened">{date(current.opened_at)}</Detail>
            <Detail label="Resolved">
              {current.resolved_at ? date(current.resolved_at) : "In progress"}
            </Detail>
          </div>
        </Panel>
      )}
      {!selected && (
        <Panel title="Case register">
          <DataTable
            rows={newest(data.case.filter(matches), "opened_at")}
            table="case"
            onFlag={onFlag}
            onRow={(row) => setSelected(str(row.case_id))}
            columns={[
              { key: "case_id", title: "Case" },
              { key: "title" },
              statusColumn(),
              { key: "priority" },
              moneyColumn("exposure_usd", "Exposure"),
              timeColumn("opened_at", "Opened"),
            ]}
            emptyTitle="No cases yet"
            emptyText="The ingest service opens cases in SpacetimeDB. Each case brings its full task tree and evidence trail."
          />
        </Panel>
      )}
      <Panel
        title={selected ? "Task tree & live feed" : "Live task feed"}
        action={
          <span className="muted small">
            {tasks.length} tasks · newest first
          </span>
        }
      >
        <DataTable
          rows={tasks}
          table="task"
          onFlag={onFlag}
          onRow={(row) => setTaskId(str(row.task_id))}
          columns={[
            {
              key: "display_id",
              title: "Task",
              format: (value, row) => (
                <span style={{ paddingLeft: selected ? depth(row) * 14 : 0 }}>
                  {selected && row.parent_task_id ? "↳ " : ""}
                  {str(value, str(row.task_id))}
                </span>
              ),
            },
            { key: "case_id", title: "Case" },
            agentColumn,
            { key: "skill" },
            statusColumn("business_status"),
            { key: "model_used", title: "Model" },
            timeColumn("updated_at"),
          ]}
          emptyTitle="No matching tasks"
        />
      </Panel>
      {task && (
        <Panel
          title={str(task.display_id, str(task.task_id))}
          eyebrow={agentName(task.agent)}
          action={
            <button className="text-button" onClick={() => setTaskId("")}>
              Close details
            </button>
          }
        >
          <div className="padded stack">
            <div className="detail-grid">
              <Detail label="A2A state">
                <Badge value={task.a2a_state} />
              </Detail>
              <Detail label="Business status">
                <Badge value={task.business_status} />
              </Detail>
              <Detail label="Parent task">{str(task.parent_task_id)}</Detail>
              <Detail label="Model">{str(task.model_used)}</Detail>
              <Detail label="Confidence">
                {task.confidence === undefined
                  ? "—"
                  : `${(num(task.confidence) * 100).toFixed(0)}%`}
              </Detail>
              <Detail label="Outcome">{str(task.outcome)}</Detail>
            </div>
            <details open>
              <summary>Artifacts, evidence & runtime context</summary>
              <JsonDetail value={task} />
            </details>
            <DataTable
              rows={data.doc_read.filter((row) => row.task_id === task.task_id)}
              columns={[
                {
                  key: "doc_ref",
                  title: "Source",
                  format: (value) => (
                    <ExternalLink href={value}>{str(value)}</ExternalLink>
                  ),
                },
                { key: "page" },
                { key: "span" },
                agentColumn,
              ]}
              emptyTitle="No document reads recorded"
            />
          </div>
        </Panel>
      )}
      {selected && (
        <>
          <Panel
            title="Conversations & outreach"
            eyebrow="DEMO MODE · real contacts, team destinations"
          >
            {calls.length ? (
              calls.map((call) => (
                <div className="call-card" key={str(call.id)}>
                  <div className="panel-head">
                    <div className="inline">
                      <Phone size={16} />
                      <strong>{str(call.persona)}</strong>
                      <Badge value={call.status} />
                    </div>
                    <FlagButton
                      onFlag={onFlag}
                      target={{ ref: `call:${str(call.id)}`, caseId: selected }}
                    />
                  </div>
                  <div className="demo-banner">
                    Would contact{" "}
                    <strong>{str(call.intended_to, "Not recorded")}</strong>
                    <ArrowRight size={14} />
                    Routed to{" "}
                    <strong>{str(call.routed_to, "Not confirmed")}</strong>
                  </div>
                  <div className="detail-grid">
                    <Detail label="Started">{date(call.started_at)}</Detail>
                    <Detail label="Recording">
                      <ExternalLink href={call.recording_url}>
                        {call.recording_url
                          ? "Open recording"
                          : "Not available"}
                      </ExternalLink>
                    </Detail>
                  </div>
                  <details open>
                    <summary>Redacted transcript</summary>
                    <pre className="transcript">
                      {str(
                        call.transcript_redacted,
                        "The Voice Desk has not published a transcript yet.",
                      )}
                    </pre>
                  </details>
                  <details>
                    <summary>Structured findings</summary>
                    <JsonDetail value={json(call.findings_json, {})} />
                  </details>
                </div>
              ))
            ) : (
              <Empty title="No calls for this case">
                Calls appear with both the intended real contact and the routed
                team destination.
              </Empty>
            )}
          </Panel>
          <Panel title="Evidence & policy trail">
            <DataTable
              rows={events}
              table="audit_event"
              onFlag={onFlag}
              columns={[
                timeColumn(),
                agentColumn,
                { key: "kind" },
                { key: "summary" },
                { key: "policy_ids", title: "Policy" },
                { key: "evidence_refs", title: "Evidence" },
              ]}
              emptyTitle="No audit events yet"
            />
          </Panel>
        </>
      )}
    </div>
  );
}
export function LedgerView({ onFlag }: { onFlag: FlagHandler }) {
  const { data } = useLive();
  const [entry, setEntry] = useState<Row | null>(null);
  const activeHolds = data.hold.filter((row) =>
    ["active", "held"].includes(str(row.status)),
  );
  const cash = data.metric.find((row) =>
    ["cash_position", "cash_position_usd", "cash_balance"].includes(
      str(row.name),
    ),
  );
  return (
    <div className="stack">
      <div className="summary-grid three">
        <div className="summary-card">
          <span>Cash position</span>
          <strong>{cash ? money(cash.value) : "—"}</strong>
          <small>
            {cash
              ? `Updated ${date(cash.updated_at)}`
              : "Awaiting cash balance metric"}
          </small>
        </div>
        <div className="summary-card">
          <span>Active holds</span>
          <strong>
            {money(
              activeHolds.reduce(
                (sum, row) =>
                  sum + Math.max(0, num(row.amount) - num(row.released_amount)),
                0,
              ),
            )}
          </strong>
          <small>{activeHolds.length} invoices under control</small>
        </div>
        <div className="summary-card">
          <span>Payment proposals</span>
          <strong>
            {
              data.payment_proposal.filter(
                (row) => row.status === "proposed" || row.status === "pending",
              ).length
            }
          </strong>
          <small>Proposals recorded by Carrier AP</small>
        </div>
      </div>
      <Panel title="Holds" eyebrow="Enforced in transactional reducers">
        <DataTable
          rows={data.hold}
          table="hold"
          onFlag={onFlag}
          columns={[
            { key: "id", title: "Hold" },
            { key: "case_id", title: "Case" },
            { key: "invoice_id", title: "Invoice" },
            moneyColumn("amount", "Original amount"),
            {
              key: "released_amount",
              title: "Remaining",
              format: (value, row) =>
                money(Math.max(0, num(row.amount) - num(value))),
            },
            { key: "reason_code", title: "Reason" },
            statusColumn(),
            { key: "placed_by", title: "Placed by" },
            { key: "released_by", title: "Released by" },
          ]}
          emptyTitle="No holds recorded"
        />
      </Panel>
      <Panel
        title="General journal"
        action={
          <span className="subtle-badge">
            <ShieldCheck size={13} /> Balanced · open period · independent
            approval
          </span>
        }
      >
        <DataTable
          rows={newest(data.ledger_entry, "posted_at")}
          table="ledger_entry"
          onFlag={onFlag}
          onRow={setEntry}
          columns={[
            { key: "id", title: "Entry" },
            { key: "case_id", title: "Case" },
            { key: "period" },
            { key: "memo" },
            { key: "requested_by", title: "Requester" },
            { key: "approved_by", title: "Approver" },
            timeColumn("posted_at", "Posted"),
          ]}
          emptyTitle="No journal entries yet"
        />
      </Panel>
      {entry && (
        <Panel
          title={`Journal ${str(entry.id)}`}
          action={
            <button className="text-button" onClick={() => setEntry(null)}>
              Close
            </button>
          }
        >
          <DataTable
            rows={json<Row[]>(entry.lines ?? entry.lines_json, [])}
            columns={[
              { key: "account" },
              moneyColumn("debit", "Debit"),
              moneyColumn("credit", "Credit"),
            ]}
          />
        </Panel>
      )}
      <Panel title="Payment proposals">
        <DataTable
          rows={data.payment_proposal}
          table="payment_proposal"
          onFlag={onFlag}
          columns={[
            { key: "id" },
            { key: "case_id", title: "Case" },
            { key: "carrier_id", title: "Carrier" },
            moneyColumn(),
            statusColumn(),
            { key: "proposed_by", title: "Proposed by" },
          ]}
          emptyTitle="No proposed payments"
        />
      </Panel>
    </div>
  );
}
