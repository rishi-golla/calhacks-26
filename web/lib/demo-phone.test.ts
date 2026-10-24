import assert from "node:assert/strict";
import test from "node:test";
import {
  DEMO_PHONE_STORAGE_KEY,
  DemoPhoneClient,
  demoPhoneAction,
  isDemoCallTerminal,
  type DemoCallStatus,
} from "./demo-phone";

const FIRST = "5ee8128f-3479-45a8-9bdd-dbe656651b08";
const SECOND = "c6c15443-1b66-4c53-92e8-e466244e089d";
const ready: DemoCallStatus = {
  enabled: true, ready: true, destination_suffix: "4226",
  hours_override: true, case_id: "SYN-CARTESIA-TEAM-CALL",
};
const accepted = (state = "queued", id = FIRST): DemoCallStatus => ({
  ...ready, request: { request_id: id, task_id: "task", state },
});
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "content-type": "application/json" },
});
function storage() {
  const data = new Map<string, string>();
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { data.set(key, value); },
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

test("readiness and reload only read status, and the browser sees only a phone suffix", async () => {
  const requests: { url: string; method?: string }[] = [];
  const client = new DemoPhoneClient({
    storage, uuid: () => FIRST,
    fetcher: async (url, options) => {
      requests.push({ url, method: options?.method });
      return response({ ...ready, phone: "+12025550123" });
    },
  });
  await client.refresh();
  assert.deepEqual(requests, [{ url: "/api/demo-call", method: undefined }]);
  assert.equal(client.snapshot().status?.destination_suffix, "4226");
  assert.equal(JSON.stringify(client.snapshot()).includes("+12025550123"), false);
  assert.equal(demoPhoneAction(client.snapshot()).disabled, false);
});

test("one deliberate action persists the UUID before POST and ignores a double click", async () => {
  const saved = storage();
  const pending = deferred<Response>();
  const posts: string[] = [];
  const client = new DemoPhoneClient({
    storage: () => saved, uuid: () => FIRST,
    fetcher: async (_url, options) => {
      if (options?.method !== "POST") return response(ready);
      assert.deepEqual(JSON.parse(saved.getItem(DEMO_PHONE_STORAGE_KEY)!), { request_id: FIRST });
      posts.push(String(options.body));
      return pending.promise;
    },
  });
  await client.refresh();
  const call = client.start();
  await client.start();
  assert.deepEqual(posts, [JSON.stringify({ request_id: FIRST })]);
  assert.equal(client.snapshot().submitting, true);
  pending.resolve(response(accepted()));
  await call;
  assert.equal(demoPhoneAction(client.snapshot()).active, true);
  assert.equal(demoPhoneAction(client.snapshot()).disabled, true);
});

test("ambiguous POST failure survives reload and retries the same ID without automatically posting", async () => {
  const saved = storage();
  const posts: string[] = [];
  const first = new DemoPhoneClient({
    storage: () => saved, uuid: () => FIRST,
    fetcher: async (_url, options) => {
      if (options?.method !== "POST") return response(ready);
      posts.push(String(options.body));
      throw new Error("connection closed after send");
    },
  });
  await first.refresh();
  await first.start();
  assert.equal(demoPhoneAction(first.snapshot()).retry, true);
  assert.match(first.snapshot().error, /same request/);
  const gets: string[] = [];
  const reloaded = new DemoPhoneClient({
    storage: () => saved, uuid: () => { throw new Error("must not generate another ID"); },
    fetcher: async (url, options) => {
      if (options?.method !== "POST") { gets.push(url); return response(ready); }
      posts.push(String(options.body));
      return response(accepted());
    },
  });
  await reloaded.refresh();
  assert.deepEqual(gets, [`/api/demo-call?request_id=${FIRST}`]);
  assert.equal(posts.length, 1);
  await reloaded.start();
  assert.deepEqual(posts, [JSON.stringify({ request_id: FIRST }), JSON.stringify({ request_id: FIRST })]);
});

test("known server request is adopted and completion immediately allows the next deliberate demo", async () => {
  const saved = storage();
  let state = "working";
  let generated = 0;
  const posts: string[] = [];
  const client = new DemoPhoneClient({
    storage: () => saved, uuid: () => { generated += 1; return SECOND; },
    fetcher: async (_url, options) => {
      if (options?.method !== "POST") return response(accepted(state));
      posts.push(String(options.body));
      return response(accepted("queued", SECOND));
    },
  });
  await client.refresh();
  assert.equal(client.snapshot().requestId, FIRST);
  await client.start();
  assert.equal(generated, 0);
  state = "completed";
  await client.refresh();
  assert.equal(demoPhoneAction(client.snapshot()).label, "Call my phone again");
  assert.equal(demoPhoneAction(client.snapshot()).disabled, false);
  assert.equal(generated, 0);
  await client.start();
  assert.deepEqual(posts, [JSON.stringify({ request_id: SECOND })]);
});

test("structured readiness blockers preserve the reason and request without retrying", async () => {
  const saved = storage();
  let posts = 0;
  const client = new DemoPhoneClient({
    storage: () => saved, uuid: () => FIRST,
    fetcher: async (_url, options) => {
      if (options?.method !== "POST") return response(ready);
      posts += 1;
      return response({ ...ready, ready: false, reason: "Calling is temporarily unavailable." }, 503);
    },
  });
  await client.refresh();
  await client.start();
  assert.equal(client.snapshot().error, "Calling is temporarily unavailable.");
  assert.equal(demoPhoneAction(client.snapshot()).disabled, true);
  assert.equal(client.snapshot().requestId, FIRST);
  await client.start();
  assert.equal(posts, 1);
});

test("late GET cannot overwrite a newer POST result", async () => {
  const stale = deferred<Response>();
  let reads = 0;
  const client = new DemoPhoneClient({
    storage, uuid: () => FIRST,
    fetcher: async (_url, options) => {
      if (options?.method === "POST") return response(accepted());
      reads += 1;
      return reads === 1 ? response(ready) : stale.promise;
    },
  });
  await client.refresh();
  const read = client.refresh();
  await client.start();
  stale.resolve(response(ready));
  await read;
  assert.equal(client.snapshot().status?.request?.request_id, FIRST);
  assert.equal(demoPhoneAction(client.snapshot()).active, true);
});

test("uncertain and waiting states are nonterminal and never create a new ID", async () => {
  for (const state of ["uncertain", "queued", "working", "waiting"]) {
    const client = new DemoPhoneClient({
      storage, uuid: () => { throw new Error("must not create a new call"); },
      fetcher: async () => response(accepted(state)),
    });
    await client.refresh();
    assert.equal(isDemoCallTerminal(client.snapshot().status?.request), false);
    assert.equal(demoPhoneAction(client.snapshot()).disabled, true);
    await client.start();
  }
});

test("storage failure prevents POST so a reload cannot lose its idempotency key", async () => {
  let posts = 0;
  const client = new DemoPhoneClient({
    storage: () => ({ getItem: () => null, setItem: () => { throw new Error("storage blocked"); } }),
    uuid: () => FIRST,
    fetcher: async (_url, options) => { if (options?.method === "POST") posts += 1; return response(ready); },
  });
  await client.refresh();
  await client.start();
  assert.equal(posts, 0);
  assert.equal(demoPhoneAction(client.snapshot()).disabled, true);
  assert.match(client.snapshot().error, /save the call request/);
});

test("server-approved admission retry reuses its UUID even while readiness is blocked by that request", async () => {
  const saved = storage();
  const posts: string[] = [];
  const client = new DemoPhoneClient({
    storage: () => saved, uuid: () => { throw new Error("must reuse admitted request ID"); },
    fetcher: async (_url, options) => {
      if (options?.method !== "POST") return response({
        ...ready, ready: false, reason: "A call request is pending.",
        request: { request_id: FIRST, task_id: "task", state: "uncertain", retryable: true },
      });
      posts.push(String(options.body));
      return response(accepted("working"));
    },
  });
  await client.refresh();
  assert.equal(demoPhoneAction(client.snapshot()).disabled, false);
  assert.equal(demoPhoneAction(client.snapshot()).label, "Retry call request");
  await client.start();
  assert.deepEqual(posts, [JSON.stringify({ request_id: FIRST })]);
  assert.equal(demoPhoneAction(client.snapshot()).disabled, true);
});

test("proxy errors stay visible while an ambiguous request retains its ID", async () => {
  const saved = storage();
  const client = new DemoPhoneClient({
    storage: () => saved, uuid: () => FIRST,
    fetcher: async (_url, options) => options?.method === "POST"
      ? response({ error: "Gateway unavailable; retry the same request." }, 503)
      : response(ready),
  });
  await client.refresh();
  await client.start();
  assert.equal(client.snapshot().error, "Gateway unavailable; retry the same request.");
  assert.equal(client.snapshot().requestId, FIRST);
  assert.equal(demoPhoneAction(client.snapshot()).retry, true);
});

test("an unknown saved UUID stays pending while another request blocks readiness and is reused afterward", async () => {
  const saved = storage();
  saved.setItem(DEMO_PHONE_STORAGE_KEY, JSON.stringify({ request_id: FIRST }));
  let blocked = true;
  const gets: string[] = [];
  const posts: string[] = [];
  const client = new DemoPhoneClient({
    storage: () => saved, uuid: () => { throw new Error("must retain unknown request ID"); },
    fetcher: async (url, options) => {
      if (options?.method === "POST") {
        posts.push(String(options.body));
        return response(accepted("working"));
      }
      gets.push(url);
      return response({ ...ready, ready: !blocked,
        reason: blocked ? "A team call request is already pending." : "" });
    },
  });
  await client.refresh();
  assert.equal(client.snapshot().requestId, FIRST);
  assert.equal(demoPhoneAction(client.snapshot()).disabled, true);
  await client.start();
  assert.equal(posts.length, 0);
  blocked = false;
  await client.refresh();
  assert.equal(demoPhoneAction(client.snapshot()).label, "Retry call request");
  assert.equal(demoPhoneAction(client.snapshot()).disabled, false);
  assert.deepEqual(gets, Array(2).fill(`/api/demo-call?request_id=${FIRST}`));
  assert.equal(posts.length, 0);
  await client.start();
  assert.deepEqual(posts, [JSON.stringify({ request_id: FIRST })]);
});
