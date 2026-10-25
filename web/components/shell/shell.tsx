"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Activity, Bell, Database, FlaskConical, Home, ListTree, Network, Play, Receipt, RefreshCw, Zap } from "lucide-react";
import { useOps, type DataMode } from "../data-provider";
import { agentLabel } from "@/lib/agents";
import { fmtAgo, fmtMoney, type AttentionItem } from "@/lib/ops";
import { Mascot } from "../mascot";
import BurstControl from "./burst-control";
import Intro from "./intro";
import { StartFresh } from "./view-epoch";

const NAV = [
  { href: "/", label: "Finance", icon: Home, exact: true },
  { href: "/invoices", label: "Invoices", icon: Receipt },
  { href: "/operations", label: "Operations", icon: Activity },
  { href: "/agents", label: "Agents", icon: Network },
  { href: "/cases", label: "Cases", icon: ListTree },
  { href: "/demo", label: "Demo", icon: Play },
];

export default function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { ops } = useOps();
  const [ready, setReady] = useState(false);
  const title = NAV.find((n) => (n.exact ? pathname === n.href : pathname.startsWith(n.href)))?.label ?? "FreightLine";
  return (
    <>
      {/* The intro keeps the original Senra look, so it sits outside the themed wrapper. */}
      <div className="fl fl-intro-host">
        <Intro onDone={() => setReady(true)} />
      </div>
      <div className={`fl theme-mono theme-glass ${ready ? "" : "is-holding"}`}>
        <div className="fl-shell">
          <aside className="fl-rail">
            <Link href="/" className="fl-brand" aria-label="FreightLine home">
              <span className="fl-brand-name">FreightLine</span>
            </Link>
            <nav className="fl-nav" aria-label="Main">
              {NAV.map((item) => {
                const active = item.exact ? pathname === item.href : pathname.startsWith(item.href);
                return (
                  <Link key={item.href} href={item.href} className={`fl-nav-item ${active ? "active" : ""}`} aria-current={active ? "page" : undefined}>
                    <item.icon size={15} />
                    {item.label}
                    {item.href === "/" && ops.approvalsPending > 0 && <span className="fl-count">{ops.approvalsPending}</span>}
                  </Link>
                );
              })}
            </nav>
          </aside>
          <header className="fl-topbar">
            <span className="fl-crumb">
              <strong>{title}</strong>
            </span>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Notifications />
              {pathname.startsWith("/demo") && <StartFresh />}
              <BurstControl />
              <SourcePill />
            </div>
          </header>
          <main className="fl-main">{children}</main>
        </div>
      </div>
    </>
  );
}

/** Closes a popover on outside click. */
function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", key);
    };
  }, [open]);
  return { open, setOpen, ref };
}

const KIND_LABEL: Record<AttentionItem["kind"], string> = {
  approval_pending: "Approve",
  task_failed: "Failed",
  input_required: "Input",
  flag_open: "Flag",
  review_issue: "Review",
  hold_active: "Hold",
  call_no_answer: "No answer",
  policy_deny: "Blocked",
  stale_agent: "Stale",
};

/** Bell: the items that need a person, newest critical first, linking to their case. */
function Notifications() {
  const { attention, now } = useOps();
  const { open, setOpen, ref } = usePopover();
  const urgent = attention.filter((a) => a.severity === "critical" || a.severity === "high");
  const items = attention.slice(0, 8);
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button className="fl-btn quiet small" onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-haspopup="dialog" aria-label={`${urgent.length} items need attention`}>
        <Bell size={14} />
        {urgent.length > 0 && (
          <span className="fl-num" style={{ color: "var(--orange)" }}>
            {urgent.length}
          </span>
        )}
      </button>
      {open && (
        <div className="fl-popover" role="dialog" aria-label="Needs attention" style={{ width: 360, padding: 0 }}>
          <div className="fl-panel-head" style={{ padding: "10px 12px" }}>
            <span className="fl-section">Needs attention</span>
            <span className="fl-micro">{attention.length}</span>
          </div>
          {items.length === 0 ? (
            <p className="fl-dim" style={{ padding: 14, fontSize: 13 }}>
              All clear.
            </p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {items.map((item) => (
                <li key={item.id} style={{ borderTop: "1px solid var(--line-soft)" }}>
                  <Link
                    href={item.caseId ? `/cases/${encodeURIComponent(item.caseId)}` : "/"}
                    onClick={() => setOpen(false)}
                    style={{ display: "grid", gridTemplateColumns: "28px 1fr auto", gap: 10, alignItems: "center", padding: "9px 12px" }}
                    className="fl-notif"
                  >
                    <Mascot id={item.agent === "human" ? "human" : item.agent} size={28} mood={item.severity === "critical" || item.kind === "task_failed" ? "alert" : "idle"} />
                    <span style={{ minWidth: 0 }}>
                      <span style={{ display: "flex", gap: 6, alignItems: "baseline" }}>
                        <span className={`fl-chip outline ${item.severity === "critical" || item.severity === "high" ? "hot" : ""}`} style={{ fontSize: 10, padding: "1px 5px" }}>
                          {KIND_LABEL[item.kind]}
                        </span>
                        <span style={{ fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.title}</span>
                      </span>
                      <span className="fl-micro" style={{ display: "block", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {item.caseId || agentLabel(item.agent)} · {fmtAgo(item.at, now)}
                      </span>
                    </span>
                    <span className="fl-num" style={{ fontSize: 13 }}>
                      {item.amount ? fmtMoney(item.amount) : ""}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
          {attention.length > items.length && (
            <Link href="/?focus=attention" onClick={() => setOpen(false)} className="fl-btn quiet small" style={{ display: "flex", justifyContent: "center", borderTop: "1px solid var(--line-soft)", borderRadius: 0 }}>
              View all {attention.length}
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

const MODES: { id: DataMode; label: string }[] = [
  { id: "auto", label: "Auto" },
  { id: "live", label: "Live" },
  { id: "sim", label: "Simulation" },
];

function SourcePill() {
  const { source, mode, setMode, lastUpdated, now, serverUrl, databaseName, error, reconnect, sim, simulated } = useOps();
  const { open, setOpen, ref } = usePopover();
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
          <div className="fl-tabs" style={{ marginBottom: 10 }}>
            {MODES.map((m) => (
              <button key={m.id} className={`fl-tab ${mode === m.id ? "active" : ""}`} onClick={() => setMode(m.id)}>
                {m.label}
              </button>
            ))}
          </div>
          <div className="fl-micro" style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{simulated ? "Simulated · not recorded" : `${databaseName} · ${serverUrl}`}</span>
            <span>{lastUpdated ? fmtAgo(lastUpdated, now) : "no data"}</span>
          </div>
          {error && !simulated && (
            <p style={{ margin: "8px 0 0", fontSize: 12, color: "var(--orange)" }}>{error}</p>
          )}
          {simulated ? (
            <div style={{ marginTop: 12 }}>
              <label className="fl-micro" style={{ display: "flex", justifyContent: "space-between" }}>
                <span>Rate</span>
                <span className="fl-mono">{sim.ratePerMinute}/min</span>
              </label>
              <input type="range" min={0} max={120} step={1} value={sim.ratePerMinute} onChange={(e) => sim.setRate(Number(e.target.value))} style={{ width: "100%", padding: 0, background: "transparent", border: 0 }} />
              <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
                <button className="fl-btn small" onClick={() => sim.inject("detention_unsupported")}>
                  <Zap size={12} /> Detention
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
          <Link href="/ops" className="fl-micro" style={{ display: "block", marginTop: 12, textDecoration: "underline", textUnderlineOffset: 3 }} onClick={() => setOpen(false)}>
            Admin console
          </Link>
        </div>
      )}
    </div>
  );
}
