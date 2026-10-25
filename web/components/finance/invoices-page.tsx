"use client";

import { InvoicesPanel } from "./invoices-table";
import { LiveTicker } from "./live-ticker";
import "./finance.css";

export default function InvoicesPage() {
  return (
    <div className="fl-page fh">
      <div className="fh-row fh-row-bottom fl-reveal">
        <InvoicesPanel />
        <LiveTicker />
      </div>
    </div>
  );
}
