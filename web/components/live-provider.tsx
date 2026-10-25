"use client";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { DbConnection } from "@/module_bindings";
import {
  camel,
  emptySnapshot,
  type HumanReducer,
  type Row,
  type Snapshot,
  type TableName,
} from "@/lib/data";
import {
  caseQueries,
  liveWindowMinutes,
  snapshotBuilder,
  subscriptionPlan,
  windowCutoff,
  type LiveProfile,
  type PlannedQuery,
} from "@/lib/live-queries";

type ConnectionState = "connecting" | "live" | "fallback" | "offline";
type LiveContext = {
  data: Snapshot;
  state: ConnectionState;
  error: string;
  identity: string;
  serverUrl: string;
  databaseName: string;
  lastUpdated: number;
  /**
   * Bumped whenever rows arrive in bulk rather than as live changes (initial
   * load, a case's history loading, the window sliding). Consumers that diff
   * snapshots for animation treat the next snapshot as a new baseline.
   */
  epoch: number;
  /** Append-only tables hold rows newer than `since` only (null: no window). */
  historyWindow: { minutes: number; since: string | null };
  /**
   * Keep one case's full history live, whatever its age, until the returned
   * release function is called. Reference-counted per case id.
   */
  focusCase: (caseId: string) => () => void;
  reconnect: () => void;
  mutate: (name: HumanReducer, payload: Row) => Promise<void>;
};
const Context = createContext<LiveContext | null>(null);
type TableHandle = {
  iter: () => Iterable<unknown>;
  onInsert: (cb: () => void) => void;
  onUpdate?: (cb: () => void) => void;
  onDelete: (cb: () => void) => void;
};
type SubscriptionHandle = {
  isActive: () => boolean;
  isEnded: () => boolean;
  unsubscribe: () => void;
};
/** A subscription that can be released before the server has applied it. */
type Sub = { release: () => void };
const database = process.env.NEXT_PUBLIC_SPACETIME_DB || "freightops";
const uri = process.env.NEXT_PUBLIC_SPACETIME_URL || "http://localhost:3000";
const tokenKey = `freightops.identity.v1:${uri}:${database}`;
/** At most this many focused cases ride along on the read-only fallback. */
const FALLBACK_CASES = 4;
/**
 * `profile` picks which tables are subscribed and how (see `lib/live-queries`).
 * The default, `ops`, is the legacy `/ops` dashboard's superset.
 */
export function LiveProvider({
  children,
  profile = "ops",
}: {
  children: ReactNode;
  profile?: LiveProfile;
}) {
  const [data, setData] = useState<Snapshot>(emptySnapshot);
  const [state, setState] = useState<ConnectionState>("connecting");
  const [error, setError] = useState("");
  const [identity, setIdentity] = useState("");
  const [lastUpdated, setLastUpdated] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [epoch, setEpoch] = useState(0);
  const [minutes] = useState(() => liveWindowMinutes());
  const [since, setSince] = useState<string | null>(() =>
    windowCutoff(Date.now(), minutes),
  );
  const connection = useRef<DbConnection | null>(null);
  const connected = useRef(false);
  const retries = useRef(0);
  const focused = useRef(new Map<string, number>());
  const syncFocus = useRef<() => void>(() => {});
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let slideTimer: ReturnType<typeof setInterval> | undefined;
    let polling = false;
    let ready = false;
    const controller = new AbortController();
    connected.current = false;
    setState("connecting");
    const token = localStorage.getItem(tokenKey) ?? undefined;
    const fallback = async () => {
      if (disposed || ready || polling) return;
      polling = true;
      try {
        const params = new URLSearchParams({ profile });
        const cases = [...focused.current.keys()].slice(0, FALLBACK_CASES);
        if (cases.length) params.set("case", cases.join(","));
        const response = await fetch(`/api/snapshot?${params}`, {
          cache: "no-store",
          signal: controller.signal,
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.error ?? "Fallback unavailable");
        if (!disposed && !ready) {
          setData(body.tables);
          setState("fallback");
          setLastUpdated(Date.now());
        }
      } catch (err) {
        if (!disposed && !ready) {
          setState("offline");
          setError(
            err instanceof Error ? err.message : "SpacetimeDB is unavailable",
          );
        }
      } finally {
        polling = false;
      }
    };
    const lost = (message: string) => {
      if (disposed) return;
      ready = false;
      connected.current = false;
      syncFocus.current = () => {};
      if (slideTimer) clearInterval(slideTimer);
      slideTimer = undefined;
      setError(message);
      setState("connecting");
      void fallback();
      if (!retryTimer) {
        const delay = Math.min(
          30000,
          2000 * 2 ** Math.min(retries.current++, 4),
        );
        retryTimer = setTimeout(() => {
          if (!disposed) setAttempt((value) => value + 1);
        }, delay);
      }
    };
    try {
      let cutoff = windowCutoff(Date.now(), minutes);
      const plan = subscriptionPlan(profile, cutoff);
      let current = emptySnapshot();
      let rebuild: ReturnType<typeof snapshotBuilder> | undefined;
      const dirty = new Set<TableName>();
      let bulk = false;
      const conn = DbConnection.builder()
        .withUri(uri)
        .withDatabaseName(database)
        .withToken(token)
        .onConnect((client, currentIdentity, freshToken) => {
          if (disposed) {
            client.disconnect();
            return;
          }
          localStorage.setItem(tokenKey, freshToken);
          setIdentity(currentIdentity.toHexString());
          const tables = client.db as unknown as Record<string, TableHandle>;
          rebuild = snapshotBuilder((name) => tables[camel(name)].iter());
          const refresh = () => {
            if (disposed || !ready || !dirty.size || !rebuild) return;
            current = rebuild(current, dirty);
            dirty.clear();
            setData(current);
            if (bulk) {
              bulk = false;
              setEpoch((value) => value + 1);
            }
            setLastUpdated(Date.now());
          };
          const schedule = () => {
            if (!timer)
              timer = setTimeout(() => {
                timer = undefined;
                refresh();
              }, 40);
          };
          /** Rows just arrived as a batch, not as live changes. */
          const bulkLoaded = (queries: PlannedQuery[]) => {
            bulk = true;
            for (const query of queries) dirty.add(query.table);
            schedule();
          };
          for (const name of plan.tables) {
            const table = tables[camel(name)];
            const touch = () => {
              dirty.add(name);
              schedule();
            };
            table.onInsert(touch);
            table.onDelete(touch);
            table.onUpdate?.(touch);
          }
          const subscribe = (
            queries: PlannedQuery[],
            onApplied: () => void,
            onError: (message: string) => void,
          ): Sub => {
            let released = false;
            // Callbacks run only after the server answers, so `handle` is set by then.
            const end = () => {
              try {
                if (handle.isActive() && !handle.isEnded()) handle.unsubscribe();
              } catch {
                /* connection already closed */
              }
            };
            const handle: SubscriptionHandle = client
              .subscriptionBuilder()
              .onApplied(() => {
                if (disposed) return;
                if (released) return end();
                onApplied();
              })
              .onError((ctx) => {
                if (!disposed && !released) onError(String(ctx.event));
              })
              .subscribe(queries.map((query) => query.sql));
            return {
              release: () => {
                if (released) return;
                released = true;
                end();
              },
            };
          };
          let pending = 1 + (plan.windowed.length ? 1 : 0);
          const applied = () => {
            if (--pending > 0 || ready) return;
            ready = true;
            connected.current = true;
            retries.current = 0;
            if (retryTimer) clearTimeout(retryTimer);
            retryTimer = undefined;
            setState("live");
            setError("");
            setSince(cutoff);
            syncFocus.current = focus;
            focus();
            bulkLoaded([...plan.base, ...plan.windowed]);
            refresh();
            if (plan.windowed.length) slideTimer = setInterval(slide, 30_000);
          };
          const failed = (message: string) =>
            lost(`Subscription unavailable: ${message}`);
          subscribe(plan.base, applied, failed);
          let windowSub = plan.windowed.length
            ? subscribe(plan.windowed, applied, failed)
            : undefined;
          // Slide the window forward: subscribe the newer cutoff, then drop the
          // old one once the new one is applied, so shared rows never blink out.
          // The new window is a subset of the old one, so nothing arrives in
          // bulk; rows leaving the window come through the delete callbacks.
          let sliding = false;
          const slide = () => {
            const next = windowCutoff(Date.now(), minutes);
            if (!ready || sliding || !next || next === cutoff) return;
            sliding = true;
            const queries = subscriptionPlan(profile, next).windowed;
            const nextSub: Sub = subscribe(
              queries,
              () => {
                sliding = false;
                windowSub?.release();
                windowSub = nextSub;
                cutoff = next;
                setSince(next);
              },
              failed,
            );
          };
          // One subscription per focused case: its windowed tables, unbounded in time.
          const caseSubs = new Map<string, Sub>();
          const focus = () => {
            if (disposed || !ready) return;
            for (const [id, sub] of caseSubs)
              if (!focused.current.has(id)) {
                sub.release();
                caseSubs.delete(id);
              }
            for (const id of focused.current.keys()) {
              if (caseSubs.has(id)) continue;
              const queries = caseQueries(profile, id, cutoff);
              if (!queries.length) continue;
              caseSubs.set(
                id,
                subscribe(
                  queries,
                  () => bulkLoaded(queries),
                  // A case that cannot load its history still shows its windowed rows.
                  () => caseSubs.delete(id),
                ),
              );
            }
          };
        })
        .onConnectError((_ctx, err) =>
          lost(err?.message || "Cannot connect to SpacetimeDB"),
        )
        .onDisconnect((_ctx, err) =>
          lost(err?.message || "Live connection closed"),
        )
        .build();
      connection.current = conn;
    } catch (err) {
      lost(err instanceof Error ? err.message : "Connection failed");
    }
    const initialFallback = setTimeout(() => void fallback(), 4000);
    const interval = setInterval(() => {
      if (!ready) void fallback();
    }, 5000);
    return () => {
      disposed = true;
      connected.current = false;
      syncFocus.current = () => {};
      controller.abort();
      clearTimeout(initialFallback);
      clearInterval(interval);
      if (slideTimer) clearInterval(slideTimer);
      if (timer) clearTimeout(timer);
      if (retryTimer) clearTimeout(retryTimer);
      connection.current?.disconnect();
      connection.current = null;
    };
  }, [attempt, profile, minutes]);
  const focusCase = useCallback((caseId: string) => {
    if (!caseId) return () => {};
    const counts = focused.current;
    counts.set(caseId, (counts.get(caseId) ?? 0) + 1);
    syncFocus.current();
    let done = false;
    return () => {
      if (done) return;
      done = true;
      const left = (counts.get(caseId) ?? 1) - 1;
      if (left > 0) counts.set(caseId, left);
      else counts.delete(caseId);
      syncFocus.current();
    };
  }, []);
  const mutate = useCallback(async (name: HumanReducer, payload: Row) => {
    if (!connection.current || !connected.current)
      throw new Error(
        "Reconnect to live SpacetimeDB before making a change. Fallback snapshots are read-only.",
      );
    const reducers = connection.current.reducers as unknown as Record<
      string,
      (args: { payload: string }) => Promise<void>
    >;
    await reducers[camel(name)]({
      payload: JSON.stringify({ ...payload, write_id: crypto.randomUUID() }),
    });
  }, []);
  return (
    <Context.Provider
      value={{
        data,
        state,
        error,
        identity,
        serverUrl: uri,
        databaseName: database,
        lastUpdated,
        epoch,
        historyWindow: { minutes, since },
        focusCase,
        reconnect: () => {
          retries.current = 0;
          setAttempt((value) => value + 1);
        },
        mutate,
      }}
    >
      {children}
    </Context.Provider>
  );
}
export function useLive() {
  const value = useContext(Context);
  if (!value) throw new Error("LiveProvider is required");
  return value;
}
/** Keep `caseId`'s full history live while the calling component is mounted. */
export function useCaseFocus(caseId: string) {
  const { focusCase } = useLive();
  useEffect(() => focusCase(caseId), [focusCase, caseId]);
}
