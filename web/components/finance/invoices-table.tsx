"use client";
/**
 * Invoices & payments: open carrier invoices (with contract delta and hold
 * state) and payment proposals. Rows remount when their status changes, so a
 * status flip flashes the row.
 */
import Link from "next/link";
import { useMemo, useState, type CSSProperties } from "react";
import { ArrowUpRight } from "lucide-react";
import { useOps } from "@/components/data-provider";
import { Mascot } from "@/components/mascot";
import { num, str, timestamp } from "@/lib/data";
import { fmtAgo } from "@/lib/ops";
import { emptyNote, humanize, nick, usd, usdShort } from "./format";
import { FreshTr, useSettled } from "./fresh";

const LIMIT = 8;
const OUTSTANDING = new Set(["proposed", "awaiting_approval"]);

const STATUS_CHIP: Record<string, string> = {
  open: "info outline",
  held: "hot outline",
  proposed: "info outline",
  awaiting_approval: "hot solid",
  approved: "ok outline",
  sent: "ok solid",
  paid: "ok solid",
  rejected: "bad outline",
};
const statusLabel = (s: string) => (s === "awaiting_approval" ? "Needs approval" : humanize(s));

function CaseLink({ caseId }: { caseId: string }) {
  if (!caseId) return <span className="fh-mute">—</span>;
  return (
    <Link href={`/cases/${encodeURIComponent(caseId)}`} className="fh-case-link" aria-label={`Open ${caseId}`}>
      <span className="fl-mono">{caseId}</span>
      <ArrowUpRight size={12} />
    </Link>
  );
}

/** Row id; links to its case (the only case link left on phones). */
function IdCell({ id, caseId, title }: { id: string; caseId: string; title?: string }) {
  return caseId ? (
    <Link href={`/cases/${encodeURIComponent(caseId)}`} className="fh-cell-main fl-mono fh-id-link" title={title ?? "Open case"}>
      {id}
    </Link>
  ) : (
    <span className="fh-cell-main fl-mono" title={title}>
      {id}
    </span>
  );
}

export function InvoicesPanel() {
  const { snapshot, now, finance, source } = useOps();
  const [tab, setTab] = useState<"invoices" | "proposals">("invoices");
  const [showAll, setShowAll] = useState(false);
  const settled = useSettled();

  const companies = useMemo(() => new Map(snapshot.company.map((c) => [str(c.id), str(c.name)])), [snapshot.company]);
  const holds = useMemo(() => {
    const m = new Map<string, number>();
    for (const h of snapshot.hold)
      if (["active", "held"].includes(str(h.status, "")))
        m.set(str(h.invoice_id), (m.get(str(h.invoice_id)) ?? 0) + Math.max(0, num(h.amount) - num(h.released_amount)));
    return m;
  }, [snapshot.hold]);

  const invoices = useMemo(
    () =>
      snapshot.invoice
        .filter((i) => ["open", "held"].includes(str(i.status, "open")))
        .map((i) => {
          const id = str(i.id);
          const amount = num(i.amount) - num(i.paid_amount);
          const contract = num(i.contract_amount);
          return {
            id,
            caseId: str(i.case_id, ""),
            carrier: companies.get(str(i.carrier_id)) ?? str(i.carrier_id),
            load: str(i.load_id),
            amount,
            contract,
            delta: contract ? amount - contract : 0,
            issuedAt: timestamp(i.issued_at),
            status: holds.has(id) ? "held" : str(i.status, "open"),
            held: holds.get(id) ?? 0,
          };
        })
        .sort((a, b) => b.issuedAt - a.issuedAt || b.id.localeCompare(a.id)),
    [snapshot.invoice, companies, holds],
  );
  const proposals = useMemo(
    () =>
      snapshot.payment_proposal
        .map((p) => ({
          id: str(p.id),
          caseId: str(p.case_id, ""),
          carrier: companies.get(str(p.carrier_id)) ?? str(p.carrier_id),
          invoice: str(p.invoice_id),
          amount: num(p.amount),
          status: str(p.status, "proposed"),
          by: str(p.proposed_by, "carrier_ap"),
          at: timestamp(p.proposed_at),
        }))
        // Outstanding first, then newest.
        .sort((a, b) => Number(OUTSTANDING.has(b.status)) - Number(OUTSTANDING.has(a.status)) || b.at - a.at),
    [snapshot.payment_proposal, companies],
  );

  const rows = tab === "invoices" ? invoices : proposals;
  const visible = showAll ? rows : rows.slice(0, LIMIT);

  return (
    <section className="fl-panel fh-inv" aria-label="Invoices and payments">
      <header className="fl-panel-head">
        <span className="fl-section" style={{ "--accent": "var(--aqua)" } as CSSProperties}>
          Invoices & payments
        </span>
        <div className="fl-tabs fh-inv-tabs" role="tablist">
          <button role="tab" aria-selected={tab === "invoices"} className={`fl-tab ${tab === "invoices" ? "active" : ""}`} onClick={() => setTab("invoices")}>
            Open invoices <span className="fl-num">{finance.outstandingInvoices.count}</span>
          </button>
          <button role="tab" aria-selected={tab === "proposals"} className={`fl-tab ${tab === "proposals" ? "active" : ""}`} onClick={() => setTab("proposals")}>
            Proposals <span className="fl-num">{finance.paymentsProposed.count}</span>
          </button>
        </div>
      </header>
      <div className="fh-inv-summary">
        {tab === "invoices" ? (
          <>
            <span>
              <b className="fl-num">{usd(finance.outstandingInvoices.amount)}</b> outstanding
            </span>
            <span>
              <b className="fl-num">{finance.heldInvoices.count}</b> held · {usdShort(finance.heldInvoices.amount)} on hold
            </span>
            <span className={finance.overdueInvoices.count ? "hot" : ""}>
              <b className="fl-num">{finance.overdueInvoices.count}</b> overdue 30d+
            </span>
          </>
        ) : (
          <>
            <span>
              <b className="fl-num">{usd(finance.paymentsProposed.amount)}</b> awaiting release
            </span>
            <span>
              <b className="fl-num">{proposals.filter((p) => p.status === "awaiting_approval").length}</b> need human approval
            </span>
          </>
        )}
      </div>
      <div className="fh-table-wrap">
        {rows.length === 0 ? (
          <div className="fh-empty">
            <div>
              <div className="fl-label strong">{emptyNote(source, tab === "invoices" ? "No open carrier invoices" : "No payment proposals yet")}</div>
              <div className="fh-empty-sub">{source === "live" ? "Rows appear here as soon as agents record them." : "Nothing to show from the current source."}</div>
            </div>
          </div>
        ) : tab === "invoices" ? (
          <table className="fl-table fh-table">
            <thead>
              <tr>
                <th>Invoice · carrier</th>
                <th className="num">Amount</th>
                <th className="num fh-opt">Δ contract</th>
                <th>Status</th>
                <th className="fh-opt">Case</th>
              </tr>
            </thead>
            <tbody>
              {(visible as typeof invoices).map((r) => (
                <FreshTr key={`${r.id}:${r.status}`} settled={settled}>
                  <td>
                    <IdCell id={r.id} caseId={r.caseId} title={r.load ? `Load ${r.load} · open case` : undefined} />
                    <span className="fh-cell-sub" suppressHydrationWarning>
                      {r.carrier}
                      {r.issuedAt ? ` · issued ${fmtAgo(r.issuedAt, now)}` : ""}
                    </span>
                  </td>
                  <td className="num">
                    <span className="fh-cell-main">{usd(r.amount)}</span>
                    <span className="fh-cell-sub">{r.contract ? `contract ${usd(r.contract)}` : "no contract"}</span>
                  </td>
                  <td className={`num fh-opt ${r.delta > 0 ? "fh-hot" : r.delta < 0 ? "fh-ok" : "fh-mute"}`}>
                    {r.delta ? `${r.delta > 0 ? "+" : "−"}${usd(Math.abs(r.delta))}` : "—"}
                  </td>
                  <td>
                    <span className={`fl-chip ${STATUS_CHIP[r.status] ?? "outline"}`}>{statusLabel(r.status)}</span>
                    {r.held > 0 && <span className="fh-cell-sub fh-hot">{usd(r.held)} held</span>}
                  </td>
                  <td className="fh-opt">
                    <CaseLink caseId={r.caseId} />
                  </td>
                </FreshTr>
              ))}
            </tbody>
          </table>
        ) : (
          <table className="fl-table fh-table">
            <thead>
              <tr>
                <th>Proposal · carrier</th>
                <th className="num">Amount</th>
                <th>Status</th>
                <th className="fh-opt">By</th>
                <th className="fh-opt">Case</th>
              </tr>
            </thead>
            <tbody>
              {(visible as typeof proposals).map((r) => (
                <FreshTr key={`${r.id}:${r.status}`} settled={settled}>
                  <td>
                    <IdCell id={r.id} caseId={r.caseId} />
                    <span className="fh-cell-sub" suppressHydrationWarning>
                      {r.carrier}
                      {r.at ? ` · ${fmtAgo(r.at, now)}` : ""}
                    </span>
                  </td>
                  <td className="num">
                    <span className="fh-cell-main">{usd(r.amount)}</span>
                    <span className="fh-cell-sub fl-mono">{r.invoice}</span>
                  </td>
                  <td>
                    <span className={`fl-chip ${STATUS_CHIP[r.status] ?? "outline"}`}>{statusLabel(r.status)}</span>
                  </td>
                  <td className="fh-opt">
                    <span className="fh-by">
                      <Mascot id={r.by} size={22} />
                      {nick(r.by)}
                    </span>
                  </td>
                  <td className="fh-opt">
                    <CaseLink caseId={r.caseId} />
                  </td>
                </FreshTr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {rows.length > LIMIT && (
        <button className="fh-more" onClick={() => setShowAll((v) => !v)}>
          {showAll ? "Show fewer" : `Show all ${rows.length}`}
        </button>
      )}
    </section>
  );
}
