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
import Link from "next/link";
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
  { id: "phone", name: "Detention charge", channel: "phone" as const, sim: "detention_unsupported", text: "Carrier bills two hours of detention; GPS says 47 minutes." },
  { id: "bank_change", name: "Bank change", channel: "email" as const, sim: "bank_change", text: "New bank details arrive the day before a payment." },
  { id: "short_pay", name: "Short payment", channel: "phone" as const, sim: "shipper_short_pay", text: "A shipper pays less than the invoice." },
  { id: "scenario", name: "Your scenario", channel: "phone" as const, sim: undefined, text: "Describe a situation; the team works it live." },
];

const GUIDE = [
  { say: "Which load is this? Where was it going?", expect: "GG-1042, Oakland to Sacramento. You are the Sierra Valley Transport dispatcher." },
  { say: "What are you disputing?", expect: "$250 detention billed; the provisional calculation is $125 pending a signed receipt." },
  { say: "Driver checked in at 8:45 and left at 12:15.", expect: "Two free hours from the 9:00 appointment, then 75 billable minutes at $100/h: $125. It asks for the signed receipt." },
  { say: "What paperwork do you have?", expect: "Signed POD and rate confirmation; the signed detention receipt is missing." },
  { say: "Where can I send it?", expect: "No channel is configured; it says so and never invents an address." },
  { say: "Has this invoice been paid?", expect: "Reports the $1,225 invoice as unpaid without promising payment." },
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
            <span className="fl-micro">{gateway ? "Gateway connected" : simulated ? "Runs in simulation" : "Gateway not connected"}</span>
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
                  <span className="fl-micro">Teammate playing the counterparty</span>
                  <select required value={chosenTarget} onChange={(e) => setTarget(e.target.value)}>
                    <option value="">Select a team destination</option>
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
                <textarea required={gateway} minLength={10} maxLength={4000} rows={4} value={scenario} onChange={(e) => setScenario(e.target.value)} placeholder="A freight finance situation, the conflicting evidence, and what to investigate" />
              </label>
            )}
            <div className="dm-actions">
              {gateway ? (
                <span className="fl-micro">{channel === "phone" ? "Rings the selected team phone" : "Emails the selected team inbox"}</span>
              ) : canSimulate ? (
                <span className="fl-micro">Starts a simulated case and opens its trace</span>
              ) : (
                <span className="fl-micro">{configError || (kind === "scenario" ? "Custom scenarios need the gateway" : "Enable the gateway to run this")}</span>
              )}
              <button type="submit" className="fl-btn primary" disabled={busy || (!gateway && !canSimulate) || (gateway && !chosenTarget)}>
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
      <p className="fl-micro dm-foot">
        Outreach only reaches the team allowlist. <Link href="/ops">Admin console</Link>
      </p>
    </div>
  );
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
  const line = loading
    ? "Checking…"
    : request
      ? request.state === "uncertain" && !request.retryable
        ? "Reconciling call status…"
        : request.message || `Call ${(request.call_status || request.state).replaceAll("_", " ")}`
      : status && !status.ready && !action.active
        ? status.reason || "Not available yet"
        : status?.ready
          ? "Ready"
          : "";
  return (
    <section className="fl-panel dm-phone" aria-label="Demo call">
      <div className="fl-panel-head">
        <span className="fl-section">Call</span>
        <span className="fl-micro">Dispatcher Outreach · Cartesia</span>
      </div>
      <div className="dm-phone-body">
        <Mascot id="voice_desk" size={96} mood={action.active ? "talking" : action.terminal ? "success" : "idle"} />
        <div className="dm-phone-main">
          <div className="fl-micro">Team phone</div>
          <div className="fl-num dm-phone-number">{status?.destination_suffix ? `••• ••• ${status.destination_suffix}` : "—"}</div>
          <button type="button" className="fl-btn primary" disabled={action.disabled} onClick={() => void client.start()}>
            {submitting || (action.active && !action.retry) ? <LoaderCircle size={14} className="dm-spin" /> : <Phone size={14} />}
            {action.label}
          </button>
          <p className="dm-phone-status" role="status" aria-live="polite">
            {action.terminal ? <Check size={13} /> : null}
            {error || line}
          </p>
        </div>
      </div>
      <details className="dm-guide">
        <summary>
          <span className="fl-label strong">What to say</span>
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
