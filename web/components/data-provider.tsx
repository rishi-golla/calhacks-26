"use client";
/**
 * Single data source for the FreightLine UI.
 *
 * Wraps the live SpacetimeDB `LiveProvider` and the client-side `DemoSim`.
 * Pages call `useOps()` and never care which one is feeding them, but the
 * `source` field is always honest: "live" / "fallback" rows come from the
 * database; "simulation" rows are generated in this browser and are labeled
 * as such everywhere they appear.
 *
 * Source selection:
 *   - `?data=sim` or `NEXT_PUBLIC_DATA_SOURCE=sim` forces the simulation.
 *   - `?data=live` forces the database.
 *   - default ("auto"): use the database when it is live or has a fallback
 *     snapshot with rows; otherwise show the simulation so the product is
 *     never a blank page.
 *
 * "Start fresh": when a `view_epoch` is set, `snapshot` and every derived view
 * are scoped to activity since it (lib/view-epoch), unless this viewer chose
 * "Show earlier activity". `fullSnapshot` and `allCases` stay unscoped for the
 * case page, so a direct link to an earlier case still opens.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { usePathname } from "next/navigation";
import { emptySnapshot, type HumanReducer, type Row, type Snapshot } from "@/lib/data";
import { DemoSim, type SimOptions } from "@/lib/demo-sim";
import {
  buildAttention,
  buildCases,
  buildFinance,
  buildOpsMetrics,
  buildWorkloads,
  diffSnapshots,
  type AttentionItem,
  type CaseView,
  type FinanceMetrics,
  type LiveEvent,
  type OpsMetrics,
  type AgentWorkload,
} from "@/lib/ops";
import type { AgentId } from "@/lib/agents";
import { caseIdFromPath } from "@/lib/live-queries";
import {
  readViewEpoch,
  scopeEvents,
  scopeSnapshot,
  showEarlierApplies,
  showEarlierValue,
  SHOW_EARLIER_KEY,
  viewScope,
  type ViewEpoch,
  type ViewScope,
} from "@/lib/view-epoch";
import { LiveProvider, useCaseFocus, useLive } from "./live-provider";

export type DataSource = "live" | "fallback" | "simulation" | "connecting" | "offline";
export type DataMode = "auto" | "live" | "sim";

type EventListener = (events: LiveEvent[]) => void;
type EventScope = { unscoped?: boolean };

/**
 * `ready`: Start fresh works here. `pending`: the live module is still being
 * asked. `absent`: the module (or these bindings) predate `view_epoch`.
 * `read-only`: not on a live connection. `observer`: this browser has no
 * identity yet, or its principal was deactivated, so `set_view_epoch` would be refused.
 */
export type ViewEpochSupport = "ready" | "pending" | "absent" | "read-only" | "observer";

export const OBSERVER_HINT = "This dashboard's identity is not allowed to start fresh.";

export type ViewEpochControl = {
  /** The shared epoch (the simulation keeps its own), or null. */
  epoch: ViewEpoch | null;
  /** True when rows before the epoch are hidden for this viewer. */
  active: boolean;
  /** This viewer chose "Show earlier activity" for the current epoch. */
  showEarlier: boolean;
  setShowEarlier: (on: boolean) => void;
  support: ViewEpochSupport;
  /** Cases opened before the epoch kept because they were still in flight. */
  carried: number;
  /** Hide everything before now. Nothing is deleted. */
  set: () => Promise<void>;
  /** Remove the epoch for everyone. */
  clear: () => Promise<void>;
};

type OpsContext = {
  /** Rows in view: scoped to the "Start fresh" epoch when one applies. */
  snapshot: Snapshot;
  /** Every row, whatever the epoch (the case page). */
  fullSnapshot: Snapshot;
  source: DataSource;
  mode: DataMode;
  setMode: (mode: DataMode) => void;
  /** True when rows are generated in the browser. */
  simulated: boolean;
  /** Wall clock, refreshed every second, so "x seconds ago" labels tick. */
  now: number;
  lastUpdated: number;
  error: string;
  identity: string;
  serverUrl: string;
  databaseName: string;
  reconnect: () => void;
  mutate: (name: HumanReducer, payload: Row) => Promise<void>;
  /** Subscribe to deltas between consecutive snapshots (for animation); scoped unless `unscoped`. */
  subscribe: (listener: EventListener, options?: EventScope) => () => void;
  /** Simulation controls (no-ops on live). */
  sim: {
    ratePerMinute: number;
    setRate: (perMinute: number) => void;
    inject: (scenario?: string) => string | null;
  };
  viewEpoch: ViewEpochControl;
  /* Derived views, memoized on the snapshot. */
  cases: Map<string, CaseView>;
  /** Every case, whatever the epoch (the case page). */
  allCases: Map<string, CaseView>;
  caseList: CaseView[];
  workloads: Record<AgentId, AgentWorkload>;
  ops: OpsMetrics;
  finance: FinanceMetrics;
  attention: AttentionItem[];
};

const Context = createContext<OpsContext | null>(null);

const envMode = (): DataMode => {
  const v = process.env.NEXT_PUBLIC_DATA_SOURCE;
  return v === "sim" || v === "live" ? v : "auto";
};

function readModeFromUrl(): DataMode | null {
  if (typeof window === "undefined") return null;
  const v = new URLSearchParams(window.location.search).get("data");
  return v === "sim" || v === "live" || v === "auto" ? v : null;
}

function OpsBridge({ children, simOptions }: { children: ReactNode; simOptions?: SimOptions }) {
  const live = useLive();
  // A case page keeps that case's whole history live; elsewhere append-only
  // tables hold a recent window only (see lib/live-queries).
  useCaseFocus(caseIdFromPath(usePathname()));
  const [mode, setModeState] = useState<DataMode>(envMode);
  const [now, setNow] = useState(() => Date.now());
  const [simSnapshot, setSimSnapshot] = useState<Snapshot>(emptySnapshot);
  const [simRate, setSimRate] = useState(simOptions?.ratePerMinute ?? 8);
  const sim = useRef<DemoSim | null>(null);
  const listeners = useRef(new Set<EventListener>());
  const unscopedListeners = useRef(new Set<EventListener>());
  const previous = useRef<Snapshot | null>(null);
  const seenEpoch = useRef(live.epoch);
  const mountedAt = useRef(Date.now());

  useEffect(() => {
    const fromUrl = readModeFromUrl();
    try {
      if (fromUrl) {
        // A `?data=` choice sticks for later navigations without the param, so a
        // link from a simulated case does not land on the live database.
        setModeState(fromUrl);
        localStorage.setItem("freightline.data-mode", fromUrl);
        return;
      }
      const saved = localStorage.getItem("freightline.data-mode");
      if (saved === "sim" || saved === "live" || saved === "auto") setModeState(saved);
    } catch {
      if (fromUrl) setModeState(fromUrl);
    }
  }, []);
  const setMode = useCallback((next: DataMode) => {
    setModeState(next);
    try {
      localStorage.setItem("freightline.data-mode", next);
    } catch {
      /* storage unavailable */
    }
  }, []);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const liveHasRows = live.data.case.length > 0 || live.data.task.length > 0 || live.data.agent.length > 0;
  const source: DataSource =
    mode === "sim"
      ? "simulation"
      : mode === "live"
        ? live.state === "live"
          ? "live"
          : live.state === "fallback"
            ? "fallback"
            : live.state
        : live.state === "live" && (liveHasRows || live.lastUpdated > 0)
          ? "live"
          : live.state === "fallback" && liveHasRows
            ? "fallback"
            : live.state === "connecting" && Date.now() - mountedAt.current < 3500
              ? "connecting"
              : "simulation";
  const simulated = source === "simulation";

  // Run the simulation only while it is the visible source.
  useEffect(() => {
    if (!simulated) return;
    if (!sim.current) sim.current = new DemoSim({ ...simOptions, ratePerMinute: simRate });
    const engine = sim.current;
    setSimSnapshot(engine.snapshot());
    const timer = setInterval(() => {
      if (engine.tick()) setSimSnapshot(engine.snapshot());
    }, 250);
    return () => clearInterval(timer);
    // simRate is applied through setRate below, not by rebuilding the engine.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [simulated]);

  const fullSnapshot = simulated ? simSnapshot : live.data;

  /* -------- Start fresh: one scope for every view -------- */
  const epoch = useMemo(() => readViewEpoch(fullSnapshot), [fullSnapshot]);
  const [showEarlierFor, setShowEarlierFor] = useState<string | null>(null);
  useEffect(() => {
    try {
      setShowEarlierFor(localStorage.getItem(SHOW_EARLIER_KEY));
    } catch {
      /* storage unavailable */
    }
  }, []);
  const showEarlier = showEarlierApplies(showEarlierFor, epoch);
  const setShowEarlier = useCallback(
    (on: boolean) => {
      const value = on && epoch ? showEarlierValue(epoch) : null;
      setShowEarlierFor(value);
      try {
        if (value) localStorage.setItem(SHOW_EARLIER_KEY, value);
        else localStorage.removeItem(SHOW_EARLIER_KEY);
      } catch {
        /* storage unavailable */
      }
    },
    [epoch],
  );
  const since = epoch && !showEarlier ? epoch.since : undefined;
  const scope = useMemo<ViewScope | null>(() => (since === undefined ? null : viewScope(fullSnapshot, since)), [fullSnapshot, since]);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const previousScoped = useRef<Snapshot | null>(null);
  const snapshot = useMemo(() => {
    const next = scope ? scopeSnapshot(fullSnapshot, scope, previousScoped.current) : fullSnapshot;
    previousScoped.current = scope ? next : null;
    return next;
  }, [fullSnapshot, scope]);
  // `set_view_epoch` is open to any connected identity; only a deactivated principal is refused.
  const canWrite = useMemo(
    () => Boolean(live.identity) && !fullSnapshot.principal.some((row) => row.identity === live.identity && row.active === false),
    [fullSnapshot.principal, live.identity],
  );
  const support: ViewEpochSupport = simulated
    ? "ready"
    : source !== "live"
      ? "read-only"
      : live.optionalTables.view_epoch === "absent"
        ? "absent"
        : live.optionalTables.view_epoch !== "ready"
          ? "pending"
          : canWrite
            ? "ready"
            : "observer";
  const liveMutate = live.mutate;
  const changeEpoch = useCallback(
    async (change: { clear?: true }) => {
      if (simulated) {
        if (!sim.current) return;
        sim.current.setViewEpoch(change);
        setSimSnapshot(sim.current.snapshot());
        return;
      }
      if (support === "absent") throw new Error("Start fresh needs a module update: this database has no view_epoch table yet.");
      if (support === "observer") throw new Error(OBSERVER_HINT);
      await liveMutate("set_view_epoch", change);
    },
    [simulated, support, liveMutate],
  );
  const setEpochNow = useCallback(() => changeEpoch({}), [changeEpoch]);
  const clearEpoch = useCallback(() => changeEpoch({ clear: true }), [changeEpoch]);
  const viewEpoch = useMemo<ViewEpochControl>(
    () => ({ epoch, active: since !== undefined, showEarlier, setShowEarlier, support, carried: scope?.carried.size ?? 0, set: setEpochNow, clear: clearEpoch }),
    [epoch, since, showEarlier, setShowEarlier, support, scope, setEpochNow, clearEpoch],
  );

  // Emit deltas to subscribers. Diffs run on every row so a change of scope
  // never replays history; each event is then scoped like the rows.
  useEffect(() => {
    // The first snapshot after mount or a source switch is history, not news:
    // emitting it would replay thousands of events into the animations. So is
    // a bulk load on the live connection (a case's history, the window sliding).
    const baseline = previous.current;
    previous.current = fullSnapshot;
    const bulk = !simulated && seenEpoch.current !== live.epoch;
    seenEpoch.current = live.epoch;
    if (!baseline || bulk) return;
    const events = diffSnapshots(baseline, fullSnapshot);
    if (!events.length) return;
    for (const l of unscopedListeners.current) l(events);
    const scoped = scopeRef.current ? scopeEvents(events, scopeRef.current) : events;
    if (scoped.length) for (const l of listeners.current) l(scoped);
    // live.epoch changes in the same render as the bulk-loaded snapshot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fullSnapshot]);
  // Reset diff baseline when the source flips so a switch does not replay history as "new".
  useEffect(() => {
    previous.current = null;
  }, [simulated]);

  const subscribe = useCallback((listener: EventListener, options?: EventScope) => {
    const set = options?.unscoped ? unscopedListeners.current : listeners.current;
    set.add(listener);
    return () => {
      set.delete(listener);
    };
  }, []);

  // A case's view reads only its own rows, which scoping keeps whole, so the
  // scoped map is a filter of the full one.
  const allCases = useMemo(() => buildCases(fullSnapshot), [fullSnapshot]);
  const cases = useMemo(
    () => (scope ? new Map([...allCases].filter(([id]) => scope.cases.has(id))) : allCases),
    [allCases, scope],
  );
  const caseList = useMemo(() => [...cases.values()].sort((a, b) => b.openedAt - a.openedAt), [cases]);
  const workloads = useMemo(() => buildWorkloads(snapshot, now, { since }), [snapshot, now, since]);
  const ops = useMemo(() => buildOpsMetrics(snapshot, cases, now), [snapshot, cases, now]);
  const finance = useMemo(() => buildFinance(snapshot, now, { since }), [snapshot, now, since]);
  const attention = useMemo(() => buildAttention(snapshot, cases, now), [snapshot, cases, now]);

  const value = useMemo<OpsContext>(
    () => ({
      snapshot,
      fullSnapshot,
      source,
      mode,
      setMode,
      simulated,
      now,
      lastUpdated: simulated ? now : live.lastUpdated,
      error: live.error,
      identity: live.identity,
      serverUrl: live.serverUrl,
      databaseName: live.databaseName,
      reconnect: live.reconnect,
      mutate: live.mutate,
      subscribe,
      sim: {
        ratePerMinute: simRate,
        setRate: (perMinute: number) => {
          setSimRate(perMinute);
          sim.current?.setRate(perMinute);
        },
        inject: (scenario?: string) => (sim.current ? sim.current.inject(scenario as never) : null),
      },
      viewEpoch,
      cases,
      allCases,
      caseList,
      workloads,
      ops,
      finance,
      attention,
    }),
    [snapshot, fullSnapshot, source, mode, setMode, simulated, now, live.lastUpdated, live.error, live.identity, live.serverUrl, live.databaseName, live.reconnect, live.mutate, subscribe, simRate, viewEpoch, cases, allCases, caseList, workloads, ops, finance, attention],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function DataProvider({ children, simOptions }: { children: ReactNode; simOptions?: SimOptions }) {
  return (
    <LiveProvider profile="app">
      <OpsBridge simOptions={simOptions}>{children}</OpsBridge>
    </LiveProvider>
  );
}

export function useOps(): OpsContext {
  const value = useContext(Context);
  if (!value) throw new Error("DataProvider is required");
  return value;
}

/**
 * Convenience: subscribe to live events with a stable handler. Events are
 * scoped to the "Start fresh" epoch unless `unscoped` (the case page).
 */
export function useLiveEvents(handler: EventListener, { unscoped = false }: EventScope = {}) {
  const { subscribe } = useOps();
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => subscribe((events) => ref.current(events), { unscoped }), [subscribe, unscoped]);
}
