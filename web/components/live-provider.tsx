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
  TABLES,
  camel,
  emptySnapshot,
  normalize,
  type HumanReducer,
  type Row,
  type Snapshot,
} from "@/lib/data";

type ConnectionState = "connecting" | "live" | "fallback" | "offline";
type LiveContext = {
  data: Snapshot;
  state: ConnectionState;
  error: string;
  identity: string;
  serverUrl: string;
  databaseName: string;
  lastUpdated: number;
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
const database = process.env.NEXT_PUBLIC_SPACETIME_DB || "freightops";
const uri = process.env.NEXT_PUBLIC_SPACETIME_URL || "http://localhost:3000";
const tokenKey = `freightops.identity.v1:${uri}:${database}`;
export function snapshotAuthorizationHeaders(): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    const token = window.localStorage.getItem(tokenKey);
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}
export function LiveProvider({ children }: { children: ReactNode }) {
  const [data, setData] = useState<Snapshot>(emptySnapshot);
  const [state, setState] = useState<ConnectionState>("connecting");
  const [error, setError] = useState("");
  const [identity, setIdentity] = useState("");
  const [lastUpdated, setLastUpdated] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const connection = useRef<DbConnection | null>(null);
  const connected = useRef(false);
  const retries = useRef(0);
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
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
        const response = await fetch("/api/snapshot", {
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
      const refreshSnapshot = (client: DbConnection) => {
        const snapshot = emptySnapshot();
        const tables = client.db as unknown as Record<string, TableHandle>;
        for (const name of TABLES)
          snapshot[name] = Array.from(
            tables[camel(name)].iter(),
            (row) => normalize(row) as Row,
          );
        setData(snapshot);
        setLastUpdated(Date.now());
      };
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
          const refresh = () => {
            if (disposed || !ready) return;
            refreshSnapshot(client);
          };
          const schedule = () => {
            if (!timer)
              timer = setTimeout(() => {
                timer = undefined;
                refresh();
              }, 40);
          };
          for (const name of TABLES) {
            const table = tables[camel(name)];
            table.onInsert(schedule);
            table.onDelete(schedule);
            table.onUpdate?.(schedule);
          }
          client
            .subscriptionBuilder()
            .onApplied(() => {
              ready = true;
              connected.current = true;
              retries.current = 0;
              if (retryTimer) clearTimeout(retryTimer);
              setState("live");
              setError("");
              refresh();
            })
            .onError((ctx) =>
              lost(`Subscription unavailable: ${String(ctx.event)}`),
            )
            .subscribe(TABLES.map((name) => `SELECT * FROM "${name}"`));
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
      controller.abort();
      clearTimeout(initialFallback);
      clearInterval(interval);
      if (timer) clearTimeout(timer);
      if (retryTimer) clearTimeout(retryTimer);
      connection.current?.disconnect();
      connection.current = null;
    };
  }, [attempt]);
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
