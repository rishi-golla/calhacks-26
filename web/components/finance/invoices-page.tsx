"use client";

import { InvoicesPanel } from "./invoices-table";
import { LiveTicker } from "./live-ticker";
import { PageHead } from "./page-head";
import "./finance.css";

export default function InvoicesPage() {
  return (
    <div className="fl-page fh">
      <PageHead title="Invoices & payments" sub="Open invoices, holds, proposed payments and the live ledger." />
      <div className="fh-row fh-row-bottom fl-reveal">
        <InvoicesPanel />
        <LiveTicker />
      </div>
    </div>
  );
}
