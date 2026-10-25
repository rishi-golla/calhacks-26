"use client";
/**
 * Live log: a floating, draggable glass window that streams every decision in
 * the database — agent tool calls, holds, postings, validator verdicts, policy
 * denials and human approvals — over whatever page is open. It reads the same
 * `audit_event` / `review` rows the case trace does, so it works in Live,
 * Snapshot and Simulation alike, and it leaves the page underneath untouched.
 */
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import { ScrollText, X } from "lucide-react";
import { useOps } from "@/components/data-provider";
import { AGENT_META, agentColor, isAgentId } from "@/lib/agents";
import { json, str, timestamp } from "@/lib/data";
import { isHistoryAudit } from "@/lib/history";
import { buildTimeline, fmtAgo, type TimelineEvent } from "@/lib/ops";

type Lane = "all" | "agents" | "validator" | "policy" | "human" | "ledger";
const LANES: { id: Lane; label: string }[] = [
  { id: "all", label: "All" },
  { id: "agents", label: "Agents" },
  { id: "validator", label: "Validator" },
  { id: "policy", label: "Policy" },
  { id: "human", label: "Human" },
  { id: "ledger", label: "Ledger" },
];
const KEEP = 400;
const RARE_KEEP = 150;
const RECT_KEY = "fl.log.rect.v1";
const MIN_W = 360;
const MIN_H = 320;

type Rect = { x: number; y: number; w: number; h: number };
const UUIDISH = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type Check = { name: string; ok: boolean; detail?: string };

const isHuman = (e: TimelineEvent) => {
  const agent = e.agent.toLowerCase();
  return agent === "human" || agent.startsWith("human") || agent.startsWith("operator") || str(e.row.kind, "") === "decide_approval";
};

function laneOf(e: TimelineEvent): Lane {
  if (e.kind === "review" || e.agent === "validator") return "validator";
  if (e.kind === "policy") return "policy";
  if (isHuman(e)) return "human";
  if (e.kind === "journal" || e.kind === "payment" || e.kind === "hold") return "ledger";
  return "agents";
}

function actorName(agent: string) {
  if (isAgentId(agent)) return AGENT_META[agent].name;
  if (agent === "intake") return "Intake";
  if (!agent) return "System";
  if (agent.toLowerCase().startsWith("human") || agent.toLowerCase().startsWith("operator")) return "Human";
  return agent.replace(/^admin:/, "").replaceAll("_", " ");
}

function tone(e: TimelineEvent): "ok" | "bad" | "warn" | "" {
  if (e.kind === "policy" || e.kind === "task_failed") return "bad";
  if (e.kind === "review") return str(e.row.verdict, "").includes("issue") || /issue/i.test(e.title) ? "warn" : "ok";
  if (e.kind === "approval") return /approved|released/i.test(e.title) ? "ok" : /rejected/i.test(e.title) ? "bad" : "warn";
  if (e.kind === "hold") return /release/i.test(e.title) ? "ok" : "warn";
  if (e.kind === "journal") return "ok";
  return "";
}

function loadRect(): Rect | null {
  try {
    const raw = window.localStorage.getItem(RECT_KEY);
    return raw ? (JSON.parse(raw) as Rect) : null;
  } catch {
    return null;
  }
}

function clamp(r: Rect): Rect {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const w = Math.min(Math.max(MIN_W, r.w), vw - 16);
  const h = Math.min(Math.max(MIN_H, r.h), vh - 16);
  return { w, h, x: Math.min(Math.max(8, r.x), vw - w - 8), y: Math.min(Math.max(8, r.y), vh - h - 8) };
}

const defaultRect = (): Rect => clamp({ w: 460, h: Math.min(640, window.innerHeight - 96), x: window.innerWidth - 476, y: 60 });

/** Topbar button; the window itself is `LogWindow`. */
export function LogButton({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <button className={`fl-btn quiet small fl-log-btn ${open ? "on" : ""}`} onClick={onToggle} aria-pressed={open} aria-label="Toggle live log (L)">
      <ScrollText size={14} />
      Log
    </button>
  );
}

/** Toggles the log with `L` (outside inputs) and closes it with Escape. */
export function useLogHotkeys(open: boolean, setOpen: (next: boolean | ((prev: boolean) => boolean)) => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing = !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable);
      if (e.key === "Escape" && open) setOpen(false);
      else if ((e.key === "l" || e.key === "L") && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) setOpen((v) => !v);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, setOpen]);
}

export function LogWindow({ onClose }: { onClose: () => void }) {
  const { snapshot, now, source } = useOps();
  const [lane, setLane] = useState<Lane>("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState("");
  const [rect, setRect] = useState<Rect | null>(null);
  const drag = useRef<{ mode: "move" | "resize"; sx: number; sy: number; start: Rect } | null>(null);

  useEffect(() => {
    setRect(clamp(loadRect() ?? defaultRect()));
    const onResize = () => setRect((r) => (r ? clamp(r) : r));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  useEffect(() => {
    if (!rect) return;
    try {
      window.localStorage.setItem(RECT_KEY, JSON.stringify(rect));
    } catch {
      // Storage may be blocked; the window still works, it just forgets its place.
    }
  }, [rect]);

  const startDrag = useCallback(
    (mode: "move" | "resize") => (e: ReactPointerEvent) => {
      if (!rect || e.button !== 0) return;
      drag.current = { mode, sx: e.clientX, sy: e.clientY, start: rect };
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      e.preventDefault();
    },
    [rect],
  );
  const onPointerMove = (e: ReactPointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    setRect(d.mode === "move" ? clamp({ ...d.start, x: d.start.x + dx, y: d.start.y + dy }) : clamp({ ...d.start, w: d.start.w + dx, h: d.start.h + dy }));
  };
  const endDrag = () => {
    drag.current = null;
  };

  const events = useMemo(() => {
    // Verdicts come from the `review` table itself (it carries the checks); the `add_review` audit rows would duplicate them.
    // The validator's own "pass"/"issue" task completion says the same as its review row, so a case with review rows shows only those.
    // Importing or removing history is bookkeeping, not live activity.
    const reviewed = new Set(snapshot.review.map((r) => str(r.case_id, "")));
    const list = buildTimeline(snapshot).filter(
      (e) =>
        e.kind !== "doc_read" &&
        e.kind !== "dispatch" &&
        str(e.row.kind, "") !== "add_review" &&
        !isHistoryAudit(e.row) &&
        !(e.agent === "validator" && e.kind === "task_completed" && reviewed.has(e.caseId)),
    );
    for (const r of snapshot.review) {
      const caseId = str(r.case_id, "");
      const verdict = str(r.verdict, "pass");
      list.push({
        id: `review:${str(r.id)}`,
        at: timestamp(r.ts),
        caseId,
        taskId: str(r.target_task_id, ""),
        agent: "validator",
        kind: "review",
        title: verdict === "issue" ? "found an issue" : "passed",
        detail: "",
        policyIds: [],
        evidenceRefs: [],
        row: r,
      });
    }
    list.sort((a, b) => b.at - a.at);
    // The newest KEEP rows, plus older verdicts, denials and human decisions so those lanes never read as empty.
    const rare: Record<string, number> = {};
    return list.filter((e, i) => {
      if (i < KEEP) return true;
      const lane = laneOf(e);
      if (lane === "agents" || lane === "ledger") return false;
      rare[lane] = (rare[lane] ?? 0) + 1;
      return rare[lane] <= RARE_KEEP;
    });
  }, [snapshot]);

  const reviewsByCase = useMemo(() => {
    const by = new Map<string, Record<string, unknown>[]>();
    for (const r of snapshot.review) {
      const id = str(r.case_id, "");
      by.set(id, [...(by.get(id) ?? []), r]);
    }
    return by;
  }, [snapshot.review]);

  const counts = useMemo(() => {
    const c: Record<Lane, number> = { all: events.length, agents: 0, validator: 0, policy: 0, human: 0, ledger: 0 };
    for (const e of events) c[laneOf(e)]++;
    return c;
  }, [events]);

  const q = query.trim().toLowerCase();
  const shown = events.filter(
    (e) =>
      (lane === "all" || laneOf(e) === lane) &&
      // Search what the row shows (actor name, title with spaces) plus the raw ids.
      (!q ||
        `${e.caseId} ${e.agent} ${actorName(e.agent)} ${e.title} ${e.title.replaceAll("_", " ")} ${e.detail} ${e.policyIds.join(" ")} ${e.taskId}`
          .toLowerCase()
          .includes(q)),
  );

  const reviewed = snapshot.review.length;
  const issues = snapshot.review.filter((r) => str(r.verdict, "") === "issue").length;
  const humans = snapshot.approval.filter((a) => ["approved", "rejected"].includes(str(a.decision, ""))).length;

  if (!rect) return null;
  return (
    <section
      className="fl-log"
      role="dialog"
      aria-label="Live log"
      style={{ "--x": `${rect.x}px`, "--y": `${rect.y}px`, "--w": `${rect.w}px`, "--h": `${rect.h}px` } as CSSProperties}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
    >
      <header className="fl-log-head" onPointerDown={startDrag("move")}>
        <span className="fl-log-title">Log</span>
        {/* Live is the default; only say so when it is not. The span also pushes the close button right. */}
        <span className="fl-log-meta">{source === "live" ? "" : source === "simulation" ? "simulation" : "snapshot"}</span>
        <button className="fl-log-x" onClick={onClose} onPointerDown={(e) => e.stopPropagation()} aria-label="Close log">
          <X size={13} />
        </button>
      </header>
      <div className="fl-log-chips" role="toolbar" aria-label="Filter log">
        {LANES.map((l) => (
          <button key={l.id} className={lane === l.id ? "on" : ""} onClick={() => setLane(l.id)} aria-pressed={lane === l.id}>
            {l.label}
            {l.id !== "all" && counts[l.id] > 0 && <b>{counts[l.id]}</b>}
          </button>
        ))}
      </div>
      <input className="fl-log-search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter: case, agent, rule (P-005)…" aria-label="Filter log" />
      <ol className="fl-log-rows">
        {shown.length === 0 && <li className="fl-log-empty">{q ? "No matches" : "Nothing yet"}</li>}
        {shown.map((e) => (
          <LogRow key={e.id} e={e} now={now} open={selected === e.id} onToggle={() => setSelected((s) => (s === e.id ? "" : e.id))} reviews={reviewsByCase.get(e.caseId)} />
        ))}
      </ol>
      {(reviewed > 0 || humans > 0) && (
        <footer className="fl-log-foot">
          {reviewed > 0 && (
            <span title={`Validator: ${reviewed} reviewed`}>
              {reviewed - issues}/{reviewed} reviews passed
            </span>
          )}
          {humans > 0 && <span>{humans} human {humans === 1 ? "decision" : "decisions"}</span>}
        </footer>
      )}
      <span className="fl-log-resize" onPointerDown={startDrag("resize")} aria-hidden />
    </section>
  );
}

function reviewChecks(e: TimelineEvent, reviews: Record<string, unknown>[] | undefined): { verdict: string; checks: Check[]; actions: string[] } | null {
  if (e.kind !== "review") return null;
  const direct = e.row.checks_json !== undefined ? e.row : undefined;
  const r = direct ?? reviews?.reduce<Record<string, unknown> | undefined>((best, cur) => (!best || Math.abs(timestamp(cur.ts) - e.at) < Math.abs(timestamp(best.ts) - e.at) ? cur : best), undefined);
  if (!r) return null;
  return { verdict: str(r.verdict, ""), checks: json<Check[]>(r.checks_json, []), actions: json<string[]>(r.actions_json, []) };
}

function LogRow({ e, now, open, onToggle, reviews }: { e: TimelineEvent; now: number; open: boolean; onToggle: () => void; reviews: Record<string, unknown>[] | undefined }) {
  const t = tone(e);
  const review = open ? reviewChecks(e, reviews) : null;
  const failed = review?.checks.filter((c) => !c.ok) ?? [];
  const passed = review?.checks.filter((c) => c.ok) ?? [];
  // Raw ids (task UUIDs) say nothing at a glance; they stay searchable via the filter.
  const detail = UUIDISH.test(e.detail) ? "" : e.detail;
  const sub = [e.caseId, detail].filter(Boolean).join(" · ");
  return (
    <li className={`fl-log-row ${open ? "open" : ""}`}>
      <button className="fl-log-row-main" onClick={onToggle} aria-expanded={open}>
        <span className="fl-log-time">{fmtAgo(e.at, now)}</span>
        <i className="fl-log-dot" style={{ background: agentColor(e.agent) }} />
        <span className="fl-log-text">
          <b>{actorName(e.agent)}</b> {e.title.replaceAll("_", " ")}
          {e.policyIds.map((p) => (
            <span key={p} className={`fl-log-tag ${t}`}>
              {p}
            </span>
          ))}
          {sub && <small>{sub}</small>}
        </span>
      </button>
      {open && (review || e.evidenceRefs.length > 0 || str(e.row.reason_code, "") || e.caseId) && (
        <div className="fl-log-detail">
          {review && (
            <>
              {/* Failures carry their detail; passes collapse to one line, names in the tooltip. */}
              {failed.slice(0, 14).map((c) => (
                <div key={c.name} className="bad">
                  ✕ {c.name}
                  {c.detail && ` · ${c.detail}`}
                </div>
              ))}
              {passed.length > 0 && (
                <div className="ok" title={passed.map((c) => (c.detail ? `${c.name}: ${c.detail}` : c.name)).join("\n")}>
                  ✓ {passed.length} {passed.length === 1 ? "check" : "checks"} passed
                </div>
              )}
              {review.actions.length > 0 && review.actions[0] !== "none" && <div>{review.actions.join(", ")}</div>}
            </>
          )}
          {!review && e.evidenceRefs.length > 0 && <div>Evidence: {e.evidenceRefs.slice(0, 4).join(", ")}</div>}
          {!review && str(e.row.reason_code, "") && <div>Reason: {str(e.row.reason_code)}</div>}
          {e.caseId && (
            <div className="fl-log-links">
              <Link href={`/cases/${encodeURIComponent(e.caseId)}`} title={[str(e.row.id, ""), str(e.row.actor_identity, "") && `actor ${str(e.row.actor_identity)}`].filter(Boolean).join(" · ") || undefined}>
                Open case
              </Link>
            </div>
          )}
        </div>
      )}
    </li>
  );
}
