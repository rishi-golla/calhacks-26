"use client";
import { useMemo, type ReactNode } from "react";
import {
  date,
  money,
  newest,
  num,
  str,
  timestamp,
  type Row,
  type Snapshot,
} from "@/lib/data";
import { LiveProvider, useLive } from "./live-provider";
import {
  feedOf,
  rows,
  scope,
  stepById,
  type Line,
  type Step,
} from "./workflow-steps";
import "./workflow.css";
import "./node-view.css";

type PageProps = { data: Snapshot; caseId: string };

function field(value: unknown) {
  return value === undefined || value === null || value === ""
    ? "Not recorded"
    : str(value);
}

function stamp(value: unknown) {
  return timestamp(value) ? date(value) : "Not recorded";
}

function relativeAge(value: unknown) {
  const time = timestamp(value);
  if (!time) return "Time not recorded";
  const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
  if (seconds < 10) return "now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function currency(value: unknown) {
  return value === undefined || value === null || value === ""
    ? "Not recorded"
    : money(value);
}

function jsonText(value: unknown) {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value !== "string") return JSON.stringify(value, null, 2);
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function JsonBlock({ value }: { value: unknown }) {
  const text = jsonText(value);
  return text ? <pre className="nv-json">{text}</pre> : null;
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="nv-empty">{children}</div>;
}

function Status({ value, label }: { value: unknown; label?: string }) {
  const text = label ?? field(value);
  const normalized = text.toLowerCase();
  const tone = /pending|hold|active/.test(normalized)
    ? "warning"
    : /approved|paid|posted|resolved|complete|closed/.test(normalized)
      ? "success"
      : /rejected|failed|error/.test(normalized)
        ? "danger"
        : "neutral";
  return <span className={`nv-status ${tone}`}>{text}</span>;
}

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="nv-field">
      <span>{label}</span>
      <strong>{value || "Not recorded"}</strong>
    </div>
  );
}

function Section({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children: ReactNode;
}) {
  return (
    <section className="nv-section">
      <div className="nv-section-title">
        <h2>{title}</h2>
        {count !== undefined && <span>{count}</span>}
      </div>
      {children}
    </section>
  );
}

function InvoicePage({ data }: PageProps) {
  const invoices = newest(rows(data, "invoice"), "issued_at");
  if (!invoices.length) return <Empty>No invoice for this case yet</Empty>;
  const invoice = invoices[0];
  const carrier = rows(data, "company").find(
    (company) => str(company.id, "") === str(invoice.carrier_id, ""),
  );
  const mismatch =
    Math.abs(num(invoice.amount) - num(invoice.contract_amount)) > 0.01;
  const carrierFacts = [
    carrier?.usdot ? `USDOT ${field(carrier.usdot)}` : "",
    carrier?.mc ? `MC ${field(carrier.mc)}` : "",
  ].filter(Boolean);

  return (
    <div className="nv-document-frame">
      <div className="nv-filebar">
        <span className="nv-filemark">PDF</span>
        <strong>{field(invoice.id)}.pdf</strong>
        <Status value={invoice.status} />
      </div>
      <div className="nv-pdf-viewer">
        <article className="nv-sheet">
          <div className="nv-doc-head">
            <div>
              <span className="nv-eyebrow">INVOICE</span>
              <h2>
                {carrier ? field(carrier.name) : "Carrier name not recorded"}
              </h2>
              {carrierFacts.length > 0 && (
                <p className="nv-muted">{carrierFacts.join(" · ")}</p>
              )}
            </div>
            <div className="nv-doc-id">
              <strong>{field(invoice.id)}</strong>
              <span>{stamp(invoice.issued_at)}</span>
            </div>
          </div>
          <div className="nv-doc-strip">
            <Field label="Invoice ID" value={field(invoice.id)} />
            <Field label="Issued" value={stamp(invoice.issued_at)} />
            <Field label="Load ID" value={field(invoice.load_id)} />
          </div>
          <table className="nv-table">
            <thead>
              <tr>
                <th>Line item</th>
                <th>Amount</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Contract amount</td>
                <td>{currency(invoice.contract_amount)}</td>
              </tr>
              <tr>
                <td>Invoiced amount</td>
                <td>
                  {mismatch ? (
                    <mark className="nv-mark">{currency(invoice.amount)}</mark>
                  ) : (
                    currency(invoice.amount)
                  )}
                </td>
              </tr>
              <tr>
                <td>Paid amount</td>
                <td>{currency(invoice.paid_amount)}</td>
              </tr>
            </tbody>
          </table>
          <div className="nv-doc-status">
            <span>Invoice status</span>
            <Status value={invoice.status} />
          </div>
        </article>
      </div>
    </div>
  );
}

type InboxItem = {
  key: string;
  title: string;
  kind: "Document" | "Ingest event";
  time: unknown;
  row: Row;
  relatedTask?: Row;
};

function InboxPage({ data }: PageProps) {
  const tasks = new Map(
    rows(data, "task").map((task) => [str(task.task_id, ""), task]),
  );
  const docs: InboxItem[] = rows(data, "doc_read").map((row) => {
    const task = tasks.get(str(row.task_id, ""));
    return {
      key: `doc:${field(row.id)}`,
      title: field(row.doc_ref),
      kind: "Document",
      time: task?.updated_at || task?.started_at,
      row,
      relatedTask: task,
    };
  });
  const events: InboxItem[] = rows(data, "audit_event")
    .filter((row) => row.agent === "ingest")
    .map((row) => ({
      key: `event:${field(row.id)}`,
      title: field(row.summary || row.kind),
      kind: "Ingest event",
      time: row.ts,
      row,
    }));
  const items = [...docs, ...events].sort(
    (a, b) => timestamp(b.time) - timestamp(a.time),
  );
  if (!items.length)
    return <Empty>No documents or ingest events for this case yet</Empty>;

  const selected = items[0];
  const isDocument = selected.kind === "Document";
  const document = isDocument ? selected.row : undefined;
  const audit = !isDocument ? selected.row : undefined;
  return (
    <div className="nv-mail">
      <aside className="nv-mail-list">
        <div className="nv-mail-list-head">
          <strong>Inbox</strong>
          <span>{items.length}</span>
        </div>
        {items.map((item, index) => (
          <div
            className={`nv-mail-item ${index === 0 ? "selected" : ""}`}
            key={item.key}
          >
            <strong>{item.title}</strong>
            <span>{item.kind}</span>
            <time>{stamp(item.time)}</time>
          </div>
        ))}
      </aside>
      <article className="nv-mail-detail">
        <div className="nv-mail-detail-meta">
          <span>{selected.kind}</span>
          <time>{stamp(selected.time)}</time>
        </div>
        <h2>{selected.title}</h2>
        {document && (
          <>
            <p className="nv-mail-span">{field(document.span)}</p>
            <div className="nv-mail-fields">
              <Field label="Document" value={field(document.doc_ref)} />
              <Field label="Page" value={field(document.page)} />
              {selected.relatedTask && (
                <Field
                  label="Task"
                  value={field(selected.relatedTask.task_id)}
                />
              )}
            </div>
            <JsonBlock value={document.extracted_json} />
          </>
        )}
        {audit && (
          <>
            <p className="nv-mail-span">{field(audit.summary || audit.kind)}</p>
            <div className="nv-mail-fields">
              <Field label="Event" value={field(audit.kind)} />
              <Field label="Reason" value={field(audit.reason_code)} />
              <Field label="Agent" value={field(audit.agent)} />
            </div>
            <Field label="Task" value={field(audit.task_id)} />
            <Field
              label="Evidence"
              value={
                Array.isArray(audit.evidence_refs)
                  ? audit.evidence_refs.map((value) => str(value)).join(", ")
                  : field(audit.evidence_refs)
              }
            />
            <Field
              label="Policies"
              value={
                Array.isArray(audit.policy_ids)
                  ? audit.policy_ids.map((value) => str(value)).join(", ")
                  : field(audit.policy_ids)
              }
            />
          </>
        )}
      </article>
    </div>
  );
}

function RoutingPage({ data }: PageProps) {
  const routes = rows(data, "routing_outbox");
  const events = newest(rows(data, "audit_event"), "ts").filter((event) => {
    const description = [event.kind, event.reason_code, event.summary]
      .map((value) => str(value, ""))
      .join(" ");
    const hasRoute = routes.some(
      (route) =>
        Boolean(event.task_id) &&
        str(route.task_id, "") === str(event.task_id, ""),
    );
    return hasRoute || /route|redact|dispatch/i.test(description);
  });
  if (!events.length) return <Empty>No routed events for this case yet</Empty>;
  return (
    <div className="nv-panel nv-scroll">
      <Section title="Routed events" count={events.length}>
        <div className="nv-route-list">
          {events.map((event) => {
            const route = routes.find(
              (item) =>
                Boolean(event.task_id) &&
                str(item.task_id, "") === str(event.task_id, ""),
            );
            const targetAgent = route?.target_agent;
            const description = [event.kind, event.reason_code, event.summary]
              .map((value) => str(value, ""))
              .join(" ");
            const redacted = /redact/i.test(description);
            return (
              <article className="nv-route-item" key={field(event.id)}>
                <div className="nv-route-top">
                  <div>
                    <span className="nv-eyebrow">{field(event.kind)}</span>
                    <time>{stamp(event.ts)}</time>
                  </div>
                  <div className="nv-tags">
                    {redacted && <span className="nv-tag">Redacted</span>}
                    {targetAgent !== undefined &&
                      targetAgent !== null &&
                      targetAgent !== "" && (
                        <span className="nv-tag">
                          Routed to {field(targetAgent)}
                        </span>
                      )}
                  </div>
                </div>
                <h3>
                  {field(event.summary || event.reason_code || event.kind)}
                </h3>
                <p>{field(event.reason_code)}</p>
                <div className="nv-route-meta">
                  <span>Agent · {field(event.agent)}</span>
                  <span>Task · {field(event.task_id)}</span>
                </div>
              </article>
            );
          })}
        </div>
      </Section>
    </div>
  );
}

function RiskPage({ data }: PageProps) {
  const flags = newest(rows(data, "flag"), "ts");
  const holds = newest(rows(data, "hold"), "placed_at");
  if (!flags.length && !holds.length)
    return <Empty>No risk flags or holds for this case yet</Empty>;
  return (
    <div className="nv-panel nv-scroll">
      {holds.length > 0 && (
        <Section title="Payment holds" count={holds.length}>
          <div className="nv-risk-list">
            {holds.map((hold) => {
              const active = ["active", "on_hold"].includes(
                field(hold.status).toLowerCase(),
              );
              return (
                <article
                  className={`nv-risk-card ${active ? "active" : ""}`}
                  key={field(hold.id)}
                >
                  <div className="nv-card-head">
                    <div>
                      <span className="nv-eyebrow">
                        {field(hold.invoice_id)}
                      </span>
                      <h3>{field(hold.reason_code)}</h3>
                    </div>
                    <Status value={hold.status} />
                  </div>
                  <div className="nv-risk-meta">
                    <Field label="Amount" value={currency(hold.amount)} />
                    <Field label="Placed" value={stamp(hold.placed_at)} />
                    <Field label="Placed by" value={field(hold.placed_by)} />
                  </div>
                  {active && (
                    <div className="nv-note">
                      Active hold · {field(hold.reason_code)} ·{" "}
                      {currency(hold.amount)}
                    </div>
                  )}
                  {Boolean(hold.released_at) && (
                    <p className="nv-muted">
                      Released {stamp(hold.released_at)} ·{" "}
                      {currency(hold.released_amount)}
                    </p>
                  )}
                </article>
              );
            })}
          </div>
        </Section>
      )}
      {flags.length > 0 && (
        <Section title="Flags" count={flags.length}>
          <div className="nv-risk-list">
            {flags.map((flag) => (
              <article className="nv-risk-card" key={field(flag.id)}>
                <div className="nv-card-head">
                  <div>
                    <span className="nv-eyebrow">{field(flag.target_ref)}</span>
                    <h3>{field(flag.kind)}</h3>
                  </div>
                  <Status value={flag.status} />
                </div>
                <p>{field(flag.note)}</p>
                <div className="nv-route-meta">
                  <span>Raised by · {field(flag.raised_by)}</span>
                  <time>{stamp(flag.ts)}</time>
                </div>
              </article>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

function companyIds(data: Snapshot) {
  return new Set(
    [
      ...rows(data, "invoice").map((row) => row.carrier_id),
      ...rows(data, "payment_proposal").map((row) => row.carrier_id),
      ...rows(data, "call").map((row) => row.company_id),
    ]
      .map((value) => str(value, ""))
      .filter(Boolean),
  );
}

function ContactPage({
  data,
  verification,
}: PageProps & { verification: boolean }) {
  const ids = companyIds(data);
  const companies = rows(data, "company").filter((company) =>
    ids.has(str(company.id, "")),
  );
  const contacts = rows(data, "contact").filter((contact) =>
    ids.has(str(contact.company_id, "")),
  );
  if (!companies.length && !contacts.length)
    return <Empty>No company or contact details for this case yet</Empty>;
  return (
    <div className="nv-panel nv-scroll">
      <Section
        title={verification ? "Company verification" : "Company contacts"}
        count={companies.length}
      >
        <div className="nv-profile-list">
          {companies.map((company) => {
            const related = contacts.filter(
              (contact) => str(contact.company_id, "") === str(company.id, ""),
            );
            return (
              <article className="nv-profile-card" key={field(company.id)}>
                <div className="nv-profile-head">
                  <div>
                    <span className="nv-eyebrow">
                      {field(company.kind)}
                      {verification && company.usdot ? " · SAFER record" : ""}
                    </span>
                    <h3>{field(company.name)}</h3>
                  </div>
                  {Boolean(company.usdot) && (
                    <span className="nv-profile-id">
                      USDOT {field(company.usdot)}
                    </span>
                  )}
                </div>
                <div className="nv-profile-grid">
                  <Field label="MC" value={field(company.mc)} />
                  <Field label="Phone" value={field(company.phone)} />
                  <Field label="Website" value={field(company.website)} />
                  <Field label="Address" value="Not recorded" />
                </div>
                {related.length > 0 && (
                  <div className="nv-contact-list">
                    {related.map((contact) => (
                      <div className="nv-contact-card" key={field(contact.id)}>
                        <div>
                          <strong>{field(contact.name)}</strong>
                          <span>{field(contact.title)}</span>
                        </div>
                        <Field label="Phone" value={field(contact.phone)} />
                        <Field label="Email" value={field(contact.email)} />
                        {Boolean(contact.linkedin_url) && (
                          <Field
                            label="LinkedIn"
                            value={field(contact.linkedin_url)}
                          />
                        )}
                      </div>
                    ))}
                  </div>
                )}
                {Boolean(company.source_url) && (
                  <p className="nv-source">
                    Source · {field(company.source_url)}
                  </p>
                )}
              </article>
            );
          })}
        </div>
      </Section>
    </div>
  );
}

function duration(call: Row) {
  const start = timestamp(call.started_at);
  const end = timestamp(call.ended_at);
  if (!start) return "Not recorded";
  if (!end) return "In progress";
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(
    seconds % 60,
  ).padStart(2, "0")}`;
}

function CallPage({ data }: PageProps) {
  const calls = newest(rows(data, "call"), "started_at");
  if (!calls.length) return <Empty>No calls for this case yet</Empty>;
  const companies = rows(data, "company");
  const contacts = rows(data, "contact");
  return (
    <div className="nv-panel nv-scroll">
      <Section title="Call activity" count={calls.length}>
        <div className="nv-call-list">
          {calls.map((call) => {
            const company = companies.find(
              (row) => str(row.id, "") === str(call.company_id, ""),
            );
            const contact = contacts.find(
              (row) => str(row.id, "") === str(call.contact_id, ""),
            );
            return (
              <article className="nv-call-card" key={field(call.id)}>
                <div className="nv-card-head">
                  <div>
                    <span className="nv-eyebrow">
                      {field(call.direction)} · {field(call.persona)}
                    </span>
                    <h3>
                      {contact
                        ? field(contact.name)
                        : company
                          ? field(company.name)
                          : field(call.persona)}
                    </h3>
                  </div>
                  <Status value={call.status} />
                </div>
                <div className="nv-call-grid">
                  <Field label="Intended to" value={field(call.intended_to)} />
                  <Field label="Routed to" value={field(call.routed_to)} />
                  <Field label="Started" value={stamp(call.started_at)} />
                  <Field label="Ended" value={stamp(call.ended_at)} />
                  <Field label="Duration" value={duration(call)} />
                  <Field
                    label="Disclosure"
                    value={call.disclosure_given ? "Given" : "Not given"}
                  />
                </div>
                {Boolean(call.transcript_redacted) && (
                  <blockquote className="nv-transcript">
                    {field(call.transcript_redacted)}
                  </blockquote>
                )}
                {Boolean(call.findings_json) && (
                  <div className="nv-call-findings">
                    <span className="nv-eyebrow">Call findings</span>
                    <JsonBlock value={call.findings_json} />
                  </div>
                )}
              </article>
            );
          })}
        </div>
      </Section>
    </div>
  );
}

function ledgerLines(value: unknown): Row[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is Row =>
          typeof item === "object" && item !== null && !Array.isArray(item),
      )
    : [];
}

function PaymentPage({ data }: PageProps) {
  const proposals = newest(rows(data, "payment_proposal"), "proposed_at");
  const invoices = newest(rows(data, "invoice"), "issued_at");
  const ledger = newest(rows(data, "ledger_entry"), "posted_at");
  const holds = newest(rows(data, "hold"), "placed_at");
  if (!proposals.length && !invoices.length && !ledger.length && !holds.length)
    return <Empty>No payment activity for this case yet</Empty>;

  const proposal = proposals[0];
  const invoice =
    invoices.find(
      (row) => proposal && str(row.id, "") === str(proposal.invoice_id, ""),
    ) ?? invoices[0];
  const carrierId = proposal?.carrier_id ?? invoice?.carrier_id;
  const payee = rows(data, "company").find(
    (company) => str(company.id, "") === str(carrierId, ""),
  );
  const activeHold = holds.find((hold) =>
    ["active", "on_hold"].includes(field(hold.status).toLowerCase()),
  );
  const latestHold = activeHold ?? holds[0];
  const amount = proposal?.amount ?? invoice?.amount;
  return (
    <div className="nv-panel nv-scroll">
      <div className="nv-pay-card">
        <div className="nv-pay-head">
          <div>
            <span className="nv-eyebrow">PAYMENT</span>
            <h2>{payee ? field(payee.name) : "Payment details"}</h2>
          </div>
          <Status
            value={latestHold?.status}
            label={
              activeHold
                ? "On hold"
                : field(proposal?.status || invoice?.status)
            }
          />
        </div>
        <div className="nv-pay-fields">
          <Field
            label="Payee"
            value={payee ? field(payee.name) : "Not recorded"}
          />
          <Field label="Amount" value={currency(amount)} />
          <Field label="To account" value="Not recorded" />
          <Field
            label="Invoice"
            value={field(proposal?.invoice_id || invoice?.id)}
          />
        </div>
        {latestHold && (
          <div className={`nv-note ${activeHold ? "" : "quiet"}`}>
            <strong>{activeHold ? "On hold" : "Hold status"}</strong>
            <span>
              {field(latestHold.reason_code)} · {currency(latestHold.amount)}
            </span>
          </div>
        )}
      </div>
      {proposals.length > 0 && (
        <Section title="Payment proposals" count={proposals.length}>
          <div className="nv-transaction-list">
            {proposals.map((row) => (
              <article className="nv-transaction" key={field(row.id)}>
                <div>
                  <strong>{field(row.id)}</strong>
                  <span>{field(row.invoice_id)}</span>
                </div>
                <div>
                  <b>{currency(row.amount)}</b>
                  <Status value={row.status} />
                </div>
                <p>
                  Proposed by {field(row.proposed_by)} ·{" "}
                  {stamp(row.proposed_at)}
                </p>
              </article>
            ))}
          </div>
        </Section>
      )}
      {ledger.length > 0 && (
        <Section title="Ledger entries" count={ledger.length}>
          <div className="nv-transaction-list">
            {ledger.map((entry) => (
              <article className="nv-transaction" key={field(entry.id)}>
                <div>
                  <strong>{field(entry.memo)}</strong>
                  <span>
                    {field(entry.period)} · {stamp(entry.posted_at)}
                  </span>
                </div>
                <div className="nv-journal-lines">
                  {ledgerLines(entry.lines).map((line, index) => (
                    <span key={`${field(line.account)}:${index}`}>
                      {field(line.account)} · Dr {currency(line.debit)} / Cr{" "}
                      {currency(line.credit)}
                    </span>
                  ))}
                </div>
              </article>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

function ForecastPage({ data }: PageProps) {
  const forecasts = newest(rows(data, "cash_forecast"), "updated_at");
  if (!forecasts.length)
    return <Empty>No cash forecast for this case yet</Empty>;
  const max = Math.max(1, ...forecasts.map((row) => Math.abs(num(row.amount))));
  return (
    <div className="nv-panel nv-scroll">
      <Section title="Cash forecast" count={forecasts.length}>
        <div className="nv-forecast">
          {forecasts.map((row) => (
            <article className="nv-forecast-row" key={field(row.id)}>
              <div className="nv-forecast-labels">
                <strong>{field(row.period)}</strong>
                <span>{field(row.category)}</span>
                <b>{currency(row.amount)}</b>
              </div>
              <div className="nv-bar-track">
                <i
                  className={num(row.amount) < 0 ? "negative" : ""}
                  style={{
                    width: `${Math.max(
                      3,
                      (Math.abs(num(row.amount)) / max) * 100,
                    )}%`,
                  }}
                />
              </div>
              <div className="nv-forecast-foot">
                <span>
                  Confidence · {Math.round(num(row.confidence) * 100)}%
                </span>
                <time>{stamp(row.updated_at)}</time>
              </div>
              {Boolean(row.assumptions_json) && (
                <JsonBlock value={row.assumptions_json} />
              )}
            </article>
          ))}
        </div>
      </Section>
    </div>
  );
}

type AuditItem = {
  id: string;
  hash: unknown;
  message: unknown;
  agent: unknown;
  time: unknown;
  kind: "change" | "event";
  events?: Row[];
};

function AuditPage({ data }: PageProps) {
  const changes = rows(data, "change_set");
  const auditEvents = rows(data, "audit_event");
  if (!changes.length && !auditEvents.length)
    return <Empty>No audit changes for this case yet</Empty>;
  const changeIds = new Set(changes.map((row) => str(row.id, "")));
  const items: AuditItem[] = [
    ...changes.map((change) => ({
      id: `change:${field(change.id)}`,
      hash: change.hash,
      message: change.summary || change.action,
      agent: change.agent,
      time: change.ts,
      kind: "change" as const,
      events: auditEvents.filter(
        (event) => str(event.change_set_id, "") === str(change.id, ""),
      ),
    })),
    ...auditEvents
      .filter(
        (event) =>
          !event.change_set_id || !changeIds.has(str(event.change_set_id, "")),
      )
      .map((event) => ({
        id: `event:${field(event.id)}`,
        hash: event.hash,
        message: event.summary || event.kind,
        agent: event.agent,
        time: event.ts,
        kind: "event" as const,
      })),
  ].sort((a, b) => timestamp(b.time) - timestamp(a.time));

  return (
    <div className="nv-panel nv-scroll">
      <Section title="Audit history" count={items.length}>
        <div className="nv-audit-list">
          {items.map((item) => {
            const hash = field(item.hash);
            return (
              <article className="nv-audit-item" key={item.id}>
                <span className="nv-audit-mark" />
                <div className="nv-audit-main">
                  <div className="nv-audit-title">
                    <span className="nv-hash">
                      {hash === "Not recorded" ? hash : hash.slice(0, 7)}
                    </span>
                    <Status
                      value={item.kind}
                      label={item.kind === "change" ? "Change set" : "Event"}
                    />
                    <time>{relativeAge(item.time)}</time>
                  </div>
                  <h3>{field(item.message)}</h3>
                  <p>
                    {field(item.agent)} · {stamp(item.time)}
                  </p>
                  {item.events && item.events.length > 0 && (
                    <div className="nv-audit-events">
                      {item.events.map((event) => (
                        <span key={field(event.id)}>
                          {field(event.kind)} · {field(event.summary)}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      </Section>
    </div>
  );
}

function ApprovalPage({ data }: PageProps) {
  const approvals = [...rows(data, "approval")].sort(
    (a, b) =>
      timestamp(b.decided_at || b.expires_at) -
      timestamp(a.decided_at || a.expires_at),
  );
  if (!approvals.length) return <Empty>No approvals for this case yet</Empty>;
  return (
    <div className="nv-panel nv-scroll">
      <Section title="Approval requests" count={approvals.length}>
        <div className="nv-approval-list">
          {approvals.map((approval) => {
            const decision =
              field(approval.decision) === "Not recorded"
                ? "Pending"
                : field(approval.decision);
            return (
              <article className="nv-approval-card" key={field(approval.id)}>
                <div className="nv-card-head">
                  <div>
                    <span className="nv-eyebrow">{field(approval.id)}</span>
                    <h3>{field(approval.action)}</h3>
                  </div>
                  <Status value={decision} />
                </div>
                <div className="nv-approval-grid">
                  <Field label="Amount" value={currency(approval.amount)} />
                  <Field
                    label="Requested by"
                    value={field(approval.requested_by)}
                  />
                  <Field label="Target" value={field(approval.target_id)} />
                  <Field label="Invoice" value={field(approval.invoice_id)} />
                  <Field label="Expires" value={stamp(approval.expires_at)} />
                  <Field
                    label="Decision time"
                    value={stamp(approval.decided_at)}
                  />
                </div>
              </article>
            );
          })}
        </div>
      </Section>
    </div>
  );
}

function ReviewPage({ data }: PageProps) {
  const reviews = newest(rows(data, "review"), "ts");
  if (!reviews.length) return <Empty>No reviews for this case yet</Empty>;
  return (
    <div className="nv-panel nv-scroll">
      <Section title="Quality reviews" count={reviews.length}>
        <div className="nv-review-list">
          {reviews.map((review) => (
            <article
              className="nv-review-card"
              key={field(review.target_task_id)}
            >
              <div className="nv-card-head">
                <div>
                  <span className="nv-eyebrow">
                    {field(review.target_task_id)}
                  </span>
                  <h3>Review result</h3>
                </div>
                <Status value={review.verdict} />
              </div>
              <div className="nv-review-meta">
                <span>{stamp(review.ts)}</span>
              </div>
              <div className="nv-review-json">
                {Boolean(review.checks_json) && (
                  <div>
                    <span className="nv-eyebrow">Checks</span>
                    <JsonBlock value={review.checks_json} />
                  </div>
                )}
                {Boolean(review.friction_json) && (
                  <div>
                    <span className="nv-eyebrow">Friction</span>
                    <JsonBlock value={review.friction_json} />
                  </div>
                )}
                {Boolean(review.actions_json) && (
                  <div>
                    <span className="nv-eyebrow">Actions</span>
                    <JsonBlock value={review.actions_json} />
                  </div>
                )}
              </div>
            </article>
          ))}
        </div>
      </Section>
    </div>
  );
}

function InstructionsPage({ data }: PageProps) {
  const versions = newest(rows(data, "instruction_version"), "created_at");
  const memories = newest(rows(data, "agent_memory"), "created_at");
  if (!versions.length && !memories.length)
    return <Empty>No instructions or memory for this case yet</Empty>;
  return (
    <div className="nv-panel nv-scroll">
      {versions.length > 0 && (
        <Section title="Instruction versions" count={versions.length}>
          <div className="nv-document-list">
            {versions.map((version) => (
              <article className="nv-instruction-card" key={field(version.id)}>
                <div className="nv-card-head">
                  <div>
                    <span className="nv-eyebrow">
                      {field(version.agent)} · v{field(version.version)}
                    </span>
                    <h3>{field(version.reason)}</h3>
                  </div>
                  <Status
                    value={version.active}
                    label={version.active ? "Active" : "Inactive"}
                  />
                </div>
                <p className="nv-document-text">{field(version.diff)}</p>
                <div className="nv-route-meta">
                  <span>Author · {field(version.author)}</span>
                  <span>Created · {stamp(version.created_at)}</span>
                  <span>Source · {field(version.source_ref)}</span>
                </div>
              </article>
            ))}
          </div>
        </Section>
      )}
      {memories.length > 0 && (
        <Section title="Agent memory" count={memories.length}>
          <div className="nv-document-list">
            {memories.map((memory) => (
              <article className="nv-instruction-card" key={field(memory.id)}>
                <div className="nv-card-head">
                  <div>
                    <span className="nv-eyebrow">
                      {field(memory.agent)} · {field(memory.scope)}
                    </span>
                    <h3>{field(memory.source)}</h3>
                  </div>
                  <Status
                    value={memory.active}
                    label={memory.active ? "Active" : "Inactive"}
                  />
                </div>
                <p className="nv-document-text">{field(memory.text)}</p>
                <div className="nv-route-meta">
                  <span>Version · {field(memory.version)}</span>
                  <span>Created · {stamp(memory.created_at)}</span>
                  <span>Source ref · {field(memory.source_ref)}</span>
                </div>
              </article>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

function Timeline({ step, lines }: { step: Step; lines: Line[] }) {
  if (!lines.length) return <Empty>No events for this case yet</Empty>;
  return (
    <div className="nv-panel nv-scroll">
      <Section title={step.title} count={lines.length}>
        <div className="nv-timeline">
          {lines.map((line) => (
            <article className="nv-timeline-item" key={line.key}>
              <time>{stamp(line.ts)}</time>
              <div>
                <strong>{line.text}</strong>
                {line.meta && <span>{line.meta}</span>}
              </div>
            </article>
          ))}
        </div>
      </Section>
    </div>
  );
}

function PageContent({
  id,
  data,
  caseId,
  step,
  lines,
}: PageProps & { id: string; step: Step; lines: Line[] }) {
  switch (id) {
    case "generator":
    case "carrier_ap":
      return <InvoicePage data={data} caseId={caseId} />;
    case "ingest":
      return <InboxPage data={data} caseId={caseId} />;
    case "asus":
      return <RoutingPage data={data} caseId={caseId} />;
    case "risk":
      return <RiskPage data={data} caseId={caseId} />;
    case "contacts":
    case "verification":
      return (
        <ContactPage
          data={data}
          caseId={caseId}
          verification={id === "verification"}
        />
      );
    case "voice_desk":
      return <CallPage data={data} caseId={caseId} />;
    case "controller":
    case "payments":
      return <PaymentPage data={data} caseId={caseId} />;
    case "cfo":
      return <ForecastPage data={data} caseId={caseId} />;
    case "audit":
      return <AuditPage data={data} caseId={caseId} />;
    case "human":
      return <ApprovalPage data={data} caseId={caseId} />;
    case "validator":
      return <ReviewPage data={data} caseId={caseId} />;
    case "instructions":
      return <InstructionsPage data={data} caseId={caseId} />;
    default:
      return <Timeline step={step} lines={lines} />;
  }
}

function pageName(id: string) {
  const names: Record<string, string> = {
    generator: "Invoice",
    carrier_ap: "Invoice",
    ingest: "Inbox",
    asus: "Intake routing",
    risk: "Risk review",
    contacts: "Contacts",
    verification: "Verification",
    voice_desk: "Call activity",
    controller: "Payment",
    payments: "Payment",
    cfo: "Cash forecast",
    audit: "Audit history",
    human: "Approvals",
    validator: "Reviews",
    instructions: "Instructions & memory",
  };
  return names[id] ?? "Workflow";
}

function View({ id, caseId }: { id: string; caseId: string }) {
  const { data, state } = useLive();
  const step = stepById(id);
  const scoped = useMemo(() => scope(data, caseId), [data, caseId]);
  const lines = useMemo(
    () => (step ? feedOf(step, scoped) : []),
    [step, scoped],
  );
  const connection = String(state);
  return (
    <main className="nv">
      <header className="nv-appbar">
        <span className="nv-logo" />
        <strong className="nv-brand">Golden Gate Freight</strong>
        <span className="nv-divider" />
        <span className="nv-page-name">{pageName(id)}</span>
        <span className="nv-case">{caseId || "All cases"}</span>
      </header>
      <section className="nv-main">
        {step ? (
          <>
            <div className="nv-stephead">
              <span className="wf-agent">{step.operator}</span>
              <span className="nv-step-title">{step.title}</span>
              <span className={`nv-connection ${connection}`}>
                {connection === "live" ? "Live" : `Data ${connection}`}
              </span>
            </div>
            <div className="nv-page-body">
              <PageContent
                id={id}
                data={scoped}
                caseId={caseId}
                step={step}
                lines={lines}
              />
            </div>
          </>
        ) : (
          <Empty>No workflow page for this step</Empty>
        )}
      </section>
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
