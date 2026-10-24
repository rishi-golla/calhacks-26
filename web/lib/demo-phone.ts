export type DemoCallRequest = {
  request_id: string;
  task_id?: string;
  state: string;
  call_status?: string;
  message?: string;
  retryable?: boolean;
};

export type DemoCallStatus = {
  enabled: boolean;
  ready: boolean;
  reason?: string;
  destination_suffix: string;
  hours_override: boolean;
  case_id: string;
  request?: DemoCallRequest;
};

export type DemoPhoneSnapshot = {
  status: DemoCallStatus | null;
  requestId: string | null;
  loading: boolean;
  submitting: boolean;
  storageReady: boolean;
  error: string;
};

export const DEMO_PHONE_STORAGE_KEY = "freightops.demo-call.v1";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TERMINAL = new Set(["completed", "failed", "canceled", "cancelled", "rejected"]);
const INITIAL: DemoPhoneSnapshot = {
  status: null,
  requestId: null,
  loading: true,
  submitting: false,
  storageReady: false,
  error: "",
};

export function isDemoCallTerminal(request?: DemoCallRequest): boolean {
  return Boolean(request && TERMINAL.has(request.state.toLowerCase()));
}

export function demoPhoneAction(snapshot: DemoPhoneSnapshot) {
  const { status, requestId, loading, submitting, storageReady } = snapshot;
  const request = status?.request;
  const matched = request?.request_id === requestId;
  const active = Boolean(request && !isDemoCallTerminal(request));
  const recoverable = matched && request?.retryable === true && !isDemoCallTerminal(request);
  const retry = recoverable || Boolean(requestId && !matched && !active);
  const terminal = matched && isDemoCallTerminal(request);
  return {
    disabled: loading || submitting || (active && !recoverable) || !storageReady || !status?.enabled || (!status.ready && !recoverable),
    retry,
    active,
    terminal,
    label: submitting
      ? "Starting call…"
      : retry
        ? "Retry call request"
        : active
          ? request?.state === "uncertain" ? "Checking call status…" : "Call in progress"
          : terminal
            ? "Call my phone again"
            : "Call my phone",
  };
}

function parseStatus(value: unknown): DemoCallStatus {
  if (!value || typeof value !== "object") throw new Error("Invalid call status");
  const body = value as Record<string, unknown>;
  if (typeof body.enabled !== "boolean" || typeof body.ready !== "boolean")
    throw new Error("Invalid call status");
  let request: DemoCallRequest | undefined;
  if (body.request != null) {
    const item = body.request as Record<string, unknown>;
    if (typeof item.request_id !== "string" || !UUID.test(item.request_id) || typeof item.state !== "string")
      throw new Error("Invalid call request");
    request = {
      request_id: item.request_id,
      state: item.state,
      task_id: typeof item.task_id === "string" ? item.task_id : undefined,
      call_status: typeof item.call_status === "string" ? item.call_status : undefined,
      message: typeof item.message === "string" ? item.message : undefined,
      retryable: item.retryable === true,
    };
  }
  return {
    enabled: body.enabled,
    ready: body.ready,
    reason: typeof body.reason === "string" ? body.reason : undefined,
    destination_suffix: typeof body.destination_suffix === "string" && /^\d{4}$/.test(body.destination_suffix)
      ? body.destination_suffix : "",
    hours_override: body.hours_override === true,
    case_id: typeof body.case_id === "string" ? body.case_id : "",
    request,
  };
}

class DemoStatusError extends Error {}

async function readStatus(response: Response): Promise<DemoCallStatus> {
  const body: unknown = await response.json();
  try {
    return parseStatus(body);
  } catch {
    if (body && typeof body === "object" && "error" in body && typeof body.error === "string")
      throw new DemoStatusError(body.error.slice(0, 500));
    throw new Error("Invalid call status");
  }
}

type Dependencies = {
  fetcher: (url: string, options?: RequestInit) => Promise<Response>;
  storage: () => Pick<Storage, "getItem" | "setItem">;
  uuid: () => string;
};

/** Only an explicit start() posts. Reads and reload recovery never place a call. */
export class DemoPhoneClient {
  private value = INITIAL;
  private listeners = new Set<() => void>();
  private initialized = false;
  private refreshing = false;
  private revision = 0;

  constructor(private dependencies: Dependencies) {}

  snapshot = () => this.value;
  serverSnapshot = () => INITIAL;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  private update(change: Partial<DemoPhoneSnapshot>) {
    this.value = { ...this.value, ...change };
    this.listeners.forEach((listener) => listener());
  }
  private save(requestId: string) {
    this.dependencies.storage().setItem(DEMO_PHONE_STORAGE_KEY, JSON.stringify({ request_id: requestId }));
  }
  private initialize() {
    if (this.initialized) return;
    this.initialized = true;
    try {
      const saved = this.dependencies.storage().getItem(DEMO_PHONE_STORAGE_KEY);
      const requestId: unknown = saved ? JSON.parse(saved).request_id : null;
      if (requestId !== null && (typeof requestId !== "string" || !UUID.test(requestId)))
        throw new Error("Invalid saved request");
      this.update({ requestId, storageReady: true });
    } catch {
      this.update({ error: "Your browser could not read the saved call request. Allow site storage before trying again." });
    }
  }
  private accept(status: DemoCallStatus) {
    // Adopt an existing server request on a fresh tab, but never discard an
    // unresolved local ID merely because another request is now latest.
    if (this.value.storageReady && !this.value.requestId && status.request) {
      try {
        this.save(status.request.request_id);
        this.update({ requestId: status.request.request_id });
      } catch {
        this.update({ storageReady: false, error: "Your browser could not save the call request. Allow site storage before trying again." });
      }
    }
    this.update({ status, loading: false });
  }

  async refresh(signal?: AbortSignal) {
    this.initialize();
    if (this.refreshing || this.value.submitting) return;
    this.refreshing = true;
    const revision = this.revision;
    const query = this.value.requestId ? `?request_id=${encodeURIComponent(this.value.requestId)}` : "";
    try {
      const response = await this.dependencies.fetcher(`/api/demo-call${query}`, {
        cache: "no-store", signal: signal
          ? AbortSignal.any([signal, AbortSignal.timeout(12_000)])
          : AbortSignal.timeout(12_000),
      });
      const status = await readStatus(response);
      if (revision !== this.revision || signal?.aborted) return;
      this.accept(status);
      if (this.value.storageReady) this.update({ error: "" });
    } catch (error) {
      if (revision === this.revision && !signal?.aborted)
        this.update({ loading: false, error: error instanceof DemoStatusError ? error.message
          : "Call status is temporarily unavailable. We’ll keep checking; your request is saved." });
    } finally {
      this.refreshing = false;
    }
  }

  async start() {
    if (demoPhoneAction(this.value).disabled) return;
    const previous = this.value.status?.request;
    const confirmedTerminal = previous?.request_id === this.value.requestId && isDemoCallTerminal(previous);
    const requestId = this.value.requestId && !confirmedTerminal ? this.value.requestId : this.dependencies.uuid();
    if (!UUID.test(requestId)) return;
    try {
      // Persist before sending: an ambiguous response or reload must reuse this ID.
      this.save(requestId);
    } catch {
      this.update({ storageReady: false, error: "Your browser could not save the call request. Allow site storage before trying again." });
      return;
    }
    this.revision += 1;
    this.update({ requestId, submitting: true, error: "" });
    try {
      const response = await this.dependencies.fetcher("/api/demo-call", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId }),
        signal: AbortSignal.timeout(20_000),
      });
      const status = await readStatus(response);
      this.accept(status);
      if (!response.ok) {
        this.update({ error: status.reason || "The call request could not start. Your saved request will be reused when you retry." });
      } else if (status.request?.request_id !== requestId) {
        throw new Error("Call admission unconfirmed");
      }
    } catch (error) {
      this.update({ error: error instanceof DemoStatusError ? error.message
        : "We couldn’t confirm the call request. Retry uses the same request and won’t start a second call." });
    } finally {
      this.update({ submitting: false });
    }
  }
}
