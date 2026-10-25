"use client";
/**
 * Invoices & payments: open carrier invoices (with contract delta and hold
 * state) and payment proposals. Every row renders; the table scrolls inside
 * the panel. Rows remount when their status changes, so a status flip flashes
 * the row.
 */
import Link from "next/link";
import { useMemo, useState } from "react";
import { ArrowUpRight } from "lucide-react";
import { useOps } from "@/components/data-provider";
import { Mascot } from "@/components/mascot";
import { num, str, timestamp } from "@/lib/data";
import { fmtAgo, invoiceContractDelta } from "@/lib/ops";
import { emptyNote, humanize, nick, usd, usdShort } from "./format";
import { FreshTr, useSettled } from "./fresh";

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

/** Row id; the row's only link to its case (the case id itself is in the tooltip). */
function IdCell({ id, caseId, title }: { id: string; caseId: string; title?: string }) {
  return caseId ? (
    <Link href={`/cases/${encodeURIComponent(caseId)}`} className="fh-cell-main fl-mono fh-id-link" style={{ whiteSpace: "nowrap" }} title={title ?? `Open ${caseId}`} aria-label={`${id}, open ${caseId}`}>
      {id}
      <ArrowUpRight size={12} aria-hidden style={{ display: "inline-block", marginLeft: 4, verticalAlign: "-1px", opacity: 0.6 }} />
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
          // Amount is what is still owed; the delta is the bill against its contract.
          const amount = num(i.amount) - num(i.paid_amount);
          const contract = num(i.contract_amount);
          return {
            id,
            caseId: str(i.case_id, ""),
            carrier: companies.get(str(i.carrier_id)) ?? str(i.carrier_id),
            load: str(i.load_id),
            amount,
            contract,
            invoiced: num(i.amount),
            delta: invoiceContractDelta(i),
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

  const needApproval = proposals.filter((p) => p.status === "awaiting_approval").length;
  const rows = tab === "invoices" ? invoices : proposals;

  return (
    <section className="fl-panel fh-inv" aria-label="Invoices and payments">
      <header className="fl-panel-head">
        <div className="fl-tabs fh-inv-tabs" role="tablist">
          <button role="tab" aria-selected={tab === "invoices"} className={`fl-tab ${tab === "invoices" ? "active" : ""}`} onClick={() => setTab("invoices")}>
            Open invoices
          </button>
          <button role="tab" aria-selected={tab === "proposals"} className={`fl-tab ${tab === "proposals" ? "active" : ""}`} onClick={() => setTab("proposals")}>
            Proposals
          </button>
        </div>
      </header>
      <div className="fh-inv-summary">
        {tab === "invoices" ? (
          <>
            <span>
              <b className="fl-num">{usd(finance.outstandingInvoices.amount)}</b> outstanding
            </span>
            {finance.heldInvoices.amount > 0 && (
              <span>
                <b className="fl-num">{usdShort(finance.heldInvoices.amount)}</b> on hold
              </span>
            )}
            {finance.overdueInvoices.count > 0 && (
              <span className="hot" title="Over 30 days">
                <b className="fl-num">{finance.overdueInvoices.count}</b> overdue
              </span>
            )}
          </>
        ) : (
          <>
            <span>
              <b className="fl-num">{usd(finance.paymentsProposed.amount)}</b> awaiting release
            </span>
            {needApproval > 0 && (
              <span className="hot">
                <b className="fl-num">{needApproval}</b> need approval
              </span>
            )}
          </>
        )}
      </div>
      <div className="fh-table-wrap">
        {rows.length === 0 ? (
          <div className="fh-empty">
            <div className="fl-label strong">{emptyNote(source, tab === "invoices" ? "No open invoices" : "No payment proposals")}</div>
          </div>
        ) : tab === "invoices" ? (
          <table className="fl-table fh-table">
            <thead>
              <tr>
                <th>Invoice</th>
                <th className="num">Amount</th>
                <th className="num fh-opt">Δ contract</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {invoices.map((r) => (
                <FreshTr key={`${r.id}:${r.status}`} settled={settled}>
                  <td>
                    <IdCell
                      id={r.id}
                      caseId={r.caseId}
                      title={[r.load && `Load ${r.load}`, r.issuedAt && `Issued ${fmtAgo(r.issuedAt, now)}`, r.caseId && `Open ${r.caseId}`].filter(Boolean).join(" · ") || undefined}
                    />
                    <span className="fh-cell-sub">{r.carrier}</span>
                  </td>
                  <td className="num">
                    <span className="fh-cell-main">{usd(r.amount)}</span>
                  </td>
                  <td
                    className={`num fh-opt ${r.delta > 0 ? "fh-hot" : r.delta < 0 ? "fh-ok" : "fh-mute"}`}
                    title={r.contract ? `Invoiced ${usd(r.invoiced)} against contract ${usd(r.contract)}` : "No contract on file"}
                  >
                    {r.delta ? `${r.delta > 0 ? "+" : "−"}${usd(Math.abs(r.delta))}` : null}
                  </td>
                  <td>
                    <span className={`fl-chip ${STATUS_CHIP[r.status] ?? "outline"}`} title={r.held > 0 ? `${usd(r.held)} held` : undefined}>
                      {statusLabel(r.status)}
                    </span>
                  </td>
                </FreshTr>
              ))}
            </tbody>
          </table>
        ) : (
          <table className="fl-table fh-table">
            <thead>
              <tr>
                <th>Proposal</th>
                <th className="num">Amount</th>
                <th>Status</th>
                <th className="fh-opt">By</th>
              </tr>
            </thead>
            <tbody>
              {proposals.map((r) => (
                <FreshTr key={`${r.id}:${r.status}`} settled={settled}>
                  <td>
                    <IdCell id={r.id} caseId={r.caseId} title={[r.at && `Proposed ${fmtAgo(r.at, now)}`, r.caseId && `Open ${r.caseId}`].filter(Boolean).join(" · ") || undefined} />
                    <span className="fh-cell-sub">{r.carrier}</span>
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
                </FreshTr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
