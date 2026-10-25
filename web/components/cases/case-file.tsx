"use client";
/**
 * Case file: every source item in the case at a glance (documents, emails,
 * bank lines) and the facts agents compare, each document and email opening
 * in the evidence viewer. Read from the case's `source_record` row through a
 * subscription of its own, scoped to this case and dropped on unmount.
 *
 * A column beside the trace on wide screens; a collapsed bar above it on
 * narrow ones.
 */
import { useEffect, useId, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { EvidenceChip } from "@/components/evidence";
import { ShowWork } from "@/components/show-work";
import { useSourceRecords } from "@/components/live-provider";
import { date, timestamp } from "@/lib/data";
import { fmtMoney } from "@/lib/ops";
import { detentionWorkings, rateDeltaWorkings, shortPayWorkings } from "@/lib/show-work";
import { buildCaseFile, caseFileSummary, type CaseFile as File } from "./case-file-model";
import { Json } from "./node-panel";

const NARROW = "(max-width: 1100px)";

/** A bank line's posting day, as the bank dates it (UTC), not the viewer's. */
const day = (value: string) => {
  const ms = timestamp(value);
  return ms ? new Date(ms).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }) : "—";
};
const money = (n: number | null) => (n === null ? "—" : fmtMoney(n));
const hours = (h: number) => `${Number.isInteger(h) ? h : h.toFixed(1)} h`;

/** True at widths where the case file sits above the trace instead of beside it. */
function useNarrow() {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const query = window.matchMedia(NARROW);
    const update = () => setNarrow(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return narrow;
}

export function CaseFile({ caseId, simulated }: { caseId: string; simulated: boolean }) {
  const records = useSourceRecords(simulated ? "" : caseId);
  const result = useMemo(() => buildCaseFile(records.rows, caseId), [records.rows, caseId]);
  // Always open beside the trace; above it, collapsed to its summary until asked.
  const narrow = useNarrow();
  const [expanded, setExpanded] = useState(false);
  const open = !narrow || expanded;

  const ready = result.state === "ready" ? result.file : null;
  const note = simulated
    ? "The simulation has no source files."
    : records.state === "loading"
      ? "Loading…"
      : records.state === "offline"
        ? "Needs the live connection."
        : records.state === "absent"
          ? "This database keeps no case files."
          : "No case file mirrored for this case.";
  const summary = ready ? caseFileSummary(ready) : result.state === "invalid" ? "Unreadable" : simulated ? "Simulation" : records.state === "loading" ? "Loading" : "None";

  const bodyId = `case-file-${useId()}`;
  const head = (
    <>
      <span className="fl-section" style={{ "--accent": "var(--lilac)" } as CSSProperties}>
        Case file
      </span>
      <span className="fl-micro cf-summary">{summary}</span>
    </>
  );

  return (
    <aside className={`cf ${open ? "open" : ""}`} aria-label="Case file">
      {narrow ? (
        <button type="button" className="cf-head" onClick={() => setExpanded((o) => !o)} aria-expanded={open} aria-controls={bodyId}>
          {head}
          <ChevronDown size={14} className="cf-caret" aria-hidden />
        </button>
      ) : (
        <div className="cf-head">{head}</div>
      )}
      <div className="cf-body" id={bodyId} hidden={!open}>
        {ready ? (
          <FileBody file={ready} />
        ) : result.state === "invalid" ? (
          <p className="cf-note">
            The case record <code>{result.recordId}</code> could not be read.
          </p>
        ) : (
          <p className="cf-note">{note}</p>
        )}
      </div>
    </aside>
  );
}

function Sec({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="cf-sec">
      <h3 className="ct-sec-title">
        {title}
        {count !== undefined && <span className="fl-mono fl-mute">{count}</span>}
      </h3>
      {children}
    </section>
  );
}

function FileBody({ file }: { file: File }) {
  const short = file.billed !== null && file.bank.length > 0 ? Math.round((file.billed - file.deposited) * 100) / 100 : null;
  return (
    <>
      <Sec title="Documents" count={file.documents.length}>
        {file.documents.length || file.missing.length ? (
          <ul className="cf-list">
            {file.documents.map((d) => (
              <li key={d.ref} className="cf-doc">
                <span className="cf-kind">
                  {d.label}
                  {d.kind === "pod" && d.signed !== undefined && <span className={`fl-chip outline ${d.signed ? "ok" : "bad"} ct-mini-chip`}>{d.signed ? "signed" : "unsigned"}</span>}
                  {d.amount !== undefined && <span className="fl-num cf-amount">{fmtMoney(d.amount)}</span>}
                </span>
                <EvidenceChip evidenceRef={d.ref} />
              </li>
            ))}
            {file.missing.map((m) => (
              <li key={`missing:${m.kind}`} className="cf-doc">
                <span className="cf-kind">
                  {m.label}
                  <span className="fl-chip outline bad ct-mini-chip">missing</span>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="cf-none">None on file.</p>
        )}
      </Sec>

      <Sec title="Emails" count={file.emails.length}>
        {file.emails.length ? (
          <div className="cf-chips">
            {file.emails.map((r) => (
              <EvidenceChip key={r} evidenceRef={r} />
            ))}
          </div>
        ) : (
          <p className="cf-none">None on file.</p>
        )}
        {file.otherRefs.length > 0 && (
          <div className="cf-chips">
            {file.otherRefs.map((r) => (
              <EvidenceChip key={r} evidenceRef={r} />
            ))}
          </div>
        )}
      </Sec>

      <Sec title="Bank lines" count={file.bank.length}>
        {file.bank.length ? (
          <ul className="cf-list">
            {file.bank.map((b, i) => (
              <li key={b.id || i} className="cf-bank">
                <div className="cf-bank-top">
                  <span className="fl-num cf-bank-amount">{money(b.amount)}</span>
                  <span className="fl-mono fl-mute">{day(b.postedAt)}</span>
                  {b.unapplied && <span className="fl-chip outline hot ct-mini-chip">unapplied</span>}
                </div>
                <div className="cf-ids fl-mono">
                  {b.invoiceId && <span title={b.unapplied ? "Candidate invoice" : "Applied to invoice"}>{b.invoiceId}</span>}
                  {b.remittanceRef && <span title="Remittance reference">ref {b.remittanceRef}</span>}
                  {b.id && <span title="Bank line">{b.id}</span>}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="cf-none">No deposits yet.</p>
        )}
        {file.billed !== null && (
          <p className="cf-total">
            Billed <span className="fl-num">{fmtMoney(file.billed)}</span> · received <span className="fl-num">{fmtMoney(file.deposited)}</span>
            {short !== null && short > 0 && (
              <ShowWork workings={shortPayWorkings(file, short)} title="Short pay">
                <span className="fl-chip outline bad ct-mini-chip">short {fmtMoney(short)}</span>
              </ShowWork>
            )}
          </p>
        )}
      </Sec>

      <Sec title="Key facts">
        <Facts file={file} />
        {file.flags.length > 0 && (
          <div className="cf-chips">
            {file.flags.map((f) => (
              <span key={f} className="fl-chip outline bad ct-mini-chip">
                {f.replaceAll("_", " ")}
              </span>
            ))}
          </div>
        )}
      </Sec>

      <div className="cf-raw">
        <Json value={file.record} label="record_json" />
        <p className="fl-micro cf-stamp" title="source_record row">
          {file.recordId}
          {file.updatedAt ? ` · ${date(file.updatedAt)}` : ""}
        </p>
      </div>
    </>
  );
}

function Facts({ file }: { file: File }) {
  const rows: [string, ReactNode][] = [];
  if (file.carrier) {
    rows.push(["Carrier", file.carrier.name || file.carrier.id || "—"]);
    rows.push([
      "USDOT · MC",
      <span key="dot" className="fl-mono">
        {file.carrier.usdot || "—"} · {file.carrier.mc || "—"}
      </span>,
    ]);
  }
  if (file.invoiceCarrier)
    rows.push([
      "Invoiced by",
      <span key="inv" className="cf-bad">
        {file.invoiceCarrier.name || "Another carrier"}
        {file.invoiceCarrier.usdot && <span className="fl-mono"> · {file.invoiceCarrier.usdot}</span>}
      </span>,
    ]);
  if (file.shipper) rows.push(["Shipper", file.shipper]);
  if (file.bol) rows.push(["BOL", <span key="bol" className="fl-mono">{file.bol}</span>]);
  if (file.rate) {
    const { contract, invoice, delta } = file.rate;
    rows.push([
      "Rate",
      <span key="rate">
        <span className="fl-num">{money(contract)}</span> <span className="fl-mute">contract</span> · <span className="fl-num">{money(invoice)}</span>{" "}
        <span className="fl-mute">invoiced</span>
        {delta !== null && delta !== 0 && (
          <ShowWork workings={rateDeltaWorkings(file)} title="Rate delta">
            <span className={`fl-chip outline ${delta > 0 ? "bad" : "info"} ct-mini-chip`}>
              {delta > 0 ? "+" : "−"}
              {fmtMoney(Math.abs(delta))}
            </span>
          </ShowWork>
        )}
      </span>,
    ]);
  }
  if (file.detention) {
    const { claimedHours, dwellMin, freeTimeMin, billableHours, overClaimed } = file.detention;
    rows.push([
      "Detention",
      <span key="det">
        {claimedHours ? `${hours(claimedHours)} claimed` : "None claimed"}
        <span className="fl-mute">
          {" "}
          · GPS dwell {dwellMin ?? "—"} min, {freeTimeMin ?? "—"} free
        </span>
        {overClaimed && (
          <ShowWork workings={detentionWorkings(file)} title="Detention">
            <span className="fl-chip outline bad ct-mini-chip">GPS supports {hours(billableHours ?? 0)}</span>
          </ShowWork>
        )}
      </span>,
    ]);
  }
  if (file.claimType) rows.push(["Claim", file.claimType.replaceAll("_", " ")]);
  if (file.deductionReason) rows.push(["Deduction", file.deductionReason]);
  if (file.bankChange) {
    const b = file.bankChange;
    rows.push([
      "Bank change",
      <span key="bank" className="cf-bad">
        {b.payee || "New payee"}
        {b.accountLast4 && <span className="fl-mono"> ··{b.accountLast4}</span>}
        <span className="fl-mute">
          {b.via && ` · via ${b.via.replaceAll("_", " ")}`}
          {b.urgency && ` · “${b.urgency}”`}
          {b.senderMatches === false && " · sender not registered"}
        </span>
      </span>,
    ]);
  }
  if (!rows.length) return <p className="cf-none">No facts recorded.</p>;
  return (
    <dl className="cf-kv">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt className="fl-micro">{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}
