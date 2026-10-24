"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { Check, LoaderCircle, Phone } from "lucide-react";
import { DemoPhoneClient, demoPhoneAction } from "@/lib/demo-phone";
import "./demo-phone.css";

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
    </section>
  );
}
