import assert from "node:assert/strict";
import test from "node:test";
import { demoBurstProxy, safeBurst } from "./demo-burst-proxy";

const env = { DEMO_BURST_ENABLED: "1", DEMO_CONTROL_TOKEN: "test-server-only-secret" };
const started = { burst_id: `BURST-${"a".repeat(32)}`, count: 3, trigger_step: "carrier_invoice",
  trigger_event: "carrier_invoice.received", load_ids: ["LD-1", "LD-2", "LD-3"],
  case_ids: ["CASE-LD-1", "CASE-LD-2", "CASE-LD-3"], announced_in_s: 0.12 };
function request(method = "GET", body: unknown = { count: 3 }, headers: Record<string, string> = {}) {
  return new Request("http://127.0.0.1:3001/api/demo-burst", { method,
    headers: { host: "127.0.0.1:3001", origin: "http://127.0.0.1:3001", "content-type": "application/json", ...headers },
    ...(method === "POST" ? { body: JSON.stringify(body) } : {}),
  });
}
const unavailable = async () => { throw new Error("must not contact gateway"); };

test("burst proxy rejects remote and cross-origin controls before contacting the gateway", async () => {
  for (const headers of [{ host: "evil.example:3001" }, { origin: "https://evil.example" }, { "sec-fetch-site": "cross-site" }]) {
    assert.equal((await demoBurstProxy(request("POST", undefined, headers), { env, fetcher: unavailable })).status, 403);
  }
});

test("disabled control reports itself and never forwards", async () => {
  const disabled = await demoBurstProxy(request(), { env: {}, fetcher: unavailable });
  assert.equal((await disabled.json()).enabled, false);
  assert.equal((await demoBurstProxy(request("POST"), { env: {}, fetcher: unavailable })).status, 503);
  assert.equal((await demoBurstProxy(request("POST"), { env: { ...env, GATEWAY_URL: "https://evil.example" }, fetcher: unavailable })).status, 503);
});

test("browser can only choose a bounded count", async () => {
  for (const body of [null, [], {}, { count: 0 }, { count: 201 }, { count: 2.5 }, { count: "50" }, { count: 5, scenario: "x" }]) {
    assert.equal((await demoBurstProxy(request("POST", body), { env, fetcher: unavailable })).status, 400);
  }
});

test("a burst forwards the server-only token and returns a sanitized announcement", async () => {
  const seen: { url: string; init: RequestInit }[] = [];
  const fetcher = (async (url: URL | RequestInfo, init?: RequestInit) => {
    seen.push({ url: String(url), init: init ?? {} });
    return new Response(JSON.stringify({ ...started, secret: "never" }), { status: 202 });
  }) as typeof fetch;
  const response = await demoBurstProxy(request("POST"), { env, fetcher });
  assert.equal(response.status, 202);
  const body = await response.json();
  assert.deepEqual(body, { burst_id: started.burst_id, count: 3, trigger_event: "carrier_invoice.received",
    case_ids: started.case_ids, announced_in_s: 0.12 });
  assert.equal(seen[0].url, "http://127.0.0.1:9000/demo/burst");
  assert.equal((seen[0].init.headers as Record<string, string>).Authorization, "Bearer test-server-only-secret");
  assert.equal(seen[0].init.body, JSON.stringify({ count: 3 }));
});

test("malformed gateway answers are not shown to the browser", async () => {
  assert.equal(safeBurst({ ...started, burst_id: "BURST-nope" }), null);
  assert.equal(safeBurst({ ...started, case_ids: ["<script>"] }), null);
  assert.equal(safeBurst({ ...started, count: 500 }), null);
  const fetcher = (async () => new Response("{\"burst_id\":\"x\"}", { status: 202 })) as typeof fetch;
  assert.equal((await demoBurstProxy(request("POST"), { env, fetcher })).status, 502);
});
