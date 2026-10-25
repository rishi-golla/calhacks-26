/** Local operator load test: forwards a burst request to the gateway with the server-only control token. */
type Environment = Record<string, string | undefined>;
type Dependencies = { env: Environment; fetcher: typeof fetch };
export const BURST_MAX = 200;
const loopback = new Set(["localhost", "127.0.0.1", "[::1]"]);
const burstId = /^BURST-[0-9a-f]{32}$/;
const caseId = /^[A-Za-z0-9._-]{1,36}$/;

export type BurstResult = {
  burst_id: string;
  count: number;
  trigger_event: string;
  case_ids: string[];
  announced_in_s: number;
};

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

export function safeBurst(value: unknown): BurstResult | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const body = value as Record<string, unknown>;
  if (typeof body.burst_id !== "string" || !burstId.test(body.burst_id)) return null;
  if (!Number.isInteger(body.count) || (body.count as number) < 1 || (body.count as number) > BURST_MAX) return null;
  if (typeof body.trigger_event !== "string" || !/^[a-z_.]{1,40}$/.test(body.trigger_event)) return null;
  if (!Array.isArray(body.case_ids) || body.case_ids.length > BURST_MAX ||
      body.case_ids.some((id) => typeof id !== "string" || !caseId.test(id))) return null;
  if (typeof body.announced_in_s !== "number" || !Number.isFinite(body.announced_in_s) || body.announced_in_s < 0) return null;
  return {
    burst_id: body.burst_id, count: body.count as number, trigger_event: body.trigger_event,
    case_ids: body.case_ids as string[], announced_in_s: body.announced_in_s,
  };
}

export async function demoBurstProxy(request: Request, dependencies?: Dependencies): Promise<Response> {
  const { env, fetcher } = dependencies ?? { env: process.env, fetcher: fetch };
  if (!["GET", "POST"].includes(request.method)) return reply({ error: "Method not allowed." }, 405);
  if (!localRequest(request)) return reply({ error: "Use the local dashboard to send a burst." }, 403);
  if (env.DEMO_BURST_ENABLED !== "1" || !env.DEMO_CONTROL_TOKEN) {
    return reply({ enabled: false, max: BURST_MAX, reason: "Burst control is not configured." },
      request.method === "GET" ? 200 : 503);
  }
  let gateway: URL;
  try {
    gateway = new URL(env.GATEWAY_URL || "http://127.0.0.1:9000");
    if (!loopback.has(gateway.hostname) || !["http:", "https:"].includes(gateway.protocol) ||
        gateway.username || gateway.password || gateway.search || gateway.hash || gateway.pathname !== "/") throw new Error();
  } catch {
    return reply({ error: "The local demo gateway is not configured correctly." }, 503);
  }
  if (new URL(request.url).search) return reply({ error: "Invalid burst request." }, 400);
  if (request.method === "GET") return reply({ enabled: true, max: BURST_MAX, reason: "" });
  if (request.headers.get("content-type")?.split(";")[0].trim() !== "application/json") {
    return reply({ error: "JSON request required." }, 400);
  }
  const raw = await request.text();
  if (raw.length > 256) return reply({ error: "Request is too large." }, 413);
  let count: number;
  try {
    const payload = JSON.parse(raw);
    if (!payload || typeof payload !== "object" || Array.isArray(payload) || Object.keys(payload).length !== 1 ||
        !Number.isInteger(payload.count) || payload.count < 1 || payload.count > BURST_MAX) throw new Error();
    count = payload.count;
  } catch {
    return reply({ error: `A burst needs a count from 1 to ${BURST_MAX}.` }, 400);
  }
  try {
    const response = await fetcher(new URL("/demo/burst", gateway), {
      method: "POST",
      headers: { Authorization: `Bearer ${env.DEMO_CONTROL_TOKEN}`, "Content-Type": "application/json" },
      body: JSON.stringify({ count }), cache: "no-store", signal: AbortSignal.timeout(60_000), redirect: "error",
    });
    const text = await response.text();
    if (text.length > 64_000) throw new Error();
    const result = response.ok ? safeBurst(JSON.parse(text)) : null;
    if (result) return reply(result, 202);
    if ([400, 503].includes(response.status)) {
      return reply({ error: response.status === 503 ? "The gateway could not announce the burst; check the simulator world and Redis."
        : "The gateway rejected the burst request." }, response.status);
    }
    return reply({ error: "The demo gateway could not accept the burst." }, 502);
  } catch {
    return reply({ error: "The demo gateway is unavailable." }, 503);
  }
}
