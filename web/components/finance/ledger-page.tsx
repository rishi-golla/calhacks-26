"use client";
/**
 * Ledger: the books themselves. Journal entries with who requested and who posted each one,
 * a trial balance that must net to zero, AR/AP subledger tie-outs against their control
 * accounts, and the audit spreadsheet (GL and subledgers with blame, diff and history).
 */
import Link from "next/link";
import dynamic from "next/dynamic";
import { useMemo } from "react";
import { useOps } from "@/components/data-provider";
import { json, str, type Row } from "@/lib/data";
import { fmtMoney } from "@/lib/ops";
import "./finance.css";

const AuditWorkspace = dynamic(() => import("@/components/audit-workspace"), {
  ssr: false,
  loading: () => <div className="fh-empty-sub">Loading audit workspace…</div>,
});

type Line = { account: string; debit: number; credit: number };
const n = (v: unknown) => (typeof v === "number" ? v : Number(v) || 0);
const lines = (raw: unknown): Line[] => {
  const list = Array.isArray(raw) ? raw : json<unknown[]>(raw, []);
  return list.map((l) =>
    Array.isArray(l)
      ? { account: str(l[0]), debit: n(l[1]), credit: n(l[2]) }
      : { account: str((l as Row).account), debit: n((l as Row).debit), credit: n((l as Row).credit) },
  );
};
const AR = new Set(["accounts_receivable", "account_receivable", "ar", "a/r", "receivables", "trade_receivables"]);
const AP = new Set(["accounts_payable", "account_payable", "ap", "a/p", "payables", "trade_payables"]);
const norm = (a: string) => a.toLowerCase().replace(/[\s-]+/g, "_");
const sub = (rows: Row[]) => rows.reduce((s, r) => s + n(r.debit) - n(r.credit), 0);

export default function LedgerPage() {
  const { snapshot } = useOps();
  const snap = snapshot as unknown as Record<string, Row[] | undefined>;
  const entries = useMemo(
    () =>
      (snapshot.ledger_entry as Row[])
        .map((r) => ({ row: r, lines: lines(r.lines), at: str(r.posted_at) }))
        .sort((a, b) => b.at.localeCompare(a.at)),
    [snapshot.ledger_entry],
  );
  const trial = useMemo(() => {
    const m = new Map<string, { debit: number; credit: number }>();
    for (const e of entries)
      for (const l of e.lines) {
        const k = norm(l.account);
        const s = m.get(k) ?? { debit: 0, credit: 0 };
        s.debit += l.debit;
        s.credit += l.credit;
        m.set(k, s);
      }
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [entries]);
  const totD = trial.reduce((s, [, v]) => s + v.debit, 0);
  const totC = trial.reduce((s, [, v]) => s + v.credit, 0);
  const control = (set: Set<string>) => trial.filter(([k]) => set.has(k)).reduce((s, [, v]) => s + v.debit - v.credit, 0);
  const arSub = snap.ar_subledger ? sub(snap.ar_subledger) : null;
  const apSub = snap.ap_subledger ? sub(snap.ap_subledger) : null;
  const ties = [
    { label: "Debits = credits", a: totD, b: totC },
    { label: "AR subledger = AR control", a: arSub, b: control(AR) },
    { label: "AP subledger = AP control", a: apSub, b: control(AP) },
  ];

  return (
    <div className="fl-page fh">
      <section className="fl-panel fl-reveal" aria-label="Tie-outs">
        <header className="fl-panel-head"><span className="fl-section">Ledger · tie-outs</span></header>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))", gap: 12, padding: "4px 0 8px" }}>
          {ties.map((t) => {
            const ok = t.a !== null && Math.abs(t.a - t.b) < 0.005;
            return (
              <div key={t.label} className="fl-panel" style={{ padding: 12 }}>
                <div className="fl-micro">{t.label}</div>
                <div className="fl-num" style={{ fontSize: 18, marginTop: 4 }}>
                  {t.a === null ? "—" : fmtMoney(t.a)} · {fmtMoney(t.b)}
                </div>
                <span className={`fl-chip outline ${ok ? "ok" : t.a === null ? "" : "bad"}`}>{t.a === null ? "subledger not loaded" : ok ? "ties" : `off by ${fmtMoney(t.a - t.b)}`}</span>
              </div>
            );
          })}
        </div>
      </section>

      <div className="fh-row fl-reveal" style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,2fr)", gap: 16 }}>
        <section className="fl-panel" aria-label="Trial balance">
          <header className="fl-panel-head"><span className="fl-section">Trial balance</span></header>
          <table className="fl-table" style={{ width: "100%" }}>
            <thead><tr><th style={{ textAlign: "left" }}>Account</th><th>Debit</th><th>Credit</th></tr></thead>
            <tbody>
              {trial.map(([k, v]) => (
                <tr key={k}><td>{k.replaceAll("_", " ")}</td><td className="fl-num">{fmtMoney(v.debit)}</td><td className="fl-num">{fmtMoney(v.credit)}</td></tr>
              ))}
              <tr><td><strong>Total</strong></td><td className="fl-num"><strong>{fmtMoney(totD)}</strong></td><td className="fl-num"><strong>{fmtMoney(totC)}</strong></td></tr>
            </tbody>
          </table>
        </section>

        <section className="fl-panel" aria-label="Journal entries">
          <header className="fl-panel-head"><span className="fl-section">Journal entries</span><span className="fh-head-meta fl-micro">{entries.length}</span></header>
          <div style={{ maxHeight: 520, overflow: "auto" }}>
            <table className="fl-table" style={{ width: "100%" }}>
              <thead><tr><th style={{ textAlign: "left" }}>Posted</th><th style={{ textAlign: "left" }}>Memo · lines</th><th>Requested by</th><th>Posted by</th><th>Case</th></tr></thead>
              <tbody>
                {entries.slice(0, 300).map(({ row, lines: ls, at }) => (
                  <tr key={str(row.id)}>
                    <td className="fl-micro">{at.slice(0, 16).replace("T", " ")}</td>
                    <td>
                      <div>{str(row.memo) || str(row.id)}</div>
                      {ls.map((l, i) => (
                        <div key={i} className="fl-micro fl-num">
                          {l.debit ? `Dr ${l.account} ${fmtMoney(l.debit)}` : `Cr ${l.account} ${fmtMoney(l.credit)}`}
                        </div>
                      ))}
                    </td>
                    <td className="fl-micro">{str(row.requested_by) || "—"}</td>
                    <td className="fl-micro">{str(row.approved_by) || "—"}</td>
                    <td className="fl-micro">{str(row.case_id) ? <Link href={`/cases/${encodeURIComponent(str(row.case_id))}`}>{str(row.case_id)}</Link> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <section className="fl-panel fl-reveal" aria-label="Audit spreadsheet">
        <header className="fl-panel-head"><span className="fl-section">General ledger &amp; subledgers · blame, diff, history</span></header>
        <AuditWorkspace onFlag={() => undefined} />
      </section>
    </div>
  );
}
