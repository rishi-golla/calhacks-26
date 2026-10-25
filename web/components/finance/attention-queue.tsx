"use client";
/**
 * Needs-attention queue: everything that is waiting on a person, grouped by
 * kind with counts, severity-colored, each row linking to its case. Approvals
 * can be decided inline on the live database (one-time code required, as in
 * the ops console); on simulation or read-only snapshots the buttons stay
 * visible but disabled with the reason in the tooltip.
 */
import Link from "next/link";
import { useMemo, useState, type CSSProperties, type FormEvent } from "react";
import { Check, X } from "lucide-react";
import { useOps } from "@/components/data-provider";
import { Mascot, moodForStatus, type Mood } from "@/components/mascot";
import { isAgentId } from "@/lib/agents";
import { str } from "@/lib/data";
import { fmtAgo, type AttentionItem } from "@/lib/ops";
import { emptyNote, nick, usd } from "./format";
import { useFresh, useSettled } from "./fresh";

type Kind = AttentionItem["kind"];

const KINDS: { kind: Kind; tag: string; group: string }[] = [
  { kind: "approval_pending", tag: "Approve", group: "Approvals" },
  { kind: "task_failed", tag: "Failed", group: "Failed tasks" },
  { kind: "policy_deny", tag: "Policy", group: "Policy blocks" },
  { kind: "input_required", tag: "Input", group: "Needs input" },
  { kind: "review_issue", tag: "Review", group: "Review issues" },
  { kind: "flag_open", tag: "Flag", group: "Open flags" },
  { kind: "hold_active", tag: "Hold", group: "Active holds" },
  { kind: "call_no_answer", tag: "No answer", group: "Unanswered calls" },
  { kind: "stale_agent", tag: "Stale", group: "Stale agents" },
];
const KIND_META = Object.fromEntries(KINDS.map((k) => [k.kind, k])) as Record<Kind, (typeof KINDS)[number]>;
/** Approval actions read better as plain requests than as reducer names. */
const APPROVAL_TITLE: Record<string, string> = {
  propose_payment: "Release carrier payment",
  release_hold: "Release hold (bank change)",
  post_journal_entry: "Post journal entry",
  set_credit_limit: "Set credit limit",
};
const titleFor = (item: AttentionItem) =>
  item.kind === "approval_pending" ? (APPROVAL_TITLE[str(item.row.action, "")] ?? item.title) : item.title;
const PER_GROUP = 4;

export function AttentionQueue({ id }: { id?: string }) {
  const { attention, source } = useOps();
  const [filter, setFilter] = useState<Kind | "all">("all");
  const [expanded, setExpanded] = useState<Set<Kind>>(new Set());
  // Rows that mount after the page settles flash once (new items arriving live).
  const settled = useSettled();

  const groups = useMemo(() => {
    const by = new Map<Kind, AttentionItem[]>();
    for (const item of attention) {
      const list = by.get(item.kind);
      if (list) list.push(item);
      else by.set(item.kind, [item]);
    }
    return KINDS.filter((k) => by.has(k.kind)).map((k) => ({ ...k, items: by.get(k.kind)! }));
  }, [attention]);
  const active = filter !== "all" && !groups.some((g) => g.kind === filter) ? "all" : filter;
  const shown = active === "all" ? groups : groups.filter((g) => g.kind === active);
  const critical = attention.filter((a) => a.severity === "critical").length;

  return (
    <section className="fl-panel fh-att" id={id} aria-label={`Needs attention: ${attention.length} items`}>
      <header className="fl-panel-head">
        <span className="fl-section" style={{ "--accent": "var(--orange)" } as CSSProperties}>
          Needs attention
        </span>
        <span className="fh-head-meta">
          {critical > 0 && <span className="fl-chip bad outline">{critical} critical</span>}
          <span className={`fl-chip ${attention.length ? "hot solid" : source === "live" || source === "simulation" ? "ok outline" : "outline"}`}>{attention.length}</span>
        </span>
      </header>
      {groups.length > 1 && (
        <div className="fh-att-filter" role="toolbar" aria-label="Filter by kind">
          <button className={active === "all" ? "on" : ""} onClick={() => setFilter("all")} aria-pressed={active === "all"}>
            All <b>{attention.length}</b>
          </button>
          {groups.map((g) => (
            <button key={g.kind} className={active === g.kind ? "on" : ""} onClick={() => setFilter(g.kind)} aria-pressed={active === g.kind}>
              {g.tag} <b>{g.items.length}</b>
            </button>
          ))}
        </div>
      )}
      <div className="fh-att-body">
        {attention.length === 0 ? (
          <div className="fh-empty">
            <Mascot id="validator" mood={source === "live" || source === "simulation" ? "success" : "sleeping"} size={56} />
            <div>
              <div className="fl-label strong">{emptyNote(source, "All clear")}</div>
              <div className="fh-empty-sub">
                {source === "live" || source === "simulation" || source === "fallback"
                  ? "Nothing is waiting on a person right now."
                  : "The queue fills in once rows arrive."}
              </div>
            </div>
          </div>
        ) : (
          shown.map((g) => {
            const open = active !== "all" || expanded.has(g.kind);
            const items = open ? g.items : g.items.slice(0, PER_GROUP);
            return (
              <div className="fh-att-group" key={g.kind}>
                <div className="fh-att-group-head">
                  <span>{g.group}</span>
                  <span className="fl-num">{g.items.length}</span>
                </div>
                <ul>
                  {items.map((item) => (
                    <AttentionRow key={item.id} item={item} settled={settled} />
                  ))}
                </ul>
                {g.items.length > PER_GROUP && active === "all" && (
                  <button
                    className="fh-att-more"
                    onClick={() =>
                      setExpanded((prev) => {
                        const next = new Set(prev);
                        if (next.has(g.kind)) next.delete(g.kind);
                        else next.add(g.kind);
                        return next;
                      })
                    }
                  >
                    {open ? "Show fewer" : `Show ${g.items.length - PER_GROUP} more`}
                  </button>
                )}
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}

function AttentionRow({ item, settled }: { item: AttentionItem; settled: boolean }) {
  const { now, workloads } = useOps();
  const fresh = useFresh(settled);
  const meta = KIND_META[item.kind];
  const mood: Mood =
    item.kind === "task_failed" || item.kind === "policy_deny"
      ? "alert"
      : isAgentId(item.agent)
        ? moodForStatus(workloads[item.agent]?.status ?? "idle")
        : "idle";
  const body = (
    <>
      <Mascot id={item.agent || "human"} size={28} mood={mood} title={nick(item.agent)} />
      <span className="fh-att-main">
        <span className="fh-att-top">
          <b className="fh-att-tag">{meta?.tag ?? item.kind}</b>
          <span className="fh-att-title">{titleFor(item)}</span>
        </span>
        <span className="fh-att-sub">
          {item.caseId && <span className="fl-mono">{item.caseId}</span>}
          {item.detail && <span>{item.detail}</span>}
        </span>
      </span>
      <span className="fh-att-right">
        {item.amount > 0 && <span className="fl-num fh-att-amt">{usd(item.amount)}</span>}
        <span className="fh-att-age" suppressHydrationWarning>
          {item.at ? fmtAgo(item.at, now) : ""}
        </span>
      </span>
    </>
  );
  return (
    <li className={`fh-att-row sev-${item.severity} ${fresh ? "fh-fresh" : ""}`}>
      {item.caseId ? (
        <Link href={`/cases/${encodeURIComponent(item.caseId)}`} className="fh-att-link" aria-label={`${meta?.tag ?? ""}: ${titleFor(item)}, ${item.caseId}`}>
          {body}
        </Link>
      ) : (
        <div className="fh-att-link">{body}</div>
      )}
      {item.kind === "approval_pending" && <ApprovalActions item={item} />}
    </li>
  );
}

function ApprovalActions({ item }: { item: AttentionItem }) {
  const { source, identity, mutate, simulated } = useOps();
  const [decision, setDecision] = useState<"approved" | "rejected" | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const approver = str(item.row.approver, "");
  const lastError = str(item.row.last_error, "");
  const canDecide = source === "live" && Boolean(identity) && (!approver || approver === identity);
  const why = simulated
    ? "Simulation: the simulated controller decides these. Connect to the live database to approve."
    : source !== "live"
      ? "Read-only: reconnect to live SpacetimeDB to decide."
      : !identity
        ? "No human identity on this connection yet."
        : "Only the designated approver can decide this request.";

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!decision || !code.trim()) return;
    setBusy(true);
    setError("");
    try {
      await mutate("decide_approval", { id: str(item.row.id, ""), decision, code: code.trim() });
      setDecision(null);
      setCode("");
    } catch (err) {
      setError(String(err).replace(/^Error: /, ""));
    } finally {
      setBusy(false);
    }
  };

  if (decision)
    return (
      <form className="fh-att-decide" onSubmit={submit}>
        <input
          autoFocus
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="One-time code"
          aria-label="One-time approval code"
          inputMode="numeric"
          autoComplete="one-time-code"
        />
        <button type="submit" className={`fl-btn small ${decision === "approved" ? "primary" : "hot"}`} disabled={busy || !code.trim()}>
          {busy ? "Recording…" : decision === "approved" ? "Confirm approve" : "Confirm reject"}
        </button>
        <button type="button" className="fl-btn small quiet" onClick={() => setDecision(null)}>
          Cancel
        </button>
        {error && <span className="fh-att-err">{error}</span>}
      </form>
    );
  return (
    <div className="fh-att-actions" title={canDecide ? undefined : why}>
      <button className="fl-btn small primary" disabled={!canDecide} onClick={() => setDecision("approved")}>
        <Check size={12} /> Approve
      </button>
      <button className="fl-btn small" disabled={!canDecide} onClick={() => setDecision("rejected")}>
        <X size={12} /> Reject
      </button>
      {!canDecide && <span className="fh-att-why">{simulated ? "Sim decides" : "Read-only"}</span>}
      {lastError && <span className="fh-att-err">{lastError}</span>}
    </div>
  );
}
