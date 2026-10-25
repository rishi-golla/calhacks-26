"use client";
import { useEffect, useRef, useState } from "react";
import { Zap } from "lucide-react";
import { useOps } from "../data-provider";
import { BURST_MAX, type BurstResult } from "@/lib/demo-burst-proxy";

/** Sends a real burst of synthetic loads through ingest, the router and the agents. Only rendered when the
 *  local operator control is configured (DEMO_BURST_ENABLED and the server-only token). */
export default function BurstControl() {
  const { simulated } = useOps();
  const [enabled, setEnabled] = useState(false);
  const [open, setOpen] = useState(false);
  const [count, setCount] = useState(100);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<BurstResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let cancelled = false;
    fetch("/api/demo-burst", { cache: "no-store" })
      .then((r) => r.json())
      .then((body) => { if (!cancelled) setEnabled(body?.enabled === true); })
      .catch(() => { if (!cancelled) setEnabled(false); });
    return () => { cancelled = true; };
  }, []);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  if (!enabled) return null;
  const send = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const response = await fetch("/api/demo-burst", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ count }),
      });
      const body = await response.json();
      if (!response.ok) setError(typeof body?.error === "string" ? body.error : "The burst was not accepted.");
      else setResult(body as BurstResult);
    } catch {
      setError("The dashboard could not reach its burst route.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button className="fl-btn quiet small" onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-haspopup="dialog" title="Announce synthetic loads at once, each fast-forwarded to its carrier invoice, through Redis, ingest, the router and the real agents">
        <Zap size={14} /> Burst
      </button>
      {open && (
        <div className="fl-popover" role="dialog" aria-label="Send a burst">
          <label className="fl-micro" style={{ display: "flex", justifyContent: "space-between" }}>
            <span>Loads</span>
            <span className="fl-mono">{count}</span>
          </label>
          <input type="range" min={1} max={BURST_MAX} step={1} value={count} onChange={(e) => setCount(Number(e.target.value))} disabled={busy} style={{ width: "100%", padding: 0, background: "transparent", border: 0 }} />
          <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
            {[10, 50, 100, 200].map((n) => (
              <button key={n} className={`fl-tab ${count === n ? "active" : ""}`} onClick={() => setCount(n)} disabled={busy}>{n}</button>
            ))}
          </div>
          <button className="fl-btn small" style={{ marginTop: 12 }} onClick={send} disabled={busy}>
            <Zap size={12} /> {busy ? "Announcing…" : `Send ${count} loads`}
          </button>
          {simulated && (
            <p className="fl-micro" style={{ margin: "10px 0 0", color: "var(--orange)" }}>
              Showing the simulation. Switch to Live to watch.
            </p>
          )}
          {result && (
            <p className="fl-micro" style={{ margin: "10px 0 0" }} title={result.case_ids[0] ? `First case ${result.case_ids[0]}` : undefined}>
              Sent {result.count} in {result.announced_in_s.toFixed(1)}s
            </p>
          )}
          {error && (
            <p className="fl-micro" style={{ margin: "10px 0 0", color: "var(--orange)" }}>{error}</p>
          )}
        </div>
      )}
    </div>
  );
}
