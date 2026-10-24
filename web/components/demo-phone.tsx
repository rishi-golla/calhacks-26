"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { Check, LoaderCircle, Phone } from "lucide-react";
import { DemoPhoneClient, demoPhoneAction } from "@/lib/demo-phone";
import "./demo-phone.css";

const DEMO_GUIDE = [
  {
    say: "Which load is this? Where was it going?",
    expect: "GG-1042, from Oakland to Sacramento, California. You’re the Sierra Valley Transport dispatcher.",
  },
  {
    say: "What was the appointment time and what are you disputing?",
    expect: "Delivery was October 23 at 9:00 a.m. PDT. The carrier billed $250 detention; the provisional calculation is $125, pending signed backup.",
  },
  {
    say: "Driver checked in at 8:45 and left at 12:15. We billed $250 detention.",
    expect: "Two free hours start at the 9:00 appointment. From 11:00 to 12:15 is 75 billable minutes at $100/hour: $125. Ask for the signed detention receipt.",
  },
  {
    say: "What paperwork do you already have?",
    expect: "The signed proof of delivery, signed rate confirmation, and an unsigned detention log are present. The signed detention receipt is missing.",
  },
  {
    say: "Where can I send it?",
    expect: "No email or upload channel is configured for this demo. The agent should acknowledge that and never invent an address or link.",
  },
  {
    say: "Has this invoice been paid?",
    expect: "The agent should check the payment record and report the $1,225 invoice as unpaid, without promising or approving payment.",
  },
];

export function DemoPhone() {
  const [client] = useState(() => new DemoPhoneClient({
    fetcher: (url, options) => fetch(url, options),
    storage: () => window.sessionStorage,
    uuid: () => crypto.randomUUID(),
  }));
  const snapshot = useSyncExternalStore(client.subscribe, client.snapshot, client.serverSnapshot);
  const { status, loading, submitting, error } = snapshot;
  const action = demoPhoneAction(snapshot);
  const request = status?.request;

  useEffect(() => {
    const controller = new AbortController();
    void client.refresh(controller.signal);
    const timer = window.setInterval(() => void client.refresh(controller.signal), 3_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [client]);

  return (
    <section className="demo-phone panel" aria-labelledby="demo-phone-title">
      <div className="demo-phone-heading">
        <span className="demo-phone-icon"><Phone size={22} aria-hidden="true" /></span>
        <div>
          <span className="eyebrow">Dispatcher Outreach · Cartesia</span>
          <h2 id="demo-phone-title">Hear your agents at work.</h2>
          <p>A real AI demo call to your team phone. Demo calls can run outside business hours.</p>
        </div>
      </div>
      <div className="demo-phone-action">
        <div>
          <span className="demo-phone-label">Team phone</span>
          <strong>{status?.destination_suffix ? `••• ••• ${status.destination_suffix}` : "Checking configured phone…"}</strong>
        </div>
        <button
          type="button"
          className="button primary"
          disabled={action.disabled}
          onClick={() => void client.start()}
        >
          {submitting || (action.active && !action.retry) ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : <Phone size={16} aria-hidden="true" />}
          {action.label}
        </button>
      </div>
      <div className="demo-phone-status" role="status" aria-live="polite" aria-atomic="true">
        {loading ? <p>Checking call readiness…</p> : null}
        {!loading && status?.ready && !request ? <p>You can start another demo as soon as the previous call finishes.</p> : null}
        {!loading && status && !status.ready && !action.active ? <p>{status.reason || "Demo calling is not available yet."}</p> : null}
        {request ? (
          <div>
            <p className="demo-phone-progress">
              {action.terminal ? <Check size={14} aria-hidden="true" /> : null}
              {request.state === "uncertain" && !request.retryable
                ? "The call status is being reconciled. We’ll keep checking this request."
                : request.message || `Call ${(request.call_status || request.state).replaceAll("_", " ")}.`}
            </p>
            {action.terminal && status?.ready ? <p>You can start another demo right away with the button above.</p> : null}
          </div>
        ) : null}
        {action.retry && !error ? <p>Your saved call request is awaiting confirmation. Retry will use the same request.</p> : null}
        {error ? <p className="error-text">{error}</p> : null}
      </div>
      <details className="demo-phone-guide">
        <summary>Demo call guide <span>What to say</span></summary>
        <div className="demo-phone-guide-body">
          <span className="demo-phone-guide-badge">Fictional demo data</span>
          <p>You play the dispatcher at Sierra Valley Transport. Use these prompts in any order.</p>
          <dl className="demo-phone-guide-facts">
            <div><dt>Load</dt><dd>GG-1042 · Oakland → Sacramento, CA</dd></div>
            <div><dt>Delivery</dt><dd>October 23, 2026 · 9:00 a.m. PDT</dd></div>
            <div><dt>Freight</dt><dd>18 pallets of packaged food · dry van · 36,000 lb</dd></div>
            <div><dt>Invoice</dt><dd>$975 linehaul + $250 detention = $1,225 · unpaid</dd></div>
          </dl>
          <ol>
            {DEMO_GUIDE.map((line) => (
              <li key={line.say}>
                <strong>“{line.say}”</strong>
                <p><span className="demo-phone-guide-label">Listen for:</span> {line.expect}</p>
              </li>
            ))}
          </ol>
          <p className="demo-phone-guide-note">This call gathers facts and requests missing evidence. It does not approve or send a payment.</p>
        </div>
      </details>
    </section>
  );
}
