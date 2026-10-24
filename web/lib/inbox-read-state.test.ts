import assert from "node:assert/strict";
import test from "node:test";
import { inboxActivityFingerprint, type AgentMessage } from "./agent-inbox";
import { markMessagesRead } from "./inbox-read-state";

function message(
  id: number,
  agentId = "carrier_ap",
  recipientId?: string,
): AgentMessage {
  return {
    id: `message-${id}`,
    at: id + 1,
    agentId,
    recipientId,
    caseId: "case",
    kind: "status",
    title: "Task update",
    body: `Private business text ${id}`,
    evidenceRefs: [],
    policyIds: [],
  };
}
const ids = (messages: AgentMessage[]) =>
  messages.map(inboxActivityFingerprint);

test("more than 2000 messages settle into one canonical window without state rotation", () => {
  const messages = Array.from({ length: 2101 }, (_, index) => message(index));
  const first = markMessagesRead({}, messages, messages);
  assert.equal(first.carrier_ap.length, 2000);
  assert.deepEqual(first.carrier_ap, ids(messages.slice(-2000)));
  assert.equal(markMessagesRead(first, messages, messages), first);
  assert.equal(markMessagesRead(first, messages.slice(0, 50), messages), first);
  assert.ok(!JSON.stringify(first).includes("Private business text"));
});

test("marking one agent preserves other inboxes and a handoff belongs to both participants", () => {
  const sent = message(1, "carrier_ap", "risk");
  const risk = message(2, "risk");
  const previous = { risk: ids([risk]) };
  const first = markMessagesRead(
    previous,
    [sent, risk],
    [sent, risk],
    "carrier_ap",
  );
  assert.deepEqual(first.carrier_ap, ids([sent]));
  assert.equal(first.risk, previous.risk);
  const all = markMessagesRead(first, [sent], [sent, risk]);
  assert.deepEqual(all.risk, ids([sent, risk]));
  assert.equal(
    markMessagesRead(all, [sent], [sent, risk], "unknown-agent"),
    all,
  );
});

test("search and case subsets do not mark nonvisible records read", () => {
  const messages = [message(1), message(2), message(3)];
  const first = markMessagesRead({}, [messages[1]], messages, "carrier_ap");
  assert.deepEqual(first.carrier_ap, ids([messages[1]]));
  assert.equal(markMessagesRead(first, [], messages, "carrier_ap"), first);
  const second = markMessagesRead(first, [messages[0]], messages, "carrier_ap");
  assert.deepEqual(second.carrier_ap, ids(messages.slice(0, 2)));
});

test("removed and updated records lose stale markers while canonical order is retained", () => {
  const old = message(1);
  const current = { ...old, body: "A revised public result" };
  const later = message(2);
  const removed = message(0);
  const previous = { carrier_ap: ids([later, old, removed]) };
  const first = markMessagesRead(previous, [], [later, current], "carrier_ap");
  assert.deepEqual(first.carrier_ap, ids([later]));
  const second = markMessagesRead(
    first,
    [current],
    [later, current],
    "carrier_ap",
  );
  assert.deepEqual(second.carrier_ap, ids([current, later]));
  assert.equal(
    markMessagesRead(second, [current], [current, later], "carrier_ap"),
    second,
  );
});

test("duplicates do not consume window slots and zero limit clears only selected markers", () => {
  const messages = [message(1), message(2)];
  const duplicate = markMessagesRead(
    {},
    messages,
    [messages[0], messages[1], messages[1]],
    "carrier_ap",
    2,
  );
  assert.deepEqual(duplicate.carrier_ap, ids(messages));
  const previous = { ...duplicate, risk: ["retained"] };
  const empty = markMessagesRead(previous, messages, messages, "carrier_ap", 0);
  assert.deepEqual(empty, { carrier_ap: [], risk: ["retained"] });
  assert.equal(
    markMessagesRead(empty, messages, messages, "carrier_ap", 0),
    empty,
  );
});
