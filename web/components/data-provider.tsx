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
import { LiveProvider, useCaseFocus, useLive } from "./live-provider";

export type DataSource = "live" | "fallback" | "simulation" | "connecting" | "offline";
export type DataMode = "auto" | "live" | "sim";

type EventListener = (events: LiveEvent[]) => void;

type OpsContext = {
  snapshot: Snapshot;
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
  /** Subscribe to deltas between consecutive snapshots (for animation). */
  subscribe: (listener: EventListener) => () => void;
  /** Simulation controls (no-ops on live). */
  sim: {
    ratePerMinute: number;
    setRate: (perMinute: number) => void;
    inject: (scenario?: string) => string | null;
  };
  /* Derived views, memoized on the snapshot. */
  cases: Map<string, CaseView>;
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

  const snapshot = simulated ? simSnapshot : live.data;

  // Emit deltas to subscribers.
  useEffect(() => {
    // The first snapshot after mount or a source switch is history, not news:
    // emitting it would replay thousands of events into the animations. So is
    // a bulk load on the live connection (a case's history, the window sliding).
    const baseline = previous.current;
    previous.current = snapshot;
    const bulk = !simulated && seenEpoch.current !== live.epoch;
    seenEpoch.current = live.epoch;
    if (!baseline || bulk) return;
    const events = diffSnapshots(baseline, snapshot);
    if (events.length) for (const l of listeners.current) l(events);
    // live.epoch changes in the same render as the bulk-loaded snapshot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot]);
  // Reset diff baseline when the source flips so a switch does not replay history as "new".
  useEffect(() => {
    previous.current = null;
  }, [simulated]);

  const subscribe = useCallback((listener: EventListener) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  const cases = useMemo(() => buildCases(snapshot), [snapshot]);
  const caseList = useMemo(() => [...cases.values()].sort((a, b) => b.openedAt - a.openedAt), [cases]);
  const workloads = useMemo(() => buildWorkloads(snapshot, now), [snapshot, now]);
  const ops = useMemo(() => buildOpsMetrics(snapshot, cases, now), [snapshot, cases, now]);
  const finance = useMemo(() => buildFinance(snapshot, now), [snapshot, now]);
  const attention = useMemo(() => buildAttention(snapshot, cases, now), [snapshot, cases, now]);

  const value = useMemo<OpsContext>(
    () => ({
      snapshot,
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
      cases,
      caseList,
      workloads,
      ops,
      finance,
      attention,
    }),
    [snapshot, source, mode, setMode, simulated, now, live.lastUpdated, live.error, live.identity, live.serverUrl, live.databaseName, live.reconnect, live.mutate, subscribe, simRate, cases, caseList, workloads, ops, finance, attention],
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

/** Convenience: subscribe to live events with a stable handler. */
export function useLiveEvents(handler: EventListener) {
  const { subscribe } = useOps();
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => subscribe((events) => ref.current(events)), [subscribe]);
}
