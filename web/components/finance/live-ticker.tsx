"use client";
/**
 * Live ledger ticker: a vertical feed of the money-relevant things agents just
 * did — new cases, holds, approvals, failures, payment proposals and posted
 * journal entries — newest on top, last 30 kept.
 *
 * Sources: `useLiveEvents` deltas, plus a local diff of `payment_proposal` and
 * `ledger_entry` (the shared event union carries no amounts for those). Only
 * rows stamped within the last two minutes count as news, so a first load or a
 * source switch shows recent activity instead of replaying history.
 *
 * New rows push the list down with a transform-only FLIP. Hovering pauses the
 * feed so a row can be read; incoming rows wait in a buffer.
 */
import Link from "next/link";
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { useLiveEvents, useOps } from "@/components/data-provider";
import { Mascot } from "@/components/mascot";
import { agentColor, skillLabel } from "@/lib/agents";
import { json, num, str, timestamp, type Row, type Snapshot } from "@/lib/data";
import { fmtAgo, type CaseView, type LedgerLine, type LiveEvent } from "@/lib/ops";
import { clock, emptyNote, nick, usd } from "./format";

type Tone = "ok" | "hot" | "warn" | "info" | "bad";
type Tick = { id: string; at: number; agent: string; text: string; amount?: number; signed?: boolean; tone: Tone; caseId?: string };

const KEEP = 30;
const RECENT_MS = 120_000;
const CASH = new Set(["cash", "bank"]);

const isNews = (at: number, t: number) => at > 0 && t - at <= RECENT_MS && at - t <= 60_000;

function companyName(snapshot: Snapshot, id: unknown) {
  return str(snapshot.company.find((c) => c.id === id)?.name, str(id, "carrier"));
}

function describe(e: LiveEvent, snapshot: Snapshot, cases: Map<string, CaseView>): Tick | null {
  const base = `${e.type}:${e.caseId}:${e.at}`;
  switch (e.type) {
    case "case_opened": {
      const c = cases.get(e.caseId);
      return { id: base, at: e.at, agent: "intake", text: `New case · ${c?.title ?? e.caseId}`, amount: c?.exposureUsd || undefined, tone: "info", caseId: e.caseId };
    }
    case "task_finished": {
      if (e.state !== "failed" && e.state !== "input-required") return null;
      const skill = skillLabel(str(snapshot.task.find((r) => r.task_id === e.taskId)?.skill, "task"));
      return e.state === "failed"
        ? { id: `${base}:${e.taskId}`, at: e.at, agent: e.agent, text: `${skill} failed`, tone: "bad", caseId: e.caseId }
        : { id: `${base}:${e.taskId}`, at: e.at, agent: e.agent, text: `${skill} is waiting on a human`, tone: "warn", caseId: e.caseId };
    }
    case "hold": {
      const holds = snapshot.hold.filter((h) => h.case_id === e.caseId);
      const h = e.status === "placed" ? (holds.find((x) => ["active", "held"].includes(str(x.status))) ?? holds.at(-1)) : holds.at(-1);
      const amount = h ? Math.max(0, num(h.amount) - num(h.released_amount)) : 0;
      return e.status === "placed"
        ? { id: base, at: e.at, agent: e.agent || "controller", text: `Placed a ${amount ? usd(amount) + " " : ""}hold on ${str(h?.invoice_id, e.caseId)}`, tone: "hot", caseId: e.caseId }
        : { id: base, at: e.at, agent: e.agent || "controller", text: `Released hold on ${str(h?.invoice_id, e.caseId)}`, amount: num(h?.amount) || undefined, tone: "ok", caseId: e.caseId };
    }
    case "approval":
      return e.decision === "pending"
        ? { id: `${base}:pending`, at: e.at, agent: e.agent || "controller", text: "Asked a human to approve", amount: e.amount || undefined, tone: "hot", caseId: e.caseId }
        : { id: `${base}:${e.decision}`, at: e.at, agent: "human", text: `${e.decision === "approved" ? "Approved" : "Rejected"} the request`, amount: e.amount || undefined, tone: e.decision === "approved" ? "ok" : "bad", caseId: e.caseId };
    case "call":
      if (e.status === "no_answer") return { id: `${base}:${e.status}`, at: e.at, agent: "voice_desk", text: "Call not answered", tone: "warn", caseId: e.caseId };
      if (e.status === "completed") return { id: `${base}:${e.status}`, at: e.at, agent: "voice_desk", text: "Call completed", tone: "info", caseId: e.caseId };
      return null;
    case "review":
      return e.verdict === "issue" ? { id: base, at: e.at, agent: "validator", text: "Quality review flagged an issue", tone: "warn", caseId: e.caseId } : null;
    default:
      return null;
  }
}

function proposalTick(r: Row, status: string, at: number, snapshot: Snapshot): Tick {
  const carrier = companyName(snapshot, r.carrier_id);
  const id = `pp:${str(r.id)}:${status}`;
  const caseId = str(r.case_id, "");
  if (status === "sent" || status === "paid")
    return { id, at, agent: "carrier_ap", text: `Payment sent to ${carrier}`, amount: num(r.amount), tone: "ok", caseId };
  if (status === "awaiting_approval")
    return { id, at, agent: str(r.proposed_by, "carrier_ap"), text: `Proposed paying ${carrier} · needs approval`, amount: num(r.amount), tone: "hot", caseId };
  return { id, at, agent: str(r.proposed_by, "carrier_ap"), text: `Proposed paying ${carrier}`, amount: num(r.amount), tone: "info", caseId };
}

function ledgerTick(r: Row, at: number): Tick {
  const lines = (Array.isArray(r.lines) ? r.lines : json<LedgerLine[]>(r.lines, [])) as LedgerLine[];
  let cash = 0;
  let total = 0;
  for (const l of lines) {
    total += num(l.debit);
    if (CASH.has(str(l.account, "").toLowerCase())) cash += num(l.debit) - num(l.credit);
  }
  return {
    id: `je:${str(r.id)}`,
    at,
    agent: str(r.requested_by, "controller"),
    text: str(r.memo, "Journal entry posted"),
    amount: cash || total,
    signed: cash !== 0,
    tone: cash > 0 ? "ok" : cash < 0 ? "hot" : "info",
    caseId: str(r.case_id, ""),
  };
}

const merge = (prev: Tick[], incoming: Tick[]) => {
  const seen = new Set(prev.map((t) => t.id));
  const fresh = incoming.filter((t) => !seen.has(t.id) && (seen.add(t.id), true));
  if (!fresh.length) return prev;
  return [...fresh, ...prev].sort((a, b) => b.at - a.at).slice(0, KEEP);
};

export function LiveTicker() {
  const { snapshot, cases, simulated, source, now } = useOps();
  const [ticks, setTicks] = useState<Tick[]>([]);
  const [paused, setPaused] = useState(false);
  const buffer = useRef<Tick[]>([]);
  const [buffered, setBuffered] = useState(0);
  const pausedRef = useRef(false);
  pausedRef.current = paused;

  // Arrival times of every accepted item (not capped at KEEP) for the rate readout.
  const arrivals = useRef<number[]>([]);
  const push = (incoming: Tick[]) => {
    if (!incoming.length) return;
    const t = Date.now();
    arrivals.current = [...arrivals.current.filter((a) => t - a <= 60_000), ...incoming.map((i) => i.at)];
    if (pausedRef.current) {
      buffer.current = merge(buffer.current, incoming);
      setBuffered(buffer.current.length);
    } else setTicks((prev) => merge(prev, incoming));
  };

  useLiveEvents((events) => {
    const t = Date.now();
    const out: Tick[] = [];
    for (const e of events) {
      if (!isNews(e.at, t)) continue;
      const tick = describe(e, snapshot, cases);
      if (tick) out.push(tick);
    }
    push(out);
  });

  // Local diff of proposals (by status) and journal entries (by id).
  const baseline = useRef<{ pp: Map<string, string>; je: Set<string> } | null>(null);
  useEffect(() => {
    baseline.current = null;
    arrivals.current = [];
    setTicks([]);
    buffer.current = [];
    setBuffered(0);
  }, [simulated]);
  useEffect(() => {
    const prev = baseline.current;
    const pp = new Map(snapshot.payment_proposal.map((r) => [str(r.id), str(r.status, "")]));
    const je = new Set(snapshot.ledger_entry.map((r) => str(r.id)));
    baseline.current = { pp, je };
    const t = Date.now();
    const out: Tick[] = [];
    for (const r of snapshot.payment_proposal) {
      const status = str(r.status, "");
      const before = prev?.pp.get(str(r.id));
      if (before === status) continue;
      const at = before ? t : timestamp(r.proposed_at);
      if (isNews(at, t)) out.push(proposalTick(r, status, at, snapshot));
    }
    for (const r of snapshot.ledger_entry) {
      if (prev?.je.has(str(r.id))) continue;
      const at = timestamp(r.posted_at);
      if (isNews(at, t)) out.push(ledgerTick(r, Math.min(at, t)));
    }
    push(out);
    // push is recreated each render but only reads refs and setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot.payment_proposal, snapshot.ledger_entry]);

  const resume = () => {
    setPaused(false);
    if (buffer.current.length) {
      const pending = buffer.current;
      buffer.current = [];
      setBuffered(0);
      setTicks((prev) => merge(prev, pending));
    }
  };

  // FLIP: when rows are prepended, start the list shifted up by their height and slide to 0.
  const list = useRef<HTMLOListElement>(null);
  const head = useRef<string | null>(null);
  useLayoutEffect(() => {
    const el = list.current;
    const prevHead = head.current;
    head.current = ticks[0]?.id ?? null;
    if (!el || !prevHead || prevHead === head.current) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let shift = 0;
    for (const child of Array.from(el.children) as HTMLElement[]) {
      if (child.dataset.id === prevHead) break;
      shift += child.offsetHeight;
    }
    if (shift > 0) el.animate([{ transform: `translateY(-${shift}px)` }, { transform: "translateY(0)" }], { duration: 520, easing: "cubic-bezier(.23,1,.32,1)" });
  }, [ticks]);

  const perMinute = arrivals.current.filter((a) => now - a <= 60_000).length;

  return (
    <section className="fl-panel fh-ticker" aria-label="Live ledger and activity">
      <header className="fl-panel-head">
        <span className="fl-section" style={{ "--accent": "var(--teal)" } as CSSProperties}>
          Live ledger
        </span>
        <span className="fh-head-meta">
          {paused ? (
            <span className="fl-chip outline warn">Paused{buffered ? ` · ${buffered} new` : ""}</span>
          ) : (
            <span className="fl-micro fh-live">
              <i className="fl-dot pulse" /> {perMinute}/min
            </span>
          )}
        </span>
      </header>
      <div className="fh-ticker-body" onPointerEnter={() => setPaused(true)} onPointerLeave={resume}>
        {ticks.length === 0 ? (
          <div className="fh-empty">
            <Mascot id="intake" mood="sleeping" size={52} />
            <div>
              <div className="fl-label strong">{emptyNote(source, "Quiet for now")}</div>
              <div className="fh-empty-sub">New holds, approvals, payments and journal entries appear here as they happen.</div>
            </div>
          </div>
        ) : (
          <ol className="fh-ticker-list" ref={list} aria-live="polite" aria-relevant="additions">
            {ticks.map((t) => (
              <li key={t.id} data-id={t.id} className={`fh-tick tone-${t.tone}`} style={{ "--agent": agentColor(t.agent) } as CSSProperties}>
                <time className="fh-tick-time" dateTime={new Date(t.at).toISOString()} title={fmtAgo(t.at, now)} suppressHydrationWarning>
                  {clock(t.at)}
                </time>
                <span className="fh-tick-agent">{nick(t.agent)}</span>
                {t.caseId ? (
                  <Link href={`/cases/${encodeURIComponent(t.caseId)}`} className="fh-tick-text" title={`${t.text} · ${t.caseId}`}>
                    {t.text}
                  </Link>
                ) : (
                  <span className="fh-tick-text">{t.text}</span>
                )}
                {t.amount != null && t.amount !== 0 && (
                  <span className="fh-tick-amt fl-num">
                    {t.signed ? (t.amount > 0 ? "+" : "−") : ""}
                    {usd(Math.abs(t.amount))}
                  </span>
                )}
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}
