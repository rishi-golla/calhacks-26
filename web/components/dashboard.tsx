"use client";
import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowUpRight,
  Bell,
  BookOpen,
  Building2,
  CheckCircle2,
  ChevronRight,
  Copy,
  Cpu,
  FileSpreadsheet,
  Flag,
  LayoutDashboard,
  ListTree,
  Menu,
  MessagesSquare,
  PanelLeft,
  Play,
  PlusCircle,
  Settings,
  Radio,
  RefreshCw,
  Search,
  Wallet,
  X,
} from "lucide-react";
import { date } from "@/lib/data";
import { LiveProvider, useLive } from "./live-provider";
import { CasesView, LedgerView, Overview } from "./operations";
import { InboxView, LearningView } from "./human-views";
import { CounterpartiesView, DemoView, HarnessView } from "./support-views";
import { Dialog, Submit, type FlagTarget } from "./ui";
import AgentInbox from "./agent-inbox";
import Home from "./home";
const AuditWorkspace = dynamic(() => import("./audit-workspace"), {
  ssr: false,
  loading: () => <div className="loading">Loading audit workspace…</div>,
});
function databaseHostLabel(serverUrl: string): string {
  try {
    const host = new URL(serverUrl).hostname.toLowerCase();
    if (host === "maincloud.spacetimedb.com") return "Maincloud";
    if (
      ["localhost", "127.0.0.1", "::1", "[::1]"].includes(host) ||
      host.endsWith(".localhost")
    )
      return "Local DB";
    return "Remote DB";
  } catch {
    return "Database";
  }
}
const views = [
  {
    id: "home",
    label: "New chat",
    icon: PlusCircle,
    description: "Your AI back office.",
  },
  {
    id: "agents",
    label: "Agent inbox",
    icon: MessagesSquare,
    description: "Follow all nine specialists as they work together.",
  },
  {
    id: "overview",
    label: "Overview",
    icon: LayoutDashboard,
    description: "A live picture of your autonomous back office.",
  },
  {
    id: "cases",
    label: "Cases & tasks",
    icon: ListTree,
    description: "Follow the work, from first evidence to final decision.",
  },
  {
    id: "inbox",
    label: "Human inbox",
    icon: Bell,
    description:
      "Decisions that need your judgment. Feedback that helps agents learn.",
  },
  {
    id: "ledger",
    label: "Ledger & holds",
    icon: Wallet,
    description: "The operational books, protected by deterministic controls.",
  },
  {
    id: "audit",
    label: "Audit workspace",
    icon: FileSpreadsheet,
    description:
      "Every number has a history. Follow it all the way to its source.",
  },
  {
    id: "companies",
    label: "Counterparties",
    icon: Building2,
    description: "Real companies. Enriched contacts. Original sources.",
  },
  {
    id: "learning",
    label: "Validator & learning",
    icon: BookOpen,
    description: "Each review makes the next case more informed.",
  },
  {
    id: "harness",
    label: "Model harness",
    icon: Cpu,
    description: "Route, observe and configure your inference infrastructure.",
  },
  {
    id: "demo",
    label: "Demo console",
    icon: Play,
    description:
      "A real scenario. A team-routed conversation. A complete audit trail.",
  },
];
function DashboardBody() {
  const {
    data,
    state,
    error,
    identity,
    serverUrl,
    databaseName,
    lastUpdated,
    reconnect,
    mutate,
  } = useLive();
  const hostLabel = databaseHostLabel(serverUrl);
  const [view, setView] = useState("home");
  const [caseId, setCaseId] = useState("");
  const [query, setQuery] = useState("");
  const [mobile, setMobile] = useState(false);
  const [narrow, setNarrow] = useState(false);
  const menuTrigger = useRef<HTMLElement | null>(null);
  const navigation = useRef<HTMLElement | null>(null);
  const [connectionInfo, setConnectionInfo] = useState(false);
  const [toast, setToast] = useState<{
    message: string;
    error: boolean;
  } | null>(null);
  const [flag, setFlag] = useState<FlagTarget | null>(null);
  const [kind, setKind] = useState("wrong");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [flagError, setFlagError] = useState("");
  const notify = useCallback(
    (message: string, isError = false) =>
      setToast({ message: message.replace(/^Error: /, ""), error: isError }),
    [],
  );
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 10000);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    const media = window.matchMedia(
      `(max-width: ${view === "agents" ? 800 : 760}px)`,
    );
    const update = () => setNarrow(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [view]);
  useEffect(() => {
    if (!narrow) return;
    if (mobile)
      navigation.current
        ?.querySelector<HTMLButtonElement>(".nav-item")
        ?.focus();
    else menuTrigger.current?.focus();
  }, [mobile, narrow]);
  const openNavigation = () => {
    menuTrigger.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setMobile(true);
  };
  const navigate = (id: string) => {
    setView(id);
    setMobile(false);
    if (narrow)
      requestAnimationFrame(() =>
        document.getElementById("main-content")?.focus(),
      );
  };
  const openCase = (id: string) => {
    setCaseId(id);
    setView("cases");
  };
  const onFlag = (target: FlagTarget) => {
    setFlag(target);
    setNote("");
    setKind("wrong");
    setFlagError("");
  };
  const pending = data.approval.filter(
    (row) => !row.decision || row.decision === "pending",
  ).length;
  const current = views.find((item) => item.id === view)!;
  const human = data.principal.some(
    (row) =>
      row.identity === identity && row.role === "human" && row.active !== false,
  );
  const submitFlag = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!flag) return;
    setFlagError("");
    setBusy(true);
    try {
      await mutate("raise_flag", {
        id: `FLAG-${crypto.randomUUID()}`,
        target_ref: flag.ref,
        kind,
        note,
        ...(flag.caseId ? { case_id: flag.caseId } : {}),
      });
      notify(
        "Flag recorded and queued for Validator review. Follow its delivery and resolution in the human inbox.",
      );
      setFlag(null);
    } catch (err) {
      setFlagError(String(err).replace(/^Error: /, ""));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div
      className={`app-shell ${view === "agents" ? "agent-mode" : ""} ${view === "home" ? "home-mode" : ""}`}
    >
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      {mobile && (
        <button
          className="sidebar-backdrop"
          aria-label="Close navigation"
          onClick={() => setMobile(false)}
        />
      )}
      <aside
        ref={navigation}
        inert={narrow && !mobile}
        aria-hidden={narrow && !mobile}
        className={`sidebar ${mobile ? "open" : ""}`}
      >
        <div className="side-top">
          <a
            className="brand"
            href="#"
            onClick={(event) => {
              event.preventDefault();
              navigate("home");
            }}
            aria-label="Golden Gate Freight home"
          >
            <span className="side-orb" />
          </a>
          <button
            className="icon-button side-collapse"
            aria-label="Close navigation"
            onClick={() => setMobile(false)}
          >
            <PanelLeft size={15} />
          </button>
        </div>
        <nav aria-label="Main navigation">
          {views.map((item) => (
            <div key={item.id}>
              <button
                className={`nav-item ${view === item.id ? "active" : ""}`}
                aria-current={view === item.id ? "page" : undefined}
                aria-label={item.label}
                title={item.label}
                onClick={() => navigate(item.id)}
              >
                <item.icon size={17} />
                <span>{item.label}</span>
                {item.id === "inbox" && pending > 0 && (
                  <span className="nav-count">{pending}</span>
                )}
                {item.id === "demo" && <span className="nav-demo">DEMO</span>}
              </button>
            </div>
          ))}
        </nav>
        <div className="side-chats">
          <div className="side-chats-head">
            <span>Chats</span>
            <button
              className="icon-button"
              aria-label="Search cases"
              onClick={() => navigate("cases")}
            >
              <Search size={14} />
            </button>
          </div>
          {data.case.slice(0, 12).map((row) => (
            <button
              key={String(row.case_id)}
              className={`side-chat ${view === "cases" && caseId === row.case_id ? "active" : ""}`}
              title={String(row.title || row.case_id)}
              onClick={() => {
                openCase(String(row.case_id));
                setMobile(false);
              }}
            >
              {String(row.title || row.case_id).replace(
                /^\[W2 fixture\]\s*/,
                "",
              )}
            </button>
          ))}
        </div>
        <div className="sidebar-bottom">
          <button
            className="side-settings"
            aria-label="Database connection and identity"
            onClick={() => setConnectionInfo(true)}
          >
            <Settings size={14} />
            <span>Settings</span>
            <small>{human ? "Human controller" : "Observer"}</small>
          </button>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="inline">
            <button
              className="icon-button mobile-toggle"
              onClick={() => setMobile((value) => !value)}
              aria-label="Toggle navigation"
            >
              <Menu size={20} />
            </button>
            <span className="breadcrumb">
              Workspace <ChevronRight size={13} />{" "}
              <strong>{current.label}</strong>
            </span>
          </div>
          <div className="topbar-actions">
            <label className="search-field">
              <Search size={15} />
              <input
                aria-label="Search cases, tasks or counterparties"
                placeholder="Search cases or tasks…"
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  if (view !== "companies") setView("cases");
                }}
              />
              {query && (
                <button
                  onClick={() => setQuery("")}
                  className="icon-button"
                  aria-label="Clear search"
                >
                  <X size={13} />
                </button>
              )}
            </label>
            <button
              className={`connection-pill ${state}`}
              onClick={() => setConnectionInfo(true)}
            >
              <i
                className={`dot ${state === "live" ? "teal" : state === "connecting" ? "amber" : "gray"}`}
              />
              {state === "live"
                ? `${hostLabel} live`
                : state === "fallback"
                  ? "Fallback snapshot"
                  : state === "connecting"
                    ? "Connecting"
                    : "Offline"}
            </button>
            <button
              className="icon-button"
              onClick={() => navigate("inbox")}
              aria-label={`Human inbox, ${pending} pending approvals`}
            >
              <Bell size={18} />
              {pending > 0 && <i className="notification-dot" />}
            </button>
          </div>
        </header>
        <main id="main-content" tabIndex={-1}>
          {view === "home" && (
            <Home
              onNavigate={navigate}
              onOpenCase={openCase}
              onOpenMenu={openNavigation}
              onAsk={(text) => {
                setQuery(text);
                setCaseId("");
                navigate("cases");
              }}
            />
          )}
          {view === "agents" && (
            <AgentInbox
              openCase={openCase}
              navigate={navigate}
              openMenu={openNavigation}
            />
          )}
          <div className="page-heading">
            <div>
              <div className="page-kicker">
                OPERATIONS / {current.label.toUpperCase()}
              </div>
              <h1>{current.label}</h1>
              <p>{current.description}</p>
            </div>
            <button className="button primary" onClick={() => navigate("demo")}>
              <Play size={14} /> Run a scenario
            </button>
          </div>
          {state !== "live" && (
            <div className={`connection-banner ${state}`} role="status">
              <Radio size={17} />
              <div>
                <strong>
                  {state === "fallback"
                    ? "Live subscription interrupted. Showing a read-only fallback snapshot."
                    : state === "connecting"
                      ? "Connecting to the operational database…"
                      : "The operational database is offline."}
                </strong>
                <span>
                  {state === "fallback"
                    ? `Refreshes every 5 seconds. Last snapshot ${date(lastUpdated)}.`
                    : error ||
                      (hostLabel === "Local DB"
                        ? `Start SpacetimeDB and publish ${databaseName} on ${serverUrl} to see live operations.`
                        : `Check the connection to ${databaseName} on ${serverUrl}.`)}
                </span>
              </div>
              <button className="button" onClick={reconnect}>
                <RefreshCw size={13} />
                Reconnect
              </button>
            </div>
          )}
          {view === "overview" && (
            <Overview onFlag={onFlag} openCase={openCase} navigate={navigate} />
          )}
          {view === "cases" && (
            <CasesView
              selected={caseId}
              setSelected={setCaseId}
              query={query}
              onFlag={onFlag}
              notify={notify}
            />
          )}
          {view === "inbox" && <InboxView onFlag={onFlag} notify={notify} />}
          {view === "ledger" && <LedgerView onFlag={onFlag} />}
          {view === "audit" && <AuditWorkspace onFlag={onFlag} />}
          {view === "companies" && (
            <CounterpartiesView query={query} onFlag={onFlag} />
          )}
          {view === "learning" && (
            <LearningView onFlag={onFlag} notify={notify} />
          )}
          {view === "harness" && (
            <HarnessView notify={notify} onFlag={onFlag} />
          )}
          {view === "demo" && <DemoView notify={notify} />}
          <footer className="page-footer">
            <span>
              <Activity size={12} /> Operational truth in SpacetimeDB
            </span>
            <span>
              Golden Gate Freight · Simulated brokerage · Demo-routed outreach
            </span>
          </footer>
        </main>
      </div>
      {toast && (
        <div
          className={`toast ${toast.error ? "toast-error" : ""}`}
          role={toast.error ? "alert" : "status"}
        >
          <CheckCircle2 size={18} />
          <span>{toast.message}</span>
          <button
            className="icon-button"
            onClick={() => setToast(null)}
            aria-label="Dismiss notification"
          >
            <X size={17} />
          </button>
        </div>
      )}
      {connectionInfo && (
        <Dialog
          title="Database & dashboard identity"
          onClose={() => setConnectionInfo(false)}
        >
          <div className="stack">
            <div className="connection-detail">
              <span>Server</span>
              <strong style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                {serverUrl}
              </strong>
            </div>
            <div className="connection-detail">
              <span>Database</span>
              <strong style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                {databaseName}
              </strong>
            </div>
            <div className="connection-detail">
              <span>Transport</span>
              <strong>
                {state === "live"
                  ? "Live WebSocket subscriptions"
                  : state === "fallback"
                    ? "Read-only HTTP fallback"
                    : state}
              </strong>
            </div>
            <div className="connection-detail">
              <span>Last data update</span>
              <strong>
                {lastUpdated ? date(lastUpdated) : "No data received"}
              </strong>
            </div>
            <div className="connection-detail">
              <span>Role</span>
              <strong>
                {human
                  ? "Human controller"
                  : "Observer · human role not provisioned"}
              </strong>
            </div>
            <label>
              Your SpacetimeDB identity
              <div className="copy-input">
                <input readOnly value={identity || "Waiting for connection"} />
                <button
                  className="button"
                  disabled={!identity}
                  onClick={() =>
                    navigator.clipboard
                      .writeText(identity)
                      .then(() => notify("Identity copied."))
                      .catch(() =>
                        notify(
                          "Copy unavailable; select the identity text to copy it.",
                          true,
                        ),
                      )
                  }
                >
                  <Copy size={14} />
                  Copy
                </button>
              </div>
            </label>
            <p className="muted">
              Give this public identity to the database administrator to grant
              the human role. Your private token stays in this browser. Service
              credentials are never used by the dashboard.
            </p>
            <p className="small muted">
              Approve, flag, restore and configure actions require a live
              connection and an authorized human identity. Read-only fallback
              remains available during an interruption.
            </p>
            <button className="button" onClick={reconnect}>
              <RefreshCw size={14} /> Reconnect subscriptions
            </button>
          </div>
        </Dialog>
      )}
      {flag && (
        <Dialog
          title="Send feedback to the Validator"
          onClose={() => setFlag(null)}
        >
          <form onSubmit={submitFlag} className="stack">
            <div className="flag-target">
              <Flag size={16} />
              <code>{flag.ref}</code>
            </div>
            <label>
              Feedback type
              <select
                value={kind}
                onChange={(event) => setKind(event.target.value)}
              >
                <option value="wrong">This looks wrong</option>
                <option value="unclear">Needs clarification</option>
                <option value="policy">Policy concern</option>
                <option value="praise">Good decision</option>
                <option value="other">Other feedback</option>
              </select>
            </label>
            <label>
              What should the Validator review?
              <textarea
                autoFocus
                rows={4}
                value={note}
                minLength={3}
                maxLength={4000}
                required
                onChange={(event) => setNote(event.target.value)}
                placeholder="Point to the evidence and explain what needs another look."
              />
            </label>
            {!human && (
              <p className="notice warning">
                This browser identity needs the human role before it can submit
                feedback. Open the connection status to copy your identity for
                the administrator.
              </p>
            )}
            {flagError && (
              <p className="notice warning" role="alert">
                {flagError}
              </p>
            )}
            <Submit busy={busy} disabled={state !== "live" || !human}>
              Queue Validator review <ArrowUpRight size={14} />
            </Submit>
          </form>
        </Dialog>
      )}
    </div>
  );
}
export default function Dashboard() {
  return (
    <LiveProvider>
      <DashboardBody />
    </LiveProvider>
  );
}
