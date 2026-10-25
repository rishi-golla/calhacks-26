"use client";
/**
 * Demo console in the FreightLine shell.
 *
 * Two things a presenter needs: the live Cartesia call to the team phone
 * (same request/poll client as before, `lib/demo-phone.ts`), and scenario
 * injection. Scenarios go to the W4 gateway through `/api/demo` when it is
 * enabled; on the browser simulation they start a simulated case instead, so
 * the flow can be rehearsed without the gateway.
 */
import { useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore } from "react";
import { Check, ChevronDown, LoaderCircle, Phone, Play } from "lucide-react";
import { DemoPhoneClient, demoPhoneAction } from "@/lib/demo-phone";
import { str, type Row } from "@/lib/data";
import { useOps } from "@/components/data-provider";
import { Mascot } from "@/components/mascot";
import "./demo.css";

type DemoConfig = { enabled: boolean; targets: Record<"phone" | "email", string[]> };

const PRESETS = [
  { id: "phone", name: "Detention charge", channel: "phone" as const, sim: "detention_unsupported", text: "2h billed, GPS says 47 min" },
  { id: "bank_change", name: "Bank change", channel: "email" as const, sim: "bank_change", text: "New account pre-payment" },
  { id: "short_pay", name: "Short payment", channel: "phone" as const, sim: "shipper_short_pay", text: "Shipper underpays" },
  { id: "scenario", name: "Your scenario", channel: "phone" as const, sim: undefined, text: "Describe your own" },
];

const GUIDE = [
  { say: "Which load is this? Where was it going?", expect: "GG-1042, Oakland → Sacramento. You're the Sierra Valley Transport dispatcher." },
  { say: "What are you disputing?", expect: "$250 detention billed; $125 provisional, pending a signed receipt." },
  { say: "Driver checked in at 8:45 and left at 12:15.", expect: "75 billable min at $100/h = $125; asks for the signed receipt." },
  { say: "What paperwork do you have?", expect: "Signed POD and rate con; detention receipt missing." },
  { say: "Where can I send it?", expect: "No channel configured; never invents an address." },
  { say: "Has this invoice been paid?", expect: "$1,225 unpaid; no promise of payment." },
];

export default function DemoConsole() {
  const { snapshot, simulated, sim } = useOps();
  const router = useRouter();
  const [config, setConfig] = useState<DemoConfig | null>(null);
  const [configError, setConfigError] = useState("");
  const [kind, setKind] = useState("phone");
  const [channel, setChannel] = useState<"phone" | "email">("phone");
  const [caseId, setCaseId] = useState("");
  const [target, setTarget] = useState("");
  const [scenario, setScenario] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Row | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/demo", { signal: controller.signal })
      .then((r) => r.json())
      .then(setConfig)
      .catch((e) => {
        if (!controller.signal.aborted) setConfigError(String(e).replace(/^Error: /, ""));
      });
    return () => controller.abort();
  }, []);
  const gateway = Boolean(config?.enabled);
  const targets = config?.targets?.[channel] ?? [];
  const chosenTarget = targets.includes(target) ? target : (targets[0] ?? "");
  const preset = PRESETS.find((p) => p.id === kind)!;
  const canSimulate = simulated && kind !== "scenario";

  const start = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    setResult(null);
    if (!gateway) {
      if (canSimulate) {
        const id = sim.inject(preset.sim);
        if (id) router.push(`/cases/${encodeURIComponent(id)}`);
      }
      return;
    }
    setBusy(true);
    try {
      const response = await fetch("/api/demo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, channel, case_id: caseId, scenario, demo_target: chosenTarget }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error);
      setResult(body);
    } catch (e) {
      setError(String(e).replace(/^Error: /, ""));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fl-page dm">
      <div className="dm-grid">
        <PhoneCard />
        <section className="fl-panel" aria-label="Scenarios">
          <div className="fl-panel-head">
            <span className="fl-section">Scenario</span>
          </div>
          <div className="dm-presets" role="radiogroup" aria-label="Scenario presets">
            {PRESETS.map((p) => (
              <button
                key={p.id}
                role="radio"
                aria-checked={kind === p.id}
                className={`dm-preset ${kind === p.id ? "on" : ""}`}
                onClick={() => {
                  setKind(p.id);
                  setChannel(p.channel);
                }}
              >
                <strong>{p.name}</strong>
                <span>{p.text}</span>
              </button>
            ))}
          </div>
          <form className="dm-form" onSubmit={start}>
            {gateway && (
              <>
                <label>
                  <span className="fl-micro">Case</span>
                  <select value={caseId} onChange={(e) => setCaseId(e.target.value)}>
                    <option value="">Preset case</option>
                    {snapshot.case.map((row) => (
                      <option value={str(row.case_id)} key={str(row.case_id)}>
                        {str(row.case_id)} · {str(row.title)}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span className="fl-micro">Channel</span>
                  <select value={channel} onChange={(e) => setChannel(e.target.value as "phone" | "email")}>
                    <option value="phone">Phone</option>
                    <option value="email">Email</option>
                  </select>
                </label>
                <label className="full">
                  <span className="fl-micro">Counterparty</span>
                  <select required value={chosenTarget} onChange={(e) => setTarget(e.target.value)} title="Teammate playing the counterparty">
                    <option value="">Select teammate</option>
                    {targets.map((v) => (
                      <option key={v}>{v}</option>
                    ))}
                  </select>
                </label>
              </>
            )}
            {kind === "scenario" && (
              <label className="full">
                <span className="fl-micro">Scenario</span>
                <textarea required={gateway} minLength={10} maxLength={4000} rows={4} value={scenario} onChange={(e) => setScenario(e.target.value)} placeholder="Situation, evidence, what to check" />
              </label>
            )}
            <div className="dm-actions">
              {/* Only say something when Run cannot do what it looks like it does. */}
              <span className="fl-micro">{gateway ? "" : canSimulate ? "Simulated" : configError || "Gateway not connected"}</span>
              <button
                type="submit"
                className="fl-btn primary"
                disabled={busy || (!gateway && !canSimulate) || (gateway && !chosenTarget)}
                title={gateway ? (channel === "phone" ? "Rings the selected team phone" : "Emails the selected team inbox") : canSimulate ? "Starts a simulated case and opens its trace" : undefined}
              >
                {busy ? <LoaderCircle size={14} className="dm-spin" /> : <Play size={14} />}
                {gateway ? "Start" : "Run"}
              </button>
            </div>
          </form>
          {error && (
            <p className="dm-error" role="alert">
              {error}
            </p>
          )}
          {result && (
            <div className="dm-result">
              <p>{str(result.message)}</p>
              {result.stub ? <span className="fl-chip warn outline">Stub response</span> : null}
              <details>
                <summary className="fl-micro">Response</summary>
                <pre>{JSON.stringify(result, null, 2)}</pre>
              </details>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

const CALL_LINE: Record<string, string> = {
  completed: "Call ended",
  no_answer: "No answer",
  failed: "Call failed",
  cancelled: "Call canceled",
  canceled: "Call canceled",
  ringing: "Ringing",
  in_progress: "On the call",
  dialing: "Dialing",
};

function callLine(request: { state: string; call_status?: string; message?: string }) {
  // Only a provider call status maps to a short label; without a call the server's sentence is the news.
  const short = request.call_status ? CALL_LINE[request.call_status.toLowerCase()] : undefined;
  return short || request.message || `Call ${(request.call_status || request.state).replaceAll("_", " ")}`;
}

function PhoneCard() {
  const [client] = useState(
    () =>
      new DemoPhoneClient({
        fetcher: (url, options) => fetch(url, options),
        storage: () => window.sessionStorage,
        uuid: () => crypto.randomUUID(),
      }),
  );
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
  // A few words per state; the server's full sentence stays in the tooltip.
  const line = loading
    ? "Checking…"
    : request
      ? request.state === "uncertain" && !request.retryable
        ? "Checking call status…"
        : callLine(request)
      : status && !status.ready && !action.active
        ? status.reason || "Not available yet"
        : "";
  return (
    <section className="fl-panel dm-phone" aria-label="Demo call">
      <div className="fl-panel-head" title="Dispatcher Outreach · Cartesia">
        <span className="fl-section">Call</span>
      </div>
      <div className="dm-phone-body">
        <Mascot id="voice_desk" size={96} mood={action.active ? "talking" : action.terminal ? "success" : "idle"} />
        <div className="dm-phone-main">
          <div className="fl-num dm-phone-number" title="Team phone · outreach only reaches the team allowlist">
            {status?.destination_suffix ? `••• ••• ${status.destination_suffix}` : "—"}
          </div>
          <button type="button" className="fl-btn primary" disabled={action.disabled} onClick={() => void client.start()}>
            {submitting || (action.active && !action.retry) ? <LoaderCircle size={14} className="dm-spin" /> : <Phone size={14} />}
            {action.label}
          </button>
          <p className="dm-phone-status" role="status" aria-live="polite" title={error || request?.message || undefined}>
            {action.terminal ? <Check size={13} /> : null}
            {error || line}
          </p>
        </div>
      </div>
      <details className="dm-guide">
        <summary>
          <span className="fl-label strong">Script</span>
          <ChevronDown size={14} />
        </summary>
        <dl className="dm-facts">
          <div>
            <dt>Load</dt>
            <dd>GG-1042 · Oakland → Sacramento</dd>
          </div>
          <div>
            <dt>Delivery</dt>
            <dd>Oct 23 · 9:00 am</dd>
          </div>
          <div>
            <dt>Invoice</dt>
            <dd>$975 + $250 detention = $1,225 · unpaid</dd>
          </div>
        </dl>
        <ol>
          {GUIDE.map((g) => (
            <li key={g.say}>
              <strong>“{g.say}”</strong>
              <span>{g.expect}</span>
            </li>
          ))}
        </ol>
      </details>
    </section>
  );
}
