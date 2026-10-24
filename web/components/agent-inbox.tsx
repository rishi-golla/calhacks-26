"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import {
  Activity,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  CheckCheck,
  ChevronDown,
  CirclePause,
  CirclePlay,
  FileCheck2,
  FileSearch,
  Headphones,
  Inbox,
  Menu,
  Radio,
  RotateCcw,
  Search,
  ShieldCheck,
  Sparkles,
  Truck,
  Users,
  Wallet,
  X,
  ChartNoAxesCombined,
  ExternalLink,
  Wrench,
  LoaderCircle,
  Check,
  CircleAlert,
  Clock3,
  ShieldAlert,
} from "lucide-react";
import {
  AGENTS,
  agentName,
  emptySnapshot,
  safeUrl,
  str,
  timestamp,
  type Snapshot,
} from "@/lib/data";
import {
  buildAgentMessages,
  messagesForAgent,
  agentActivity,
  inboxActivityFingerprint,
  isSyntheticCase,
  onlineAgentCount,
  toolStatusLabel,
  type AgentMessage,
  type ToolCallStatus,
} from "@/lib/agent-inbox";
import { markMessagesRead, type ReadMarkers } from "@/lib/inbox-read-state";
import { useLive } from "./live-provider";
import "./agent-inbox.css";

type AgentId = (typeof AGENTS)[number]["id"];
type Replay = {
  tables: Snapshot;
  capturedAt: string;
  description: string;
  messages: AgentMessage[];
};
const ICONS = {
  carrier_ap: Truck,
  risk: FileSearch,
  verification: ShieldCheck,
  documents: FileCheck2,
  shipper_ar: Wallet,
  cfo: ChartNoAxesCombined,
  controller: CheckCheck,
  voice_desk: Headphones,
  validator: Sparkles,
};
const COLORS: Record<AgentId, string> = {
  carrier_ap: "#3371cc",
  risk: "#cc8733",
  verification: "#6633cc",
  documents: "#47b8a7",
  shipper_ar: "#cc336f",
  cfo: "#5ca857",
  controller: "#335ecc",
  voice_desk: "#cc7533",
  validator: "#6333cc",
};
const FRONTLINE: AgentId[] = [
  "carrier_ap",
  "risk",
  "verification",
  "documents",
  "shipper_ar",
  "cfo",
];
const SUPPORT: AgentId[] = ["controller", "voice_desk", "validator"];
const kindLabel: Record<AgentMessage["kind"], string> = {
  handoff: "Handoff",
  status: "Task update",
  result: "Result",
  audit: "Decision",
  call: "Call update",
  review: "Review",
  tool: "Tool call",
};
const TOOL_STATUS_ICONS = {
  started: LoaderCircle, completed: Check, failed: CircleAlert,
  waiting: Clock3, blocked: ShieldAlert,
};
function ToolStatus({ status }: { status: ToolCallStatus }) {
  const Icon = TOOL_STATUS_ICONS[status];
  return <span className={`ai-tool-status ${status}`}>
    <Icon size={12} aria-hidden="true" />{toolStatusLabel[status]}
  </span>;
}
function durationLabel(ms: number) {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}
function Avatar({ id, small = false }: { id: string; small?: boolean }) {
  const known = id in ICONS;
  const Icon = known ? ICONS[id as AgentId] : Users;
  return (
    <span
      className={`ai-avatar ${small ? "small" : ""}`}
      style={
        {
          "--agent-accent": known ? COLORS[id as AgentId] : "#8e8e93",
        } as CSSProperties
      }
    >
      <Icon size={small ? 17 : 22} strokeWidth={1.7} aria-hidden="true" />
    </span>
  );
}
function timeLabel(at: number) {
  return at
    ? new Date(at).toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      })
    : "Time unavailable";
}
function dayLabel(at: number) {
  return at
    ? new Date(at).toLocaleDateString([], {
        weekday: "long",
        month: "long",
        day: "numeric",
      })
    : "Recorded activity";
}
function safeReadMarkers(value: string | null): ReadMarkers {
  try {
    const candidate: unknown = JSON.parse(value || "{}");
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate))
      return {};
    return Object.fromEntries(
      Object.entries(candidate).filter(
        ([id, values]) =>
          AGENTS.some((agent) => agent.id === id) &&
          Array.isArray(values) &&
          values.every((value) => typeof value === "string"),
      ),
    );
  } catch {
    return {};
  }
}
function replaySnapshot(replay: Replay, at: number): Snapshot {
  const data = { ...replay.tables };
  data.task = replay.tables.task
    .filter((row) => timestamp(row.started_at) <= at)
    .map((row) =>
      timestamp(row.updated_at) > at
        ? {
            ...row,
            a2a_state: "working",
            business_status: "investigating",
            updated_at: row.started_at,
            artifact_json: "",
          }
        : row,
    );
  data.agent = [];
  return data;
}

function Message({
  message,
  openDetails,
  filterCase,
}: {
  message: AgentMessage;
  openDetails: (message: AgentMessage) => void;
  filterCase: (id: string) => void;
}) {
  return (
    <article
      className={`ai-message ai-message-${message.kind}`}
      aria-label={`${agentName(message.agentId)} ${kindLabel[message.kind]}`}
    >
      <Avatar id={message.agentId} small />
      <div className="ai-message-content">
        <div className="ai-message-byline">
          <strong>{agentName(message.agentId)}</strong>
          {message.recipientId ? (
            <>
              <ArrowRight size={12} aria-label="to" />
              <span>{agentName(message.recipientId)}</span>
            </>
          ) : null}
          <time
            dateTime={
              message.at ? new Date(message.at).toISOString() : undefined
            }
          >
            {timeLabel(message.at)}
          </time>
        </div>
        <div className="ai-bubble">
          <div className="ai-message-meta">
            <span className={`ai-kind ${message.kind}`}>
              {message.kind === "tool" ? <Wrench size={10} aria-hidden="true" /> : null}
              {kindLabel[message.kind]}
            </span>
            {message.caseId ? (
              <button
                onClick={() => filterCase(message.caseId)}
                title="Filter this case"
              >
                {message.caseId}
              </button>
            ) : null}
            {message.syntheticSource ? (
              <span className="ai-source-tag">Demo case</span>
            ) : null}
            {message.liveExternalEvidence ? (
              <span className="ai-source-tag">Live external evidence</span>
            ) : null}
          </div>
          <h3>{message.title}</h3>
          {message.toolCall ? (
            <div className="ai-tool-summary" aria-live="polite">
              <code>{message.toolCall.name}</code>
              <ToolStatus status={message.toolCall.status} />
              {message.toolCall.durationMs !== undefined ? <span className="ai-tool-duration">
                {durationLabel(message.toolCall.durationMs)}
              </span> : null}
            </div>
          ) : null}
          {message.body ? <p>{message.body}</p> : null}
          <div className="ai-message-actions">
            <button onClick={() => openDetails(message)}>
              {message.kind === "tool" ? "Inspect tool call" : message.evidenceRefs.length
                ? `${message.evidenceRefs.length} evidence reference${message.evidenceRefs.length === 1 ? "" : "s"}`
                : "View task details"}
              <ArrowRight size={12} />
            </button>
            {message.model ? (
              <span title={message.model}>
                {message.model.includes("fixture")
                  ? message.kind === "tool" ? "Fixture run" : "Simulated assessment"
                  : message.model}
              </span>
            ) : null}
          </div>
        </div>
      </div>
    </article>
  );
}

export default function AgentInbox({
  openCase,
  navigate,
  openMenu,
}: {
  openCase: (id: string) => void;
  navigate: (view: string) => void;
  openMenu: () => void;
}) {
  const { data, state, serverUrl, databaseName, reconnect, lastUpdated } =
    useLive();
  const [selected, setSelected] = useState("all");
  const [agentQuery, setAgentQuery] = useState("");
  const [query, setQuery] = useState("");
  const [caseId, setCaseId] = useState("");
  const [inspectedMessage, setDetails] = useState<AgentMessage | null>(null);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [sample, setSample] = useState(false);
  const [loadingReplay, setLoadingReplay] = useState(false);
  const [replayError, setReplayError] = useState("");
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [following, setFollowing] = useState(true);
  const [mobileThread, setMobileThread] = useState(false);
  const [now, setNow] = useState(0);
  const [read, setRead] = useState<{ key: string; markers: ReadMarkers }>({
    key: "",
    markers: {},
  });
  const scroll = useRef<HTMLDivElement>(null);
  const backButton = useRef<HTMLButtonElement>(null);
  const detailsClose = useRef<HTMLButtonElement>(null);
  const threadTrigger = useRef<HTMLElement | null>(null);
  const detailsTrigger = useRef<HTMLElement | null>(null);
  const readCount = useRef(0);
  const markerKey = `freightops.inbox.read.v1:${serverUrl}:${databaseName}:${sample ? "replay" : "live"}`;
  useEffect(() => {
    const tick = () => setNow(Date.now());
    tick();
    const timer = setInterval(tick, 15000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    try {
      setRead({
        key: markerKey,
        markers: safeReadMarkers(localStorage.getItem(markerKey)),
      });
    } catch {
      setRead({ key: markerKey, markers: {} });
    }
  }, [markerKey]);
  const liveMessages = useMemo(() => buildAgentMessages(data), [data]);
  const allMessages = useMemo(
    () => (sample && replay ? replay.messages.slice(0, cursor) : liveMessages),
    [sample, replay, cursor, liveMessages],
  );
  // Keep an open inspector current while the same call moves from running to done.
  const details = inspectedMessage
    ? allMessages.find((message) => message.id === inspectedMessage.id) || inspectedMessage
    : null;
  const inspectedId = details?.id;
  const visible = useMemo(
    () => messagesForAgent(allMessages, selected, caseId, query),
    [allMessages, selected, caseId, query],
  );
  const replayAt =
    sample && replay ? replay.messages[Math.max(0, cursor - 1)]?.at || 0 : 0;
  const snapshot = useMemo(
    () => (sample && replay ? replaySnapshot(replay, replayAt) : data),
    [sample, replay, replayAt, data],
  );
  const cases = sample && replay ? replay.tables.case : data.case;
  const active = AGENTS.find((agent) => agent.id === selected);
  const related = useMemo(
    () =>
      Object.fromEntries(
        AGENTS.map((agent) => [
          agent.id,
          messagesForAgent(allMessages, agent.id),
        ]),
      ),
    [allMessages],
  );
  const unread = (id: string) => {
    const known = new Set(read.key === markerKey ? read.markers[id] || [] : []);
    return (related[id] || [])
      .slice(-2000)
      .filter((message) => !known.has(inboxActivityFingerprint(message)))
      .length;
  };
  const fingerprints = visible.map(inboxActivityFingerprint).join("|");
  useEffect(() => {
    if (!following || !scroll.current?.getClientRects().length) return;
    scroll.current.scrollTop = scroll.current.scrollHeight;
    readCount.current = visible.length;
  }, [fingerprints, following, selected, visible.length, mobileThread]);
  useEffect(() => {
    if (
      !following ||
      details ||
      !scroll.current?.getClientRects().length ||
      read.key !== markerKey ||
      document.visibilityState !== "visible"
    )
      return;
    const markers = markMessagesRead(
      read.markers,
      visible,
      allMessages,
      selected,
    );
    if (markers !== read.markers) {
      setRead({ key: markerKey, markers });
      try {
        localStorage.setItem(markerKey, JSON.stringify(markers));
      } catch {
        /* Private browsing can disable storage. */
      }
    }
  }, [
    fingerprints,
    following,
    selected,
    markerKey,
    read,
    visible,
    mobileThread,
    details,
    allMessages,
  ]);
  useEffect(() => {
    if (!playing || !sample || !replay) return;
    if (cursor >= replay.messages.length) {
      setPlaying(false);
      return;
    }
    const timer = setTimeout(() => setCursor((value) => value + 1), 1000);
    return () => clearTimeout(timer);
  }, [playing, sample, replay, cursor]);
  useEffect(() => {
    if (!inspectedId) return;
    detailsClose.current?.focus();
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDetails(null);
    };
    window.addEventListener("keydown", escape);
    return () => {
      window.removeEventListener("keydown", escape);
      detailsTrigger.current?.focus();
    };
  }, [inspectedId]);
  useEffect(() => {
    if (!window.matchMedia("(max-width: 800px)").matches) return;
    if (mobileThread) backButton.current?.focus();
    else threadTrigger.current?.focus();
  }, [mobileThread]);

  const selectAgent = (id: string) => {
    threadTrigger.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setSelected(id);
    setQuery("");
    setDetails(null);
    setFollowing(true);
    setMobileThread(true);
  };
  const inspect = (message: AgentMessage) => {
    detailsTrigger.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setDetails(message);
  };
  const filterCase = (id: string) => {
    setCaseId(id);
    setFollowing(true);
  };
  const playSample = useCallback(async () => {
    setLoadingReplay(true);
    setReplayError("");
    try {
      const file = (await import("@/lib/agent-inbox-replay.json")).default;
      const tables = { ...emptySnapshot(), ...file.tables } as Snapshot;
      const messages = buildAgentMessages(tables);
      setReplay({
        tables,
        messages,
        capturedAt: file.metadata.captured_at,
        description: file.metadata.description,
      });
      setSample(true);
      setCursor(1);
      setPlaying(true);
      setCaseId("");
      setQuery("");
      setSelected("all");
      setDetails(null);
      setFollowing(true);
      setMobileThread(true);
    } catch {
      setReplayError("The recorded sample could not load. Please try again.");
    } finally {
      setLoadingReplay(false);
    }
  }, []);
  const leaveSample = () => {
    setSample(false);
    setPlaying(false);
    setCaseId("");
    setQuery("");
    setDetails(null);
    setFollowing(true);
  };
  const activity = (id: string) => {
    const current = agentActivity(
      snapshot,
      id,
      sample ? replayAt : now,
      sample || state === "live",
    );
    if (!sample) return current;
    if (!snapshot.task.some((task) => task.agent === id))
      return { state: "offline", label: "Not yet in this recording" };
    return {
      ...current,
      label:
        current.state === "working"
          ? "Working in recording"
          : current.state === "waiting"
            ? "Waiting in recording"
            : "Finished in recording",
    };
  };
  const workingCount = AGENTS.filter(
    (agent) => activity(agent.id).state === "working",
  ).length;
  const onlineCount = onlineAgentCount(data, now, state === "live");
  const syntheticVisible =
    !sample &&
    (visible.some((message) => message.syntheticSource) ||
      (caseId &&
        cases.some(
          (row) => str(row.case_id) === caseId && isSyntheticCase(row),
        )));
  const pending = data.approval.filter(
    (row) => !row.decision || row.decision === "pending",
  ).length;

  const agentButton = (id: AgentId) => {
    const agent = AGENTS.find((item) => item.id === id)!;
    if (
      agentQuery &&
      !`${agent.name} ${agent.role}`
        .toLowerCase()
        .includes(agentQuery.toLowerCase())
    )
      return null;
    const messages = related[id] || [];
    const latest = messages.at(-1);
    const status = activity(id);
    const count = unread(id);
    return (
      <button
        className={`ai-agent-row ${selected === id ? "selected" : ""}`}
        key={id}
        onClick={() => selectAgent(id)}
        aria-label={`${agent.name}, ${status.label}${count ? `, ${count} unread updates` : ""}`}
        aria-current={selected === id ? "true" : undefined}
      >
        <span className="ai-avatar-wrap">
          <Avatar id={id} />
          <i className={`ai-presence ${status.state}`} title={status.label} />
        </span>
        <span className="ai-agent-copy">
          <span className="ai-agent-name" title={agent.name}>
            {agent.name}
          </span>
          <span className="ai-agent-preview">
            {latest?.toolCall
              ? `${latest.title} · ${toolStatusLabel[latest.toolCall.status]}`
              : latest?.body || latest?.title || agent.role}
          </span>
          <span className={`ai-agent-status ${status.state}`}>
            {status.label}
          </span>
        </span>
        {count > 0 ? (
          <span className="ai-unread" aria-label={`${count} unread updates`}>
            {count > 99 ? "99+" : count}
          </span>
        ) : null}
      </button>
    );
  };
  const clearFilters = () => {
    setQuery("");
    setCaseId("");
    setFollowing(true);
  };

  return (
    <section
      className={`agent-inbox ${mobileThread ? "show-thread" : "show-list"} ${details ? "has-inspector" : ""}`}
      aria-label="Agent inbox"
    >
      <aside className="ai-team-list" aria-label="Your nine brokerage agents">
        <div className="ai-team-header">
          <div className="ai-eyebrow">
            <span className="ai-small-mark" />
            <span>GOLDEN GATE FREIGHT</span>
            <button
              className="ai-icon-button ai-mobile-menu"
              aria-label="Open workspace navigation"
              onClick={openMenu}
            >
              <Menu size={18} />
            </button>
          </div>
          <h1>
            Agent inbox <span>9</span>
          </h1>
          <p>
            {!sample && state === "live"
              ? `${onlineCount} of 9 agents online · ${workingCount} working`
              : "Your brokerage team, in conversation."}
          </p>
        </div>
        <label className="ai-agent-search">
          <Search size={15} />
          <input
            aria-label="Find an agent"
            placeholder="Find an agent…"
            value={agentQuery}
            onChange={(event) => setAgentQuery(event.target.value)}
          />
          {agentQuery ? (
            <button
              aria-label="Clear agent search"
              onClick={() => setAgentQuery("")}
            >
              <X size={14} />
            </button>
          ) : null}
        </label>
        <button
          className={`ai-team-thread ${selected === "all" ? "selected" : ""}`}
          onClick={() => selectAgent("all")}
          aria-current={selected === "all" ? "true" : undefined}
        >
          <Avatar id="all" />
          <span>
            <strong>Team conversation</strong>
            <small>Every handoff. The whole picture.</small>
          </span>
          <ChevronDown size={14} className="ai-rotated" />
        </button>
        <div className="ai-agent-list-scroll">
          <p className="ai-group-label">
            BROKERAGE SPECIALISTS <span>6</span>
          </p>
          {FRONTLINE.map(agentButton)}
          <p className="ai-group-label">
            SUPPORT & OVERSIGHT <span>3</span>
          </p>
          {SUPPORT.map(agentButton)}
          {agentQuery &&
          !AGENTS.some((agent) =>
            `${agent.name} ${agent.role}`
              .toLowerCase()
              .includes(agentQuery.toLowerCase()),
          ) ? (
            <p className="ai-no-agent">No agents match “{agentQuery}”.</p>
          ) : null}
        </div>
        <div className="ai-list-footer">
          <ShieldCheck size={15} />
          <span>
            Every action has a trail.
            <br />
            <small>Human approvals stay in your control.</small>
          </span>
        </div>
      </aside>
      <div className="ai-conversation">
        <header className="ai-conversation-header">
          <button
            className="ai-icon-button ai-back"
            aria-label="Back to agents"
            ref={backButton}
            onClick={() => setMobileThread(false)}
          >
            <ArrowLeft size={20} />
          </button>
          <Avatar id={selected} />
          <div className="ai-conversation-title">
            <h2>{active?.name || "Team conversation"}</h2>
            <p>
              {active?.role || "Nine specialists. One shared view of the work."}
            </p>
          </div>
          <div className={`ai-feed-state ${sample ? "replay" : state}`}>
            <Radio size={13} />
            {sample
              ? "Recorded replay"
              : state === "live"
                ? "Live activity"
                : state === "fallback"
                  ? "Read-only snapshot"
                  : state === "connecting"
                    ? "Connecting"
                    : "Disconnected"}
          </div>
        </header>
        <div className="ai-conversation-tools">
          <label className="ai-message-search">
            <Search size={14} />
            <input
              aria-label="Search activity"
              placeholder="Search activity…"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setFollowing(true);
              }}
            />
            {query ? (
              <button
                aria-label="Clear activity search"
                onClick={() => setQuery("")}
              >
                <X size={13} />
              </button>
            ) : null}
          </label>
          <label className="ai-case-filter">
            <span className="sr-only">Filter activity by case</span>
            <select
              aria-label="Filter activity by case"
              value={caseId}
              onChange={(event) => filterCase(event.target.value)}
            >
              <option value="">All cases</option>
              {cases.map((row) => (
                <option key={str(row.case_id)} value={str(row.case_id)}>
                  {str(row.case_id)} · {str(row.title, "Case")}
                </option>
              ))}
            </select>
          </label>
          <span className="ai-activity-count">{visible.length} updates</span>
        </div>
        {sample && replay ? (
          <div className="ai-replay-bar">
            <div>
              <CirclePlay size={15} />
              <strong>Recorded fixture</strong>
              <span>Actual A2A traffic · simulated decisions & calls</span>
            </div>
            <div className="ai-replay-controls">
              <button
                className="ai-icon-button"
                aria-label={playing ? "Pause replay" : "Play replay"}
                onClick={() => {
                  if (cursor >= replay.messages.length) setCursor(0);
                  setPlaying(!playing);
                }}
              >
                {playing ? <CirclePause size={19} /> : <CirclePlay size={19} />}
              </button>
              <input
                type="range"
                aria-label="Replay progress"
                min="0"
                max={replay.messages.length}
                value={cursor}
                onChange={(event) => {
                  setCursor(Number(event.target.value));
                  setPlaying(false);
                  setFollowing(true);
                }}
              />
              <span>
                {cursor}/{replay.messages.length}
              </span>
              <button
                className="ai-icon-button"
                aria-label="Restart replay"
                onClick={() => {
                  setCursor(0);
                  setPlaying(true);
                  setFollowing(true);
                }}
              >
                <RotateCcw size={15} />
              </button>
              <button className="ai-text-button" onClick={leaveSample}>
                Back to live
              </button>
            </div>
          </div>
        ) : state !== "live" ? (
          <div className="ai-connection-note">
            <Radio size={15} />
            <span>
              {state === "fallback"
                ? `Showing the latest saved snapshot${lastUpdated ? ` · updated ${timeLabel(lastUpdated)}` : ""}.`
                : state === "connecting"
                  ? "Connecting to your team’s activity…"
                  : "Connect SpacetimeDB to follow your agents live."}
            </span>
            <button onClick={reconnect}>Reconnect</button>
          </div>
        ) : null}
        {!sample && state === "live" ? (
          <div className="ai-live-summary" aria-label="Live team activity">
            <Activity size={14} aria-hidden="true" />
            <strong>{onlineCount}/9 online</strong>
            <span>{workingCount} working</span>
            {syntheticVisible ? (
              <span className="ai-live-source">
                Includes synthetic source data
              </span>
            ) : null}
          </div>
        ) : null}
        <div
          className="ai-transcript"
          ref={scroll}
          onScroll={() => {
            const node = scroll.current;
            if (node)
              setFollowing(
                node.scrollHeight - node.scrollTop - node.clientHeight < 90,
              );
          }}
          tabIndex={0}
          aria-label="Agent conversation history"
        >
          {visible.length ? (
            <div className="ai-message-list">
              {visible.map((message, index) => (
                <div key={message.id}>
                  {index === 0 ||
                  dayLabel(visible[index - 1].at) !== dayLabel(message.at) ? (
                    <div className="ai-day-divider">
                      <span>{dayLabel(message.at)}</span>
                    </div>
                  ) : null}
                  <Message
                    message={message}
                    openDetails={inspect}
                    filterCase={filterCase}
                  />
                </div>
              ))}
              {sample && replay && cursor >= replay.messages.length ? (
                <p className="ai-replay-ended">
                  <CheckCheck size={14} /> End of recording · no live actions
                  were taken
                </p>
              ) : null}
            </div>
          ) : (
            <div className="ai-empty">
              <div className="ai-empty-avatars">
                <Avatar id="carrier_ap" />
                <Avatar id="documents" />
                <Avatar id="risk" />
              </div>
              <span className="ai-eyebrow">
                {query || caseId
                  ? "NO MATCHING ACTIVITY"
                  : active
                    ? active.name.toUpperCase()
                    : "YOUR TEAM IS READY"}
              </span>
              <h2>
                {query || caseId
                  ? "Nothing here matches those filters."
                  : sample
                    ? "The conversation is about to begin."
                    : active
                      ? "Ready for the next assignment."
                      : "Watch the work come together."}
              </h2>
              <p>
                {query || caseId
                  ? "Try another case or a different search."
                  : active
                    ? `${active.role}. Task updates, handoffs, and evidence will appear here when this specialist gets to work.`
                    : "A missing POD. An unexpected charge. A payment that needs a second look. Follow your specialists as they hand work to each other and report back."}
              </p>
              {query || caseId ? (
                <button className="ai-primary-button" onClick={clearFilters}>
                  Clear filters
                </button>
              ) : !sample ? (
                <button
                  className="ai-primary-button"
                  onClick={playSample}
                  disabled={loadingReplay}
                >
                  <CirclePlay size={17} />
                  {loadingReplay
                    ? "Loading recording…"
                    : "Play a recorded case"}
                </button>
              ) : null}
              <small>
                {!sample && !query && !caseId
                  ? "A real nine-service test run. Clearly labeled, with no live calls."
                  : null}
              </small>
              {replayError ? (
                <p role="alert" className="ai-error">
                  {replayError}
                </p>
              ) : null}
            </div>
          )}
        </div>
        {!following && visible.length > 0 ? (
          <button className="ai-jump" onClick={() => setFollowing(true)}>
            <ArrowDown size={14} />
            {visible.length > readCount.current
              ? `${visible.length - readCount.current} new updates · `
              : ""}
            Jump to latest
          </button>
        ) : null}
        <div className="ai-conversation-footer">
          <div className="ai-watching">
            <Activity size={15} />
            <span>
              {sample
                ? "Exploring a recorded run"
                : state === "live"
                  ? workingCount
                    ? `${workingCount} specialist${workingCount > 1 ? "s" : ""} working`
                    : "Following your brokerage team"
                  : "Ready when your agents are"}
              <small>Read-only activity · tool calls, handoffs & results</small>
            </span>
          </div>
          <div>
            {!sample && allMessages.length > 0 ? (
              <button className="ai-text-button" onClick={playSample}>
                View sample
              </button>
            ) : null}
            <button
              className="ai-approval-link"
              onClick={() => navigate("inbox")}
            >
              <Inbox size={15} />
              Human inbox{pending ? <b>{pending}</b> : null}
              <ArrowRight size={13} />
            </button>
          </div>
        </div>
        <span className="sr-only" role="status" aria-live="polite">
          {visible.length} activity updates in{" "}
          {active?.name || "team conversation"}.
        </span>
      </div>
      {details ? (
        <aside className="ai-inspector" aria-label="Activity details">
          <div className="ai-inspector-heading">
            <span>ACTIVITY DETAILS</span>
            <button
              className="ai-icon-button"
              aria-label="Close activity details"
              ref={detailsClose}
              onClick={() => setDetails(null)}
            >
              <X size={18} />
            </button>
          </div>
          <Avatar id={details.agentId} />
          <h2>{details.title}</h2>
          <p>{agentName(details.agentId)}</p>
          {details.toolCall ? <div className="ai-tool-inspector-status" aria-live="polite">
            <ToolStatus status={details.toolCall.status} />
            {details.toolCall.durationMs !== undefined ? <span>{durationLabel(details.toolCall.durationMs)}</span> : null}
          </div> : null}
          <dl>
            {details.toolCall ? <>
              <dt>Tool</dt><dd><code>{details.toolCall.name}</code></dd>
              <dt>Call</dt><dd>{details.toolCall.id}</dd>
            </> : null}
            <dt>Case</dt>
            <dd>{details.caseId || "Not recorded"}</dd>
            <dt>Task</dt>
            <dd>{details.taskId || "Not recorded"}</dd>
            <dt>Recorded</dt>
            <dd>
              {details.at
                ? new Date(details.at).toLocaleString()
                : "Time unavailable"}
            </dd>
            {details.model ? (
              <>
                <dt>Model</dt>
                <dd>{details.model}</dd>
              </>
            ) : null}
            {details.syntheticSource ? (
              <>
                <dt>Case data</dt>
                <dd>Synthetic demonstration records</dd>
              </>
            ) : null}
            {details.liveExternalEvidence ? (
              <>
                <dt>Tool evidence</dt>
                <dd>Live external observations · requires independent review</dd>
              </>
            ) : null}
          </dl>
          {details.toolCall ? <section className="ai-tool-data" aria-label="Tool input and output">
            <h3>Arguments</h3>
            <pre>{JSON.stringify(details.toolCall.arguments, null, 2)}</pre>
            <h3>Result</h3>
            {details.toolCall.result !== null
              ? <pre>{JSON.stringify(details.toolCall.result, null, 2)}</pre>
              : <p className="ai-detail-empty">No result recorded yet.</p>}
            <p className="ai-detail-empty">Sensitive fields are hidden. Long values may be shortened.</p>
            <h3>Call activity</h3>
            <ol className="ai-tool-history">
              {details.toolCall.history.map((event) => <li key={`${event.at}:${event.status}`}>
                <ToolStatus status={event.status} />
                <time dateTime={event.at ? new Date(event.at).toISOString() : undefined}>{timeLabel(event.at)}</time>
              </li>)}
            </ol>
          </section> : null}
          <h3>Evidence</h3>
          {details.evidenceRefs.length ? (
            <ul>
              {details.evidenceRefs.map((ref) => (
                <li key={ref}>
                  {safeUrl(ref) ? (
                    <a href={safeUrl(ref)} target="_blank" rel="noreferrer">
                      {ref}
                      <ExternalLink size={12} />
                    </a>
                  ) : (
                    <span>{ref}</span>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="ai-detail-empty">
              No source references attached to this update.
            </p>
          )}
          {details.policyIds.length ? (
            <>
              <h3>Policy checks</h3>
              <div className="ai-policy-tags">
                {details.policyIds.map((id) => (
                  <span key={id}>{id}</span>
                ))}
              </div>
            </>
          ) : null}
          {!details.toolCall ? <details>
            <summary>Stored event data</summary>
            <pre>{JSON.stringify(details.details || {}, null, 2)}</pre>
          </details> : null}
          {details.caseId && !sample ? (
            <button
              className="ai-primary-button"
              onClick={() => openCase(details.caseId)}
            >
              Open case workspace <ArrowRight size={14} />
            </button>
          ) : null}
          {sample ? (
            <p className="ai-detail-empty">
              Fixture recording. This does not change live cases.
            </p>
          ) : null}
        </aside>
      ) : null}
    </section>
  );
}
