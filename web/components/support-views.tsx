"use client";
import { useEffect, useState } from "react";
import {
  ArrowRight,
  Building2,
  Cpu,
  Mail,
  Phone,
  Play,
  SlidersHorizontal,
} from "lucide-react";
import { AGENTS, date, json, newest, num, str, type Row } from "@/lib/data";
import { useLive } from "./live-provider";
import { Mascot } from "./mascot";
import {
  Badge,
  DataTable,
  Detail,
  Empty,
  ExternalLink,
  FlagButton,
  JsonDetail,
  Panel,
  Submit,
  agentColumn,
  timeColumn,
  type FlagHandler,
} from "./ui";
import type { Notify } from "./operations";
import { DemoPhone } from "./demo-phone";
function AgentConfig({
  agent,
  models,
  config,
  notify,
}: {
  agent: (typeof AGENTS)[number];
  models: string[];
  config?: Row;
  notify: Notify;
}) {
  const { mutate, state } = useLive();
  const [busy, setBusy] = useState(false);
  const [custom, setCustom] = useState(false);
  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      await mutate("set_agent_model", {
        agent: agent.id,
        model: str(form.get("model"), "auto"),
        enabled: form.get("enabled") === "on",
      });
      notify(
        `${agent.name} configuration saved. The harness reads it on the next call.`,
      );
    } catch (error) {
      notify(String(error), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form
      className="model-row"
      onSubmit={save}
      key={`${config?.model}:${config?.enabled}`}
    >
      <Mascot id={agent.id} size={32} />
      <div className="model-name">
        <strong>{agent.name}</strong>
        <small>{agent.role}</small>
      </div>
      <label className="sr-only" htmlFor={`model-${agent.id}`}>
        Model for {agent.name}
      </label>
      {custom ? (
        <input
          id={`model-${agent.id}`}
          name="model"
          required
          placeholder="Exact harness model ID"
          defaultValue={str(config?.model, "")}
          maxLength={120}
        />
      ) : (
        <select
          id={`model-${agent.id}`}
          name="model"
          defaultValue={str(config?.model, "auto")}
        >
          <option value="auto">Auto · harness router</option>
          {models
            .filter((model) => model !== "auto")
            .map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
        </select>
      )}
      <button
        className="text-button small"
        type="button"
        onClick={() => setCustom((value) => !value)}
      >
        {custom ? "Known models" : "Custom ID"}
      </button>
      <label className="checkbox">
        <input
          type="checkbox"
          name="enabled"
          defaultChecked={config?.enabled !== false}
        />
        Enabled
      </label>
      <button className="button" disabled={busy || state !== "live"}>
        {busy ? "Saving…" : "Save"}
      </button>
    </form>
  );
}
export function HarnessView({
  notify,
  onFlag,
}: {
  notify: Notify;
  onFlag: FlagHandler;
}) {
  const { data } = useLive();
  const [agent, setAgent] = useState("");
  const models = [
    ...new Set([
      ...data.llm_call.map((row) => str(row.model, "")),
      ...data.agent_config.map((row) => str(row.model, "")),
    ]),
  ].filter(Boolean);
  const calls = newest(
    data.llm_call.filter((row) => !agent || row.agent === agent),
  );
  return (
    <div className="stack">
      <div className="notice">
        <Cpu size={20} />
        <div>
          <strong>Your models. Your routing rules.</strong>
          <p>
            Auto delegates selection to the ASUS harness. Pin an observed model
            or enter an exact model ID served by W1.
          </p>
        </div>
      </div>
      <Panel
        title="Model routing by agent"
        action={
          <span className="subtle-badge">
            <SlidersHorizontal size={13} /> Changes apply on the next call
          </span>
        }
      >
        {AGENTS.map((item) => (
          <AgentConfig
            key={item.id}
            agent={item}
            models={models}
            config={data.agent_config.find((row) => row.agent === item.id)}
            notify={notify}
          />
        ))}
      </Panel>
      <Panel
        title="Inference telemetry"
        action={
          <select
            aria-label="Filter LLM calls by agent"
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
        }
      >
        <DataTable
          rows={calls}
          table="llm_call"
          onFlag={onFlag}
          columns={[
            timeColumn(),
            agentColumn,
            { key: "model" },
            {
              key: "latency_ms",
              title: "Latency",
              format: (value) => `${num(value).toLocaleString()} ms`,
            },
            { key: "tokens" },
            { key: "route_reason", title: "Route reason" },
            {
              key: "ensemble_agreement",
              title: "Ensemble",
              format: (value) => (
                <Badge
                  value={
                    value === false
                      ? "disagreement"
                      : value === true
                        ? "agreement"
                        : "not_reported"
                  }
                />
              ),
            },
          ]}
          emptyTitle="No model calls recorded"
          emptyText="The harness publishes model, latency, token use and ensemble decisions after inference."
        />
      </Panel>
      <Panel title="Ensemble disagreements">
        <DataTable
          rows={calls.filter((row) => row.ensemble_agreement === false)}
          columns={[
            timeColumn(),
            agentColumn,
            { key: "model" },
            { key: "route_reason", title: "Reason" },
          ]}
          emptyTitle="No disagreements reported"
          emptyText="High-stakes disagreements require human judgment under P-009."
        />
      </Panel>
    </div>
  );
}
export function CounterpartiesView({
  query,
  onFlag,
}: {
  query: string;
  onFlag: FlagHandler;
}) {
  const { data } = useLive();
  const [selected, setSelected] = useState("");
  const [kind, setKind] = useState("");
  const companies = data.company.filter(
    (row) =>
      (!kind || row.kind === kind) &&
      (!query ||
        JSON.stringify(row).toLowerCase().includes(query.toLowerCase())),
  );
  const company = data.company.find((row) => row.id === selected);
  const contacts = data.contact.filter((row) => row.company_id === selected);
  return (
    <div className="stack">
      <div className="demo-banner">
        <Phone size={16} />
        <strong>OUTREACH: DEMO MODE</strong>
        <span>
          Real contacts are shown in full. Calls and emails are routed to
          allowlisted team destinations.
        </span>
      </div>
      <Panel
        title="Counterparty directory"
        action={
          <select
            aria-label="Filter company type"
            value={kind}
            onChange={(event) => setKind(event.target.value)}
          >
            <option value="">All counterparties</option>
            {[...new Set(data.company.map((row) => str(row.kind)))].map(
              (value) => (
                <option key={value}>{value}</option>
              ),
            )}
          </select>
        }
      >
        <DataTable
          rows={companies}
          table="company"
          onFlag={onFlag}
          onRow={(row) => setSelected(str(row.id))}
          columns={[
            { key: "name", title: "Company" },
            { key: "kind", title: "Type" },
            { key: "usdot", title: "USDOT" },
            { key: "mc", title: "MC" },
            { key: "phone", title: "Registered phone" },
            {
              key: "source_url",
              title: "Source",
              format: (value) => (
                <ExternalLink href={value}>
                  {value ? "View source" : "Not recorded"}
                </ExternalLink>
              ),
            },
          ]}
          emptyTitle="Waiting for real counterparties"
          emptyText="W4's enrichment pipeline supplies company and contact records, with original sources and confidence."
        />
      </Panel>
      {company && (
        <Panel
          title={str(company.name)}
          eyebrow="Real-world counterparty"
          action={
            <button className="text-button" onClick={() => setSelected("")}>
              Close company
            </button>
          }
        >
          <div className="padded stack">
            <div className="company-header">
              <span className="company-icon">
                <Building2 size={30} />
              </span>
              <div className="detail-grid">
                <Detail label="USDOT / MC">
                  {str(company.usdot)} / {str(company.mc)}
                </Detail>
                <Detail label="Registered phone">{str(company.phone)}</Detail>
                <Detail label="Website">
                  <ExternalLink href={company.website}>
                    {str(company.website)}
                  </ExternalLink>
                </Detail>
                <Detail label="Authority status">
                  {str(company.authority_status, "Not supplied by source")}
                </Detail>
                <Detail label="Source">
                  <ExternalLink href={company.source_url}>
                    Original record
                  </ExternalLink>
                </Detail>
                <Detail label="Last scraped">{date(company.scraped_at)}</Detail>
              </div>
            </div>
            <h3>Enriched contacts</h3>
            {contacts.length ? (
              <div className="contact-grid">
                {contacts.map((contact) => (
                  <article className="contact-card" key={str(contact.id)}>
                    <div className="panel-head">
                      <div>
                        <strong>{str(contact.name)}</strong>
                        <p>{str(contact.title)}</p>
                      </div>
                      <FlagButton
                        onFlag={onFlag}
                        target={{ ref: `contact:${str(contact.id)}` }}
                      />
                    </div>
                    <div className="contact-line">
                      <Phone size={14} />
                      {str(contact.phone)}
                    </div>
                    <div className="contact-line">
                      <Mail size={14} />
                      {str(contact.email)}
                    </div>
                    <div className="source-confidence">
                      <ExternalLink href={contact.source_url}>
                        {str(contact.source)}
                      </ExternalLink>
                      <span>
                        {(num(contact.confidence) * 100).toFixed(0)}% confidence
                      </span>
                    </div>
                    {contact.linkedin_url ? (
                      <ExternalLink href={contact.linkedin_url}>
                        Professional profile
                      </ExternalLink>
                    ) : null}
                    <details>
                      <summary>Alternate values & conflicts</summary>
                      <JsonDetail value={json(contact.alternates_json, [])} />
                    </details>
                  </article>
                ))}
              </div>
            ) : (
              <Empty title="No enriched contacts yet">
                The directory record remains available while the contact
                waterfall runs.
              </Empty>
            )}
          </div>
        </Panel>
      )}
    </div>
  );
}
type DemoConfig = {
  enabled: boolean;
  mode: string;
  targets: { phone: string[]; email: string[] };
};
export function DemoView({ notify }: { notify: Notify }) {
  const { data } = useLive();
  const [config, setConfig] = useState<DemoConfig | null>(null);
  const [configError, setConfigError] = useState("");
  const [kind, setKind] = useState("phone");
  const [channel, setChannel] = useState<"phone" | "email">("phone");
  const [caseId, setCaseId] = useState("");
  const [target, setTarget] = useState("");
  const [scenario, setScenario] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Row | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/demo", { signal: controller.signal })
      .then((response) => response.json())
      .then(setConfig)
      .catch((error) => {
        if (!controller.signal.aborted) setConfigError(String(error));
      });
    return () => controller.abort();
  }, []);
  const targets = config?.targets[channel] ?? [];
  const chosenTarget = targets.includes(target) ? target : (targets[0] ?? "");
  const start = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setResult(null);
    try {
      const response = await fetch("/api/demo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind,
          channel,
          case_id: caseId,
          scenario,
          demo_target: chosenTarget,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error);
      setResult(body);
      notify(str(body.message), Boolean(body.stub));
    } catch (error) {
      notify(String(error), true);
    } finally {
      setBusy(false);
    }
  };
  const presets = [
    {
      id: "phone",
      name: "Detention charge",
      label: "PHONE",
      text: "Investigate a carrier detention claim.",
      channel: "phone" as const,
    },
    {
      id: "bank_change",
      name: "Bank change",
      label: "EMAIL",
      text: "Verify a requested payment destination.",
      channel: "email" as const,
    },
    {
      id: "short_pay",
      name: "Short payment",
      label: "COLLECTIONS",
      text: "Resolve an underpaid shipper invoice.",
      channel: "phone" as const,
    },
    {
      id: "scenario",
      name: "Your scenario",
      label: "CUSTOM",
      text: "Let a judge supply the next case.",
      channel: "phone" as const,
    },
  ];
  return (
    <div className="stack">
      <div className="demo-intro">
        <span className="eyebrow">The moment it becomes real</span>
        <h2>Put the back office to work.</h2>
        <p>
          Call your team phone and follow the agents from first handoff to final
          decision.
        </p>
      </div>
      <DemoPhone />
      {configError ? (
        <p className="notice warning">{configError}</p>
      ) : !config?.enabled ? (
        <div className="notice warning">
          <div>
            <strong>Additional scenarios are not connected yet.</strong>
            <p>
              Use Call my phone above for the live phone demo.
            </p>
          </div>
        </div>
      ) : (
        <div className="demo-banner">
          <strong>DEMO MODE</strong> All outreach is routed to the team
          allowlist. Confirmed calls appear in the case timeline.
        </div>
      )}
      <div className="preset-grid">
        {presets.map((preset) => (
          <button
            className={`preset ${kind === preset.id ? "selected" : ""}`}
            key={preset.id}
            disabled={!config?.enabled}
            onClick={() => {
              setKind(preset.id);
              setChannel(preset.channel);
            }}
          >
            <span>{preset.label}</span>
            <strong>{preset.name}</strong>
            <p>{preset.text}</p>
            <ArrowRight size={17} />
          </button>
        ))}
      </div>
      <Panel title="Set the scene">
        <form className="demo-form" onSubmit={start}>
          <div className="form-grid">
            <label>
              Case context
              <select
                value={caseId}
                onChange={(event) => setCaseId(event.target.value)}
              >
                <option value="">Use the preset’s case</option>
                {data.case.map((row) => (
                  <option value={str(row.case_id)} key={str(row.case_id)}>
                    {str(row.case_id)} · {str(row.title)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Channel
              <select
                value={channel}
                onChange={(event) =>
                  setChannel(event.target.value as "phone" | "email")
                }
              >
                <option value="phone">Phone call</option>
                <option value="email">Email</option>
              </select>
            </label>
            <label className="full">
              Teammate playing the counterparty
              <select
                required
                value={chosenTarget}
                onChange={(event) => setTarget(event.target.value)}
              >
                <option value="">Select an allowlisted team destination</option>
                {targets.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
              <small className="muted">
                Only destinations configured by the operator are available.
              </small>
            </label>
            {kind === "scenario" && (
              <label className="full">
                Judge’s scenario
                <textarea
                  required
                  minLength={10}
                  maxLength={4000}
                  rows={5}
                  placeholder="Describe a freight finance situation, the conflicting evidence, and what you want the team to investigate…"
                  value={scenario}
                  onChange={(event) => setScenario(event.target.value)}
                />
              </label>
            )}
          </div>
          <div className="form-footer">
            <span className="muted small">
              {channel === "phone"
                ? "A real call may ring the selected team phone."
                : "A real email may be sent to the selected team inbox."}
            </span>
            <Submit busy={busy} disabled={!config?.enabled || !chosenTarget}>
              <Play size={15} /> Start scenario
            </Submit>
          </div>
        </form>
      </Panel>
      {result && (
        <Panel
          title={result.stub ? "Gateway stub response" : "Gateway response"}
        >
          <div className="padded">
            <p>{str(result.message)}</p>
            <JsonDetail value={result} />
          </div>
        </Panel>
      )}
    </div>
  );
}
