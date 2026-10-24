"use client";
import { useEffect, useState } from "react";
import {
  ArrowUp,
  Bell,
  ChevronRight,
  FileSpreadsheet,
  ListTree,
  Play,
  Plus,
  Wallet,
} from "lucide-react";
import { useLive } from "./live-provider";

function useClock() {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const timer = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(timer);
  }, []);
  return now
    ? now.toLocaleTimeString("en-US", {
        hour: "numeric",
        minute: "2-digit",
        timeZone: "America/Los_Angeles",
      })
    : "";
}

export function BrandMark({ size = 40 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 40 40"
      aria-hidden="true"
      className="home-mark"
    >
      <defs>
        <linearGradient id="gg-mark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#f6c48a" />
          <stop offset="1" stopColor="#e39a52" />
        </linearGradient>
      </defs>
      <rect x="3" y="9" width="22" height="17" rx="6" fill="url(#gg-mark)" />
      <path d="M25 14h6.5l5 6v6H25z" fill="url(#gg-mark)" />
      <circle
        cx="11"
        cy="29"
        r="4.2"
        fill="#fff"
        stroke="#d98b45"
        strokeWidth="2"
      />
      <circle
        cx="30"
        cy="29"
        r="4.2"
        fill="#fff"
        stroke="#d98b45"
        strokeWidth="2"
      />
      <circle cx="11" cy="17" r="1.8" fill="#4a2a12" />
      <circle cx="17" cy="17" r="1.8" fill="#4a2a12" />
    </svg>
  );
}

export default function Home({
  onNavigate,
  onOpenCase,
  onAsk,
}: {
  onNavigate: (view: string) => void;
  onOpenCase: (caseId: string) => void;
  onAsk: (query: string) => void;
}) {
  const { data } = useLive();
  const time = useClock();
  const [draft, setDraft] = useState("");
  const pending = data.approval.filter(
    (row) => !row.decision || row.decision === "pending",
  ).length;
  const replays = data.case.slice(0, 6);
  const submit = (event?: React.FormEvent) => {
    event?.preventDefault();
    const query = draft.trim();
    if (!query) return;
    onAsk(query);
  };
  return (
    <div className="home">
      <header className="home-top">
        <nav aria-label="Quick links">
          <button onClick={() => onNavigate("overview")}>Overview</button>
          <button onClick={() => onNavigate("companies")}>
            Counterparties
          </button>
        </nav>
        <div className="home-top-actions">
          <button className="home-link" onClick={() => onNavigate("agents")}>
            Agent inbox
          </button>
          <button className="home-dark" onClick={() => onNavigate("inbox")}>
            Human inbox
          </button>
        </div>
      </header>
      <section className="home-hero">
        <BrandMark />
        <p className="home-meta">San Francisco · {time}</p>
        <h1>Your AI Back Office</h1>
        <p className="home-sub">
          Golden Gate Freight has nine agents, a ledger, and a phone line
          <br />
          to settle loads, chase paperwork, and escalate what needs you.
        </p>
        <div className="home-pills">
          <div className="home-pill-row two">
            <button onClick={() => onNavigate("inbox")}>
              <Bell size={14} className="pill-green" />
              {pending > 0
                ? `${pending} decision${pending === 1 ? "" : "s"} waiting`
                : "Human inbox"}
            </button>
            <button onClick={() => onNavigate("demo")}>
              <Play size={14} className="pill-blue" />
              Run the demo
            </button>
          </div>
          <div className="home-pill-row three">
            <button onClick={() => onNavigate("cases")}>
              <ListTree size={14} />
              Cases
            </button>
            <button onClick={() => onNavigate("ledger")}>
              <Wallet size={14} />
              Ledger
            </button>
            <button onClick={() => onNavigate("audit")}>
              <FileSpreadsheet size={14} />
              Audit
            </button>
          </div>
        </div>
        <div className="home-replays">
          <p>Watch replays</p>
          {replays.length === 0 && (
            <span className="home-empty">
              No cases yet. They appear here as agents open them.
            </span>
          )}
          {replays.map((row) => (
            <button
              key={String(row.case_id)}
              onClick={() => onOpenCase(String(row.case_id))}
            >
              <ListTree size={13} />
              <span>
                {String(row.title || row.case_id).replace(
                  /^\[W2 fixture\]\s*/,
                  "",
                )}
              </span>
              <ChevronRight size={14} />
            </button>
          ))}
        </div>
      </section>
      <form className="home-composer" onSubmit={submit}>
        <textarea
          aria-label="Ask anything"
          placeholder="Ask anything"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) submit(event);
          }}
        />
        <div className="home-composer-bar">
          <button
            type="button"
            aria-label="Open cases"
            onClick={() => onNavigate("cases")}
          >
            <Plus size={16} />
          </button>
          <button
            type="submit"
            aria-label="Send"
            className="home-send"
            disabled={!draft.trim()}
          >
            <ArrowUp size={15} />
          </button>
        </div>
      </form>
    </div>
  );
}
