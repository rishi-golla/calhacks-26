"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Activity, Bell, Database, FlaskConical, Home, ListTree, Network, Receipt, RefreshCw, Table2, Zap } from "lucide-react";
import { useOps, type DataMode } from "../data-provider";
import { fmtAgo } from "@/lib/ops";
import { Mascot } from "../mascot";
import Intro from "./intro";

const NAV = [
  { href: "/", label: "Finance", icon: Home, exact: true },
  { href: "/invoices", label: "Invoices", icon: Receipt },
  { href: "/operations", label: "Operations", icon: Activity },
  { href: "/agents", label: "Agents", icon: Network },
  { href: "/cases", label: "Cases", icon: ListTree },
];
const LEGACY = [{ href: "/ops", label: "Ops console", icon: Table2 }];

export default function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { ops } = useOps();
  const [ready, setReady] = useState(false);
  const title = NAV.find((n) => (n.exact ? pathname === n.href : pathname.startsWith(n.href)))?.label ?? "FreightLine";
  return (
    <div className="fl theme-mono">
      <Intro onDone={() => setReady(true)} />
      <div className="fl-shell" style={{ opacity: ready ? 1 : 0, transition: "opacity 400ms var(--ease-out)" }}>
        <aside className="fl-rail">
          <Link href="/" className="fl-brand" aria-label="FreightLine home">
            <span className="fl-brand-mark" aria-hidden>
              <i />
              <i />
              <i />
            </span>
            <span className="fl-brand-name">
              FreightLine
              <small>GOLDEN GATE FREIGHT</small>
            </span>
          </Link>
          <nav className="fl-nav" aria-label="Main">
            <span className="fl-label fl-nav-group">Operations</span>
            {NAV.map((item) => {
              const active = item.exact ? pathname === item.href : pathname.startsWith(item.href);
              return (
                <Link key={item.href} href={item.href} className={`fl-nav-item ${active ? "active" : ""}`} aria-current={active ? "page" : undefined}>
                  <item.icon size={15} />
                  {item.label}
                  {item.href === "/" && ops.casesNeedingAttention > 0 && <span className="fl-count">{ops.casesNeedingAttention}</span>}
                  {item.href === "/agents" && <span className="fl-micro" style={{ marginLeft: "auto" }}>{ops.tasksActive} live</span>}
                </Link>
              );
            })}
            <span className="fl-label fl-nav-group">Records</span>
            {LEGACY.map((item) => (
              <Link key={item.href} href={item.href} className="fl-nav-item">
                <item.icon size={15} />
                {item.label}
              </Link>
            ))}
          </nav>
          <RailFoot />
        </aside>
        <header className="fl-topbar">
          <span className="fl-crumb">
            FreightLine <span style={{ color: "var(--text-mute)" }}>/</span> <strong>{title}</strong>
          </span>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Link href="/?focus=attention" className="fl-btn quiet small" aria-label={`${ops.approvalsPending} pending approvals`}>
              <Bell size={14} />
              {ops.approvalsPending > 0 && <span className="fl-num" style={{ color: "var(--orange)" }}>{ops.approvalsPending}</span>}
            </Link>
            <SourcePill />
          </div>
        </header>
        <main className="fl-main">{children}</main>
      </div>
    </div>
  );
}

function RailFoot() {
  const { ops, workloads } = useOps();
  const working = Object.values(workloads).filter((w) => w.status === "working");
  return (
    <div className="fl-rail-foot">
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Mascot id="intake" size={34} mood={ops.tasksActive ? "working" : "idle"} />
        <div style={{ minWidth: 0 }}>
          <div className="fl-label strong" style={{ fontSize: 11 }}>
            {working.length}/{Object.keys(workloads).length} agents busy
          </div>
          <div className="fl-micro">{ops.tasksActive} tasks · {ops.casesActive} open cases</div>
        </div>
      </div>
      <div className="fl-micro" style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <Activity size={11} /> Operational truth in SpacetimeDB
      </div>
    </div>
  );
}

const MODES: { id: DataMode; label: string; hint: string }[] = [
  { id: "auto", label: "Auto", hint: "Database when available, otherwise simulation" },
  { id: "live", label: "Live DB", hint: "SpacetimeDB only; empty when offline" },
  { id: "sim", label: "Simulation", hint: "Browser-generated activity, clearly labeled" },
];

function SourcePill() {
  const { source, mode, setMode, lastUpdated, now, serverUrl, databaseName, error, reconnect, sim, simulated } = useOps();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  const label =
    source === "live" ? "Live" : source === "simulation" ? "Simulation" : source === "fallback" ? "Snapshot" : source === "connecting" ? "Connecting" : "Offline";
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button className={`fl-source ${source}`} onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-haspopup="dialog">
        <i className={`fl-dot ${source === "live" || source === "simulation" ? "pulse" : ""}`} />
        {label}
        {source === "simulation" ? <FlaskConical size={13} /> : <Database size={13} />}
      </button>
      {open && (
        <div className="fl-popover" role="dialog" aria-label="Data source">
          <div className="fl-section" style={{ marginBottom: 10 }}>
            Data source
          </div>
          <div className="fl-tabs" style={{ marginBottom: 10 }}>
            {MODES.map((m) => (
              <button key={m.id} className={`fl-tab ${mode === m.id ? "active" : ""}`} onClick={() => setMode(m.id)} title={m.hint}>
                {m.label}
              </button>
            ))}
          </div>
          <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 12px", margin: 0, fontSize: 12 }}>
            <dt className="fl-micro">Now</dt>
            <dd style={{ margin: 0 }}>
              {simulated ? "Simulated in this browser. No rows are written anywhere." : `${databaseName} on ${serverUrl}`}
            </dd>
            <dt className="fl-micro">Updated</dt>
            <dd style={{ margin: 0 }} className="fl-mono">
              {lastUpdated ? fmtAgo(lastUpdated, now) : "never"}
            </dd>
            {error && !simulated && (
              <>
                <dt className="fl-micro">Error</dt>
                <dd style={{ margin: 0, color: "var(--orange)" }}>{error}</dd>
              </>
            )}
          </dl>
          {simulated ? (
            <div style={{ marginTop: 12 }}>
              <label className="fl-micro" style={{ display: "flex", justifyContent: "space-between" }}>
                <span>Arrival rate</span>
                <span className="fl-mono">{sim.ratePerMinute}/min</span>
              </label>
              <input type="range" min={0} max={120} step={1} value={sim.ratePerMinute} onChange={(e) => sim.setRate(Number(e.target.value))} style={{ width: "100%", padding: 0, background: "transparent", border: 0 }} />
              <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
                <button className="fl-btn small" onClick={() => sim.inject("detention_unsupported")}>
                  <Zap size={12} /> Detention case
                </button>
                <button className="fl-btn small" onClick={() => sim.inject("bank_change")}>
                  <Zap size={12} /> Bank change
                </button>
                <button className="fl-btn small" onClick={() => sim.inject()}>
                  <Zap size={12} /> Random
                </button>
              </div>
            </div>
          ) : (
            <button className="fl-btn small" style={{ marginTop: 12 }} onClick={reconnect}>
              <RefreshCw size={12} /> Reconnect
            </button>
          )}
        </div>
      )}
    </div>
  );
}
