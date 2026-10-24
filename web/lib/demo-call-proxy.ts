/** Local operator control. Provider and gateway credentials never reach the browser. */
type Environment = Record<string, string | undefined>;
type Dependencies = { env: Environment; fetcher: typeof fetch };
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const loopback = new Set(["localhost", "127.0.0.1", "[::1]"]);
const states = new Set(["queued", "working", "waiting", "completed", "failed", "canceled", "cancelled", "rejected", "uncertain"]);

function reply(body: unknown, status = 200) {
  return Response.json(body, { status, headers: { "Cache-Control": "no-store" } });
}

function localRequest(request: Request): boolean {
  try {
    const host = request.headers.get("host");
    const url = new URL(request.url);
    const actual = new URL(`${url.protocol}//${host}`);
    if (!host || !loopback.has(actual.hostname) || actual.host !== host || actual.username || actual.password) return false;
    const origin = request.headers.get("origin");
    if (request.method === "POST" || origin) {
      const parsed = new URL(origin || "");
      if (parsed.origin !== `${url.protocol}//${host}`) return false;
    }
    const site = request.headers.get("sec-fetch-site");
    return !site || site === "same-origin" || site === "none";
  } catch {
    return false;
  }
}

function safeStatus(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const body = value as Record<string, unknown>;
  if (typeof body.enabled !== "boolean" || typeof body.ready !== "boolean") return null;
  const result: Record<string, unknown> = {
    enabled: body.enabled, ready: body.ready,
    reason: typeof body.reason === "string" ? body.reason.slice(0, 500) : "",
    destination_suffix: typeof body.destination_suffix === "string" && /^\d{4}$/.test(body.destination_suffix)
      ? body.destination_suffix : "",
    hours_override: body.hours_override === true,
    case_id: body.case_id === "SYN-CARTESIA-TEAM-CALL" ? body.case_id : "",
  };
  if (body.request !== undefined && body.request !== null) {
    if (typeof body.request !== "object" || Array.isArray(body.request)) return null;
    const request = body.request as Record<string, unknown>;
    if (typeof request.request_id !== "string" || !uuid.test(request.request_id) ||
        typeof request.task_id !== "string" || !uuid.test(request.task_id) ||
        typeof request.state !== "string" || !states.has(request.state)) return null;
    result.request = {
      request_id: request.request_id, task_id: request.task_id, state: request.state,
      retryable: request.retryable === true,
      ...(typeof request.call_status === "string" && /^[a-z_]{1,40}$/.test(request.call_status)
        ? { call_status: request.call_status } : {}),
      message: typeof request.message === "string" ? request.message.slice(0, 500) : "",
    };
  }
  return result;
}

export async function demoCallProxy(request: Request, dependencies?: Dependencies): Promise<Response> {
  const { env, fetcher } = dependencies ?? { env: process.env, fetcher: fetch };
  if (!["GET", "POST"].includes(request.method)) return reply({ error: "Method not allowed." }, 405);
  if (!localRequest(request)) return reply({ error: "Use the local dashboard to control demo calls." }, 403);
  if (env.DEMO_PHONE_ENABLED !== "1" || !env.DEMO_CONTROL_TOKEN) {
    return reply({ enabled: false, ready: false, reason: "Demo phone control is not configured.",
      destination_suffix: "", hours_override: false, case_id: "" }, request.method === "GET" ? 200 : 503);
  }
  let gateway: URL;
  try {
    gateway = new URL(env.GATEWAY_URL || "http://127.0.0.1:9000");
    if (!loopback.has(gateway.hostname) || !["http:", "https:"].includes(gateway.protocol) ||
        gateway.username || gateway.password || gateway.search || gateway.hash || gateway.pathname !== "/") throw new Error();
  } catch {
    return reply({ error: "The local demo gateway is not configured correctly." }, 503);
  }
  let body: string | undefined;
  const query = new URL(request.url).searchParams;
  const requestId = query.get("request_id");
  if ([...query.keys()].some((key) => key !== "request_id") || query.getAll("request_id").length > 1 ||
      (requestId !== null && (!uuid.test(requestId) || request.method !== "GET"))) {
    return reply({ error: "Invalid demo status request." }, 400);
  }
  if (request.method === "POST") {
    if (request.headers.get("content-type")?.split(";")[0].trim() !== "application/json") {
      return reply({ error: "JSON request required." }, 400);
    }
    const raw = await request.text();
    if (raw.length > 1024) return reply({ error: "Request is too large." }, 413);
    try {
      const payload = JSON.parse(raw);
      if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
          Object.keys(payload).length !== 1 || typeof payload.request_id !== "string" || !uuid.test(payload.request_id)) throw new Error();
      body = JSON.stringify({ request_id: payload.request_id.toLowerCase() });
    } catch {
      return reply({ error: "A valid demo request ID is required." }, 400);
    }
  }
  try {
    const target = new URL("/demo/team-call", gateway);
    if (requestId) target.searchParams.set("request_id", requestId.toLowerCase());
    const response = await fetcher(target, {
      method: request.method, headers: { Authorization: `Bearer ${env.DEMO_CONTROL_TOKEN}`,
        ...(body ? { "Content-Type": "application/json" } : {}) },
      ...(body ? { body } : {}), cache: "no-store", signal: AbortSignal.timeout(10_000),
      redirect: "error",
    });
    const raw = await response.text();
    if (raw.length > 32_000) throw new Error();
    const status = safeStatus(JSON.parse(raw));
    if (response.ok && status) return reply(status, response.status);
    if (status && [409, 429, 503].includes(response.status)) return reply(status, response.status);
    return reply({ error: "The demo gateway could not accept the request. Retry with the same request ID." }, 502);
  } catch {
    return reply({ error: "The demo gateway is unavailable. If you clicked Call my phone, its status is unknown; retry the same request." }, 503);
  }
}
