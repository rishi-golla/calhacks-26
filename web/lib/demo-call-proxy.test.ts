import assert from "node:assert/strict";
import test from "node:test";
import { demoCallProxy } from "./demo-call-proxy";

const id = "020bfe50-d16c-4daf-9071-6394d407d9ef";
const task = "25d8cc64-474d-40c1-a945-e5bdf10f5b78";
const env = { DEMO_PHONE_ENABLED: "1", DEMO_CONTROL_TOKEN: "test-server-only-secret" };
const status = { enabled: true, ready: true, reason: "", destination_suffix: "0100",
  hours_override: true, case_id: "SYN-CARTESIA-TEAM-CALL" };
function request(method = "GET", body: unknown = { request_id: id }, headers: Record<string, string> = {}) {
  return new Request("http://127.0.0.1:3001/api/demo-call", { method,
    headers: { host: "127.0.0.1:3001", origin: "http://127.0.0.1:3001",
      "content-type": "application/json", ...headers },
    ...(method === "POST" ? { body: JSON.stringify(body) } : {}),
  });
}
const unavailable = async () => { throw new Error("must not contact gateway"); };

test("demo call proxy rejects remote and cross-origin controls before contacting the gateway", async () => {
  const cases: Record<string, string>[] = [{ host: "evil.example:3001" }, { origin: "https://evil.example" },
    { origin: "http://localhost:3001" }, { origin: "" }, { "sec-fetch-site": "cross-site" }];
  for (const headers of cases) {
    const response = await demoCallProxy(request("POST", undefined, headers), { env, fetcher: unavailable });
    assert.equal(response.status, 403);
  }
  assert.equal((await demoCallProxy(request("GET", undefined, { host: "10.0.0.2:3001" }),
    { env, fetcher: unavailable })).status, 403);
});

test("disabled controls and nonlocal gateways cannot forward a request or credentials", async () => {
  const disabled = await demoCallProxy(request(), { env: {}, fetcher: unavailable });
  assert.equal((await disabled.json()).enabled, false);
  assert.equal((await demoCallProxy(request("POST"), { env: {}, fetcher: unavailable })).status, 503);
  for (const url of ["https://evil.example", "http://localhost:9000/private", "http://secret@localhost:9000",
    "http://localhost:9000?token=test"]) {
    assert.equal((await demoCallProxy(request("POST"), { env: { ...env, GATEWAY_URL: url }, fetcher: unavailable })).status, 503);
  }
});

test("browser cannot select another destination, case, or hours override", async () => {
  for (const body of [null, [], {}, { request_id: "invalid" }, { request_id: id, phone: "+15555550100" },
    { request_id: id, case_id: "another-case" }, { request_id: id, hours_override: true }]) {
    assert.equal((await demoCallProxy(request("POST", body), { env, fetcher: unavailable })).status, 400);
  }
  assert.equal((await demoCallProxy(request("POST", { request_id: "a".repeat(2000) }),
    { env, fetcher: unavailable })).status, 413);
});

test("demo call POST forwards only the stable request ID and keeps secret fields server-side", async () => {
  let forwards = 0;
  const fetcher = (async (input, options) => {
    forwards++;
    assert.equal(String(input), "http://127.0.0.1:9000/demo/team-call");
    assert.equal(options?.method, "POST");
    assert.equal(new Headers(options?.headers).get("authorization"), `Bearer ${env.DEMO_CONTROL_TOKEN}`);
    assert.equal(options?.body, JSON.stringify({ request_id: id }));
    assert.equal(options?.redirect, "error");
    return Response.json({ ...status, token: "should-never-leak", destination: "+15555550100",
      request: { request_id: id, task_id: task, state: "queued", message: "Request accepted.", secret: "hidden" } },
    { status: 202 });
  }) as typeof fetch;
  const response = await demoCallProxy(request("POST"), { env, fetcher });
  assert.equal(response.status, 202);
  assert.equal(response.headers.get("cache-control"), "no-store");
  const data = await response.json();
  assert.equal(data.request.request_id, id);
  assert.equal(forwards, 1);
  assert.ok(!JSON.stringify(data).includes("secret"));
  assert.equal(data.destination, undefined);
});

test("status GET forwards a validated historical request ID without starting work", async () => {
  const req = new Request(`http://localhost:3001/api/demo-call?request_id=${id}`, { headers: { host: "localhost:3001" } });
  const fetcher = (async (input, options) => {
    assert.equal(String(input), `http://127.0.0.1:9000/demo/team-call?request_id=${id}`);
    assert.equal(options?.method, "GET");
    assert.equal(options?.body, undefined);
    return Response.json(status);
  }) as typeof fetch;
  assert.equal((await demoCallProxy(req, { env, fetcher })).status, 200);
  const invalid = new Request("http://localhost:3001/api/demo-call?request_id=bad", { headers: { host: "localhost:3001" } });
  assert.equal((await demoCallProxy(invalid, { env, fetcher: unavailable })).status, 400);
});

test("ambiguous gateway results remain failures and cooldown reasons stay visible", async () => {
  const timeout = await demoCallProxy(request("POST"), { env, fetcher: async () => { throw new Error("private-secret"); } });
  assert.equal(timeout.status, 503);
  const text = await timeout.text();
  assert.match(text, /status is unknown/);
  assert.ok(!text.includes("private-secret"));
  const malformed = await demoCallProxy(request("POST"), { env, fetcher: async () => Response.json({ queued: true }) });
  assert.equal(malformed.status, 502);
  const cooldown = await demoCallProxy(request("POST"), { env, fetcher: async () => Response.json({ ...status,
    ready: false, reason: "One demo call per hour. Try again later." }, { status: 429 }) });
  assert.equal(cooldown.status, 429);
  assert.match((await cooldown.json()).reason, /per hour/);
});
