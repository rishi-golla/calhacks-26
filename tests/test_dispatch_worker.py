"""Dispatch worker: `first_hop` outbox rows delivered over A2A beside the review kinds.

The real SpacetimeClient talks to `FakeModule` over an httpx mock transport, so these tests see the exact SQL and
reducer payloads the worker sends. `FakeModule` follows the module's rules for the reducers the worker calls:
write receipts (an identical replay is a no-op, changed contents are HTTP 530), claim/ack leases, and dispatch
transitions (`queued → accepted`; an accepted task is immutable; a re-observed accepted event becomes duplicate).
The agent double admits tasks through a RuntimeStore exactly as common.server does, so the task id is stable on
(agent, sender, case, idempotency key)."""
import json
import re
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from a2a.types import Task

from common import review_worker
from common.envelope import DelegationMeta, TaskEnvelope
from common.review_worker import ReviewWorker, backoff_seconds, nack_reason, poll_seconds
from common.runtime_store import RuntimeStore, stable_id, task_id_for
from common.source_repository import SourceUnavailable
from common.spacetime_client import SpacetimeClient, SpacetimeHTTPError

SKILL = "process_carrier_invoice"
OUTBOX_COLUMNS = ("id", "kind", "target_agent", "case_id", "task_id", "payload_json", "status", "attempts",
                  "created_at", "updated_at", "claimed_by", "lease_expires_at", "last_error")


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def iso(moment: datetime) -> str:
    """The module's UTC timestamp format, so created_at strings sort chronologically."""
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def stamp(offset_s: float = 0) -> str:
    return iso(datetime.now(UTC) + timedelta(seconds=offset_s))


class Refused(Exception):
    """A reducer that threw; the module answers HTTP 530 and rolls the transaction back."""


class FakeModule:
    def __init__(self) -> None:
        self.outbox: dict[str, dict] = {}
        self.dispatch: dict[str, dict] = {}
        self.sources: set[str] = set()
        self.receipts: dict[str, str] = {}
        self.outages: dict[str, list] = {}     # reducer -> outcomes: "connect", "timeout" (commits) or a status
        self.log: list[tuple] = []             # ("sql", text) and ("call", reducer, payload), in order

    def client(self) -> SpacetimeClient:
        return SpacetimeClient(actor="router", url="http://stdb.test", database="t", token="tok", disabled=False,
                               transport=httpx.MockTransport(self.handler))

    def first_hop(self, event_id: str, *, priority: str = "P2", created_at: str | None = None,
                  agent: str = "carrier_ap", case_id: str | None = None, queue_ms: int = 250) -> dict:
        """What enqueue_first_hop commits: the outbox row and a queued dispatch row."""
        case_id = case_id or f"CASE-{event_id}"
        env = TaskEnvelope(task_type=SKILL, case_id=case_id, priority=priority,
                           subject={"invoice_amount": 1200, "load_id": f"LOAD-{event_id}"},
                           evidence_refs=[f"event:{event_id}"])
        body = {"event_id": event_id, "case_id": case_id, "skill": SKILL, "agent": agent,
                "envelope": env.model_dump(mode="json"), "queue_ms": queue_ms, "policy_version": "w1-route-v1"}
        row = self.add(f"first_hop:{event_id}", "first_hop", agent, case_id, canonical(body), created_at)
        self.dispatch[event_id] = {"event_id": event_id, "case_id": case_id, "skill": SKILL, "agent": agent,
                                   "task_id": "", "status": "queued", "reason": "routed", "queue_ms": queue_ms,
                                   "policy_version": "w1-route-v1"}
        return row

    def add(self, row_id: str, kind: str, target_agent: str, case_id: str, payload_json: str,
            created_at: str | None = None, task_id: str = "", **fields) -> dict:
        created = created_at or stamp(-len(self.outbox) - 100)
        row = {"id": row_id, "kind": kind, "target_agent": target_agent, "case_id": case_id, "task_id": task_id,
               "payload_json": payload_json, "status": "pending", "attempts": 0, "created_at": created,
               "updated_at": created, "claimed_by": "", "lease_expires_at": "", "last_error": "", **fields}
        self.outbox[row_id] = row
        return row

    def review(self, row_id: str, *, attempts: int, last_error: str, age_s: float, **fields) -> dict:
        """A review_output row that has already failed `attempts` times, last updated `age_s` seconds ago."""
        return self.add(row_id, "review_output", "validator", f"CASE-{row_id}", canonical({"artifact": {}}),
                        task_id=f"task-{row_id}", attempts=attempts, last_error=last_error,
                        updated_at=stamp(-age_s), **fields)

    def claimed(self) -> list[str]:
        return [call["id"] for call in self.calls("claim_outbox")]

    def calls(self, reducer: str | None = None) -> list[dict]:
        return [entry[2] for entry in self.log if entry[0] == "call" and reducer in (None, entry[1])]

    def steps(self) -> list[tuple[str, str]]:
        """The worker's conversation: SQL by table, reducer calls by name, and A2A sends by agent."""
        return [("sql", entry[1].split(" FROM ")[1].split()[0]) if entry[0] == "sql" else entry[:2]
                for entry in self.log]

    def elapse(self, seconds: float) -> None:
        """Let time pass for every stored row (backoff, leases), as the worker and module both read the clock."""
        for row in self.outbox.values():
            for key in ("created_at", "updated_at", "lease_expires_at"):
                if row[key]:
                    row[key] = iso(datetime.fromisoformat(row[key]) - timedelta(seconds=seconds))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sql"):
            sql = request.content.decode()
            self.log.append(("sql", sql))
            return self.sql(sql)
        reducer = path.rsplit("/", 1)[1]
        payload = json.loads(json.loads(request.content)[0])
        self.log.append(("call", reducer, payload))
        outcome = self.outages.get(reducer, []).pop(0) if self.outages.get(reducer) else 200
        if outcome == "connect":
            raise httpx.ConnectError("refused", request=request)
        if outcome not in (200, "timeout"):
            return httpx.Response(outcome)
        body = canonical({"action": reducer, "payload": payload})
        receipt = self.receipts.get(payload["write_id"])
        if receipt is None:
            try:
                getattr(self, reducer)(payload)
            except Refused:
                return httpx.Response(530)
            self.receipts[payload["write_id"]] = body
        elif receipt != body:
            return httpx.Response(530)
        if outcome == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200)

    def sql(self, sql: str) -> httpx.Response:
        if "FROM routing_outbox" in sql:
            columns = OUTBOX_COLUMNS
            rows = [[row[c] for c in columns] for row in self.outbox.values() if row["status"] != "done"]
        elif "FROM source_record" in sql:
            columns = ("id",)
            match = re.search(r"WHERE id = '((?:[^']|'')*)'", sql)
            record_id = match.group(1).replace("''", "'") if match else None
            rows = [[record_id]] if record_id in self.sources else []
        else:
            columns, rows = (), []
        return httpx.Response(200, json=[{"schema": {"elements": [{"name": {"some": c}} for c in columns]},
                                          "rows": rows}])

    def claim_outbox(self, p: dict) -> None:
        row = self.outbox.get(p["id"])
        if row is None or row["status"] == "done":
            raise Refused
        if row["status"] == "processing" and datetime.fromisoformat(row["lease_expires_at"]) > datetime.now(UTC):
            raise Refused
        row.update(status="processing", attempts=row["attempts"] + 1, claimed_by="router",
                   lease_expires_at=stamp(p.get("lease_seconds", 60)), updated_at=stamp(), last_error="")

    def ack_outbox(self, p: dict) -> None:
        row = self.outbox[p["id"]]
        if row["status"] != "processing" or datetime.fromisoformat(row["lease_expires_at"]) <= datetime.now(UTC):
            raise Refused
        row.update(status="done" if p["success"] else "pending", claimed_by="", lease_expires_at="",
                   updated_at=stamp(), last_error="" if p["success"] else p.get("error", "delivery failed"))

    def record_dispatch(self, p: dict) -> None:
        if p["status"] == "accepted" and not p["task_id"]:
            raise Refused
        row = {key: p[key] for key in ("event_id", "case_id", "skill", "agent", "task_id", "status", "reason",
                                       "queue_ms", "policy_version")}
        previous = self.dispatch.get(p["event_id"])
        if previous:
            if any(previous[key] != row[key] for key in ("case_id", "agent", "skill")):
                raise Refused
            if previous["task_id"] and row["task_id"] and previous["task_id"] != row["task_id"]:
                raise Refused
            row["task_id"] = row["task_id"] or previous["task_id"]
            if previous["status"] != "queued":
                row["status"] = "duplicate"
            row.update(queue_ms=previous["queue_ms"], policy_version=previous["policy_version"])
        self.dispatch[p["event_id"]] = row


class Agent:
    """Admits like common.server.on_message_send; `failure` is raised instead of admitting while set."""

    def __init__(self, store: RuntimeStore, module: FakeModule | None = None) -> None:
        self.store = store
        self.module = module
        self.sent: list[tuple[str, TaskEnvelope, DelegationMeta]] = []
        self.fresh: list[bool] = []
        self.failure: Exception | None = None
        self.context_id: str | None = None

    async def __call__(self, agent: str, env: TaskEnvelope, meta: DelegationMeta) -> Task:
        self.sent.append((agent, env, meta))
        if self.module:
            self.module.log.append(("send", agent, meta.idempotency_key))
        if self.failure:
            raise self.failure
        row, fresh = await self.store.admit(agent, meta.from_agent, meta.idempotency_key, env.model_dump(),
                                            meta.model_dump(exclude_none=True), {})
        self.fresh.append(fresh)
        return Task(id=row["id"], context_id=self.context_id or env.case_id)


@pytest.fixture(autouse=True)
def fixture_mode(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    for name in ("DISPATCH_POLL_SECONDS", "A2A_DATABASE_URL", "A2A_TOKEN_ROUTER", "SPACETIME_TOKEN_ROUTER",
                 "SPACETIME_TOKEN", "AGENT_URL_CARRIER_AP", "AGENT_URL_VALIDATOR", "DISPATCH_MAX_REVIEW_CLAIMS",
                 "REVIEW_MAX_ATTEMPTS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def module() -> FakeModule:
    return FakeModule()


@pytest.fixture
async def stores(tmp_path):
    worker_store = RuntimeStore(f"sqlite+aiosqlite:///{tmp_path / 'worker.sqlite'}")
    agent_store = RuntimeStore(f"sqlite+aiosqlite:///{tmp_path / 'agent.sqlite'}")
    yield worker_store, agent_store
    await worker_store.close()
    await agent_store.close()


@pytest.fixture
def agent(module, stores) -> Agent:
    return Agent(stores[1], module)


@pytest.fixture
def worker(module, stores, agent) -> ReviewWorker:
    return ReviewWorker(db=module.client(), store=stores[0], submit=agent)


def stable_task(event_id: str, case_id: str, agent: str = "carrier_ap") -> str:
    return task_id_for(agent, "router", case_id, f"event:{event_id}")


async def test_first_hop_checks_source_sends_records_accepted_then_acks(module, worker, agent, stores):
    before = dict(module.first_hop("E1", priority="P1", queue_ms=391))
    module.sources.add("case:CASE-E1")
    assert await worker.run_once() == 1

    task_id = stable_task("E1", "CASE-E1")
    assert module.steps() == [("sql", "routing_outbox"), ("call", "claim_outbox"), ("sql", "source_record"),
                              ("send", "carrier_ap"), ("call", "record_dispatch"), ("call", "ack_outbox")]
    assert ("sql", "SELECT id FROM source_record WHERE id = 'case:CASE-E1'") in module.log

    sent_agent, env, meta = agent.sent[0]
    assert sent_agent == "carrier_ap"
    assert env == TaskEnvelope.model_validate(json.loads(before["payload_json"])["envelope"])
    assert meta.model_dump() == DelegationMeta(
        case_id="CASE-E1", from_agent="router", delegation_depth=0, visited=[], idempotency_key="event:E1",
        correlation_id="CASE-E1", causation_id="E1", priority="P1").model_dump()

    assert module.calls("record_dispatch") == [{
        "write_id": stable_id("first-hop-accepted", "first_hop:E1"), "event_id": "E1", "case_id": "CASE-E1",
        "agent": "carrier_ap", "skill": SKILL, "task_id": task_id, "status": "accepted",
        "reason": "delivered by dispatch worker", "queue_ms": 391, "policy_version": "w1-route-v1"}]
    assert module.calls("claim_outbox")[0]["lease_seconds"] == 60
    assert module.calls("ack_outbox")[0]["success"] is True
    after = module.outbox["first_hop:E1"]
    assert {key: after[key] for key in ("status", "attempts", "claimed_by", "lease_expires_at", "last_error")} == {
        "status": "done", "attempts": 1, "claimed_by": "", "lease_expires_at": "", "last_error": ""}
    assert after["payload_json"] == before["payload_json"]
    assert module.dispatch["E1"]["status"] == "accepted" and module.dispatch["E1"]["task_id"] == task_id
    stored = await stores[1].get(task_id)
    assert stored["agent"] == "carrier_ap" and stored["envelope"]["case_id"] == "CASE-E1"
    # Nothing left to do on the next poll.
    assert await worker.run_once() == 0 and len(agent.sent) == 1


async def test_missing_source_record_nacks_without_sending_and_retries_next_poll(module, worker, agent):
    module.first_hop("E2")
    assert await worker.run_once() == 0
    assert agent.sent == [] and module.calls("record_dispatch") == []
    assert module.outbox["first_hop:E2"]["status"] == "pending"
    reason = "SourceUnavailable (first hop case has no source record yet)"
    assert module.outbox["first_hop:E2"]["last_error"] == reason
    assert module.calls("ack_outbox") == [{"id": "first_hop:E2", "success": False, "error": reason,
                                           "write_id": stable_id("review-nack", "first_hop:E2", "1")}]
    assert module.dispatch["E2"]["status"] == "queued"

    module.sources.add("case:CASE-E2")
    assert await worker.run_once() == 0                          # backing off for 2 s after one failure
    assert len(module.calls("claim_outbox")) == 1
    module.elapse(2)
    assert await worker.run_once() == 1
    assert module.outbox["first_hop:E2"]["status"] == "done"
    assert module.outbox["first_hop:E2"]["attempts"] == 2
    assert module.dispatch["E2"]["status"] == "accepted"


@pytest.mark.parametrize("failure", [
    ValueError("destination card does not advertise the required identity/skill"),
    TimeoutError("A2A timeout"),
    RuntimeError("A2A destination returned no durable task acceptance"),
])
async def test_send_failure_nacks_with_last_error_and_keeps_the_loop_running(module, worker, agent, failure):
    module.first_hop("E3")
    module.first_hop("E4")
    module.sources.update({"case:CASE-E3", "case:CASE-E4"})
    agent.failure = failure
    assert await worker.run_once() == 0
    for row_id in ("first_hop:E3", "first_hop:E4"):
        assert module.outbox[row_id]["status"] == "pending"
        assert module.outbox[row_id]["last_error"] == type(failure).__name__
    assert module.calls("record_dispatch") == []
    assert {row["status"] for row in module.dispatch.values()} == {"queued"}

    agent.failure = None
    module.elapse(2)
    assert await worker.run_once() == 2
    assert {row["status"] for row in module.dispatch.values()} == {"accepted"}


async def test_agent_down_with_the_real_transport_nacks_then_recovers(module, stores, agent, monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("AGENT_URL_CARRIER_AP", f"http://127.0.0.1:{port}")
    module.first_hop("E5")
    module.sources.add("case:CASE-E5")
    worker = ReviewWorker(db=module.client(), store=stores[0])   # real common.a2a_transport.send_task
    assert await worker.run_once() == 0
    assert module.outbox["first_hop:E5"]["last_error"] == "ConnectError"
    assert module.outbox["first_hop:E5"]["status"] == "pending"

    worker.submit = agent                                        # the agent comes back
    module.elapse(2)
    assert await worker.run_once() == 1
    assert module.dispatch["E5"]["status"] == "accepted"
    assert module.dispatch["E5"]["task_id"] == stable_task("E5", "CASE-E5")


@pytest.mark.parametrize("crash", ["record_dispatch_response_lost", "ack_lost"])
async def test_crash_between_send_and_ack_is_recovered_by_the_agent_idempotency_key(
        module, stores, agent, crash):
    module.first_hop("E6")
    module.sources.add("case:CASE-E6")
    if crash == "record_dispatch_response_lost":
        # The accepted write commits but its response is lost, and so is the nack: the lease is left behind.
        module.outages = {"record_dispatch": ["timeout"], "ack_outbox": ["connect"]}
    else:
        # The process dies after recording acceptance: neither the ack nor the nack reach the module.
        module.outages = {"ack_outbox": ["connect", "connect"]}
    first = ReviewWorker(db=module.client(), store=stores[0], submit=agent)
    assert await first.run_once() == 0
    assert module.outbox["first_hop:E6"]["status"] == "processing"
    assert module.dispatch["E6"]["status"] == "accepted"

    restarted = ReviewWorker(db=module.client(), store=stores[0], submit=agent)
    assert await restarted.run_once() == 0                       # an unexpired lease is left alone
    module.elapse(61)                                            # the 60 s lease expires
    assert await restarted.run_once() == 1

    task_id = stable_task("E6", "CASE-E6")
    assert [meta.idempotency_key for _, _, meta in agent.sent] == ["event:E6", "event:E6"]
    assert agent.fresh == [True, False]                          # the agent returned the task it already had
    writes = module.calls("record_dispatch")
    assert len(writes) == 2 and writes[0] == writes[1]           # identical replay: a receipt no-op
    assert module.dispatch["E6"]["status"] == "accepted"          # not re-observed as a duplicate
    assert module.dispatch["E6"]["task_id"] == task_id
    assert module.outbox["first_hop:E6"]["status"] == "done"
    assert module.outbox["first_hop:E6"]["attempts"] == 2


async def test_first_hops_are_claimed_by_envelope_priority_before_review_rows(module, worker):
    module.first_hop("P3-old", priority="P3", created_at=stamp(-50))
    module.add("review-1", "review_output", "validator", "CASE-R", "{}", created_at=stamp(-40), task_id="t-1")
    module.first_hop("P0-new", priority="P0", created_at=stamp(-10))
    module.first_hop("P1", priority="P1", created_at=stamp(-30))
    module.first_hop("P0-old", priority="P0", created_at=stamp(-20))
    module.add("first_hop:broken", "first_hop", "carrier_ap", "CASE-X", "not json", created_at=stamp(-35))
    module.first_hop("P2", priority="P2", created_at=stamp(-45))
    module.add("other", "settlement_note", "cfo", "CASE-O", "{}", created_at=stamp(-60))
    await worker.run_once()
    assert [call["id"] for call in module.calls("claim_outbox")] == [
        "first_hop:P0-old", "first_hop:P0-new", "first_hop:P1", "first_hop:P2", "first_hop:broken",
        "first_hop:P3-old", "review-1"]
    assert module.outbox["first_hop:broken"]["last_error"] == "JSONDecodeError"
    assert module.outbox["other"]["status"] == "pending"          # unknown kinds are never claimed


async def test_review_kinds_are_still_delivered_beside_first_hops(module, worker, agent):
    module.first_hop("E7")
    module.sources.add("case:CASE-E7")
    module.add("review-2", "review_output", "validator", "CASE-R2", canonical({"artifact": {}}), task_id="t-2")
    assert await worker.run_once() == 2
    assert {row["status"] for row in module.outbox.values()} == {"done"}
    by_agent = {sent_agent: (env, meta) for sent_agent, env, meta in agent.sent}
    env, meta = by_agent["validator"]
    assert env.task_type == "review_output" and env.subject == {"target_task_id": "t-2"}
    assert meta.idempotency_key == "outbox:review-2"
    assert by_agent["carrier_ap"][1].idempotency_key == "event:E7"
    assert len(module.calls("record_dispatch")) == 1               # only the first hop writes dispatch telemetry


async def test_rejected_accepted_write_nacks_as_a_deterministic_rejection(module, worker, agent):
    # Like T4's simulated rows: the event already carries a different task, so `accepted` is refused (530).
    module.first_hop("E8")
    module.sources.add("case:CASE-E8")
    module.dispatch["E8"].update(status="duplicate", task_id="task-sim")
    assert await worker.run_once() == 0
    assert module.outbox["first_hop:E8"]["last_error"] == "SpacetimeHTTPError 530 rejected"
    assert module.outbox["first_hop:E8"]["status"] == "pending"
    assert module.dispatch["E8"]["task_id"] == "task-sim"


@pytest.mark.parametrize("server_status, reason", [(530, "SpacetimeHTTPError 530 rejected"),
                                                   (503, "SpacetimeHTTPError 503")])
def test_nack_reason_names_the_type_and_marks_reducer_rejections(server_status, reason):
    assert nack_reason(SpacetimeHTTPError(server_status, mutation=True)) == reason
    assert nack_reason(KeyError("event_id")) == "KeyError"


def test_nack_reason_names_why_the_source_is_unavailable_and_stays_deterministic():
    """Live Oct 25: followups failed for 15 minutes as a bare "SourceUnavailable", which hid that the worker
    had no task history for them. The code-authored reason is kept; the type is still the first word."""
    reason = nack_reason(SourceUnavailable("terminal source task history is unavailable"))
    assert reason == "SourceUnavailable (terminal source task history is unavailable)"
    assert review_worker.exhausted({"kind": "review_followup", "attempts": 10, "last_error": reason}, 10)
    assert nack_reason(SourceUnavailable()) == "SourceUnavailable"


async def test_two_workers_sharing_the_router_identity_never_both_deliver_a_row(module, stores, agent):
    """Both workers authenticate as the router, and the module keys receipts by identity and write_id. With a
    claim write_id of (row, attempt) alone, the second claim replayed the first's receipt and reported success,
    so both workers delivered the row to their own fleets (live Oct 25: one task ran on two fleets)."""
    module.first_hop("E20")
    module.sources.add("case:CASE-E20")
    row = dict(module.outbox["first_hop:E20"])   # what both workers read in the same poll
    second = ReviewWorker(db=module.client(), store=stores[0], submit=agent, worker_id="other")
    overlap = []

    async def submit_while_the_other_worker_polls(name, env, meta):
        # The first worker holds the lease and is mid-delivery when the second worker claims the same row.
        overlap.append(await second.claim_and_deliver(row))
        return await agent(name, env, meta)

    first = ReviewWorker(db=module.client(), store=stores[0], submit=submit_while_the_other_worker_polls,
                         worker_id="mac")
    assert await first.claim_and_deliver(row) is True
    assert overlap == [False]
    assert [entry[1] for entry in module.log if entry[0] == "send"] == ["carrier_ap"]
    claims = module.calls("claim_outbox")
    assert len(claims) == 2 and claims[0]["write_id"] != claims[1]["write_id"]
    assert module.outbox["first_hop:E20"]["status"] == "done" and module.outbox["first_hop:E20"]["attempts"] == 1


def test_each_worker_gets_its_own_claim_identity(module, stores, agent):
    one = ReviewWorker(db=module.client(), store=stores[0], submit=agent)
    two = ReviewWorker(db=module.client(), store=stores[0], submit=agent)
    assert one.worker_id and two.worker_id and one.worker_id != two.worker_id


@pytest.mark.parametrize("mismatch", ["task_id", "context_id"])
async def test_acceptance_that_breaks_the_stable_identity_is_not_recorded(module, worker, agent, mismatch):
    module.first_hop("E9")
    module.sources.add("case:CASE-E9")
    if mismatch == "context_id":
        agent.context_id = "OTHER-CASE"
    else:
        async def other_task(sent_agent, env, meta):
            return Task(id="not-the-stable-id", context_id=env.case_id)
        worker.submit = other_task
    assert await worker.run_once() == 0
    assert module.outbox["first_hop:E9"]["last_error"] == "ValueError"
    assert module.calls("record_dispatch") == []
    assert module.dispatch["E9"]["status"] == "queued"


async def test_route_that_disagrees_with_its_row_is_not_sent(module, worker, agent):
    row = module.first_hop("E10")
    module.sources.add("case:CASE-E10")
    row["target_agent"] = "cfo"
    assert await worker.run_once() == 0
    assert agent.sent == []
    assert module.outbox["first_hop:E10"]["last_error"] == "ValueError"


async def test_contract_round_trip_with_the_router_enqueue(module, worker, agent, tmp_path, monkeypatch):
    """The worker delivers exactly what the router's direct A2A first hop would have sent."""
    from harness.ingest import dispatch
    from harness.ingest.dispatch import Dispatcher
    from harness.ingest.telemetry import POLICY_VERSION, QUEUE_MS

    enqueued = []

    class Router:
        disabled = False

        def __init__(self, **kwargs):
            pass

        async def mutate(self, name, payload):
            enqueued.append((name, payload))

    direct = AsyncMock(return_value=Task(id="direct", context_id="CASE-RT"))
    monkeypatch.setattr(dispatch, "SpacetimeClient", Router)
    monkeypatch.setattr(dispatch, "send_task", direct)
    env = TaskEnvelope(task_type=SKILL, case_id="CASE-RT", priority="P0", subject={"invoice_amount": 99.5},
                       evidence_refs=["event:E-RT"])
    token = QUEUE_MS.set(777)
    try:
        await Dispatcher(tmp_path / "spacetime.sqlite", transport="spacetime").send("E-RT", SKILL, env, "routed")
        await Dispatcher(tmp_path / "a2a.sqlite", transport="a2a").send("E-RT", SKILL, env, "routed")
    finally:
        QUEUE_MS.reset(token)
    [(reducer, payload)] = enqueued
    assert reducer == "enqueue_first_hop"
    # What enqueue_first_hop stores: the payload without its write id and reason, canonicalised.
    body = {key: payload[key] for key in ("event_id", "case_id", "skill", "agent", "envelope", "queue_ms",
                                          "policy_version")}
    module.add("first_hop:E-RT", "first_hop", payload["agent"], payload["case_id"], canonical(body))
    module.dispatch["E-RT"] = {**{key: payload[key] for key in ("event_id", "case_id", "skill", "agent",
                                                                "queue_ms", "policy_version")},
                               "task_id": "", "status": "queued", "reason": "routed"}
    module.sources.add("case:CASE-RT")
    assert await worker.run_once() == 1

    [(sent_agent, sent_env, sent_meta)] = agent.sent
    direct_agent, direct_env, direct_meta = direct.await_args.args
    assert (sent_agent, sent_env, sent_meta) == (direct_agent, direct_env, direct_meta)
    [accepted] = module.calls("record_dispatch")
    assert accepted["queue_ms"] == 777 and accepted["policy_version"] == POLICY_VERSION
    assert accepted["task_id"] == stable_task("E-RT", "CASE-RT")


def pass_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith("outbox pass:")]


async def test_exhausted_review_backlog_does_not_delay_a_fresh_first_hop(module, worker, agent, caplog):
    # Maincloud's shape: review rows whose originals live elsewhere fail the same way on every pass.
    kinds = ("review_followup", "review_output", "review_flag")
    errors = sorted(review_worker.DETERMINISTIC_ERRORS)
    for index in range(100):
        module.add(f"stale-{index}", kinds[index % 3], "cfo", f"CASE-S{index}", "{}", created_at=stamp(-7200),
                   task_id=f"task-{index}", attempts=10 + index % 20, last_error=errors[index % len(errors)],
                   updated_at=stamp(-3600))
    module.first_hop("FRESH", priority="P2", created_at=stamp())   # enqueued this instant
    module.sources.add("case:CASE-FRESH")
    caplog.set_level("INFO", logger="common.review_worker")
    assert await worker.run_once() == 1
    assert module.claimed() == ["first_hop:FRESH"]
    assert [call["id"] for call in module.calls("ack_outbox")] == ["first_hop:FRESH"]
    assert module.outbox["first_hop:FRESH"]["status"] == "done"
    assert module.dispatch["FRESH"]["status"] == "accepted"
    assert {row["status"] for key, row in module.outbox.items() if key.startswith("stale-")} == {"pending"}
    summary = ("outbox pass: first_hop due=1 delivered=1 backoff=0 leased=0; review claimed=0 delivered=0 "
               "backoff=0 exhausted=100 deferred=0 leased=0")
    assert pass_lines(caplog) == [summary]


async def test_transient_review_failures_back_off_and_at_most_20_are_claimed_per_pass(module, worker, agent,
                                                                                       caplog):
    due = set()
    for index in range(50):
        attempts = 1 + index % 5                                  # backoff 2, 4, 8, 16 or 32 s
        wait = 2 ** attempts
        is_due = index % 2 == 1
        module.review(f"r{index:02}", attempts=attempts, last_error="ConnectError",
                      age_s=wait + 1 if is_due else wait / 2, created_at=stamp(-1000 + index))
        if is_due:
            due.add(f"r{index:02}")
    agent.failure = httpx.ConnectError("validator down")
    caplog.set_level("INFO", logger="common.review_worker")
    assert await worker.run_once() == 0
    first = module.claimed()
    assert len(first) == 20 and set(first) <= due and first == sorted(first)   # oldest due rows first
    assert pass_lines(caplog)[-1].endswith("review claimed=20 delivered=0 backoff=25 exhausted=0 deferred=5 "
                                           "leased=0")
    assert await worker.run_once() == 0                          # the 20 just nacked are backing off now
    second = module.claimed()[20:]
    assert set(second) == due - set(first)
    assert pass_lines(caplog)[-1].endswith("review claimed=5 delivered=0 backoff=45 exhausted=0 deferred=0 "
                                           "leased=0")


async def test_a_failing_first_hop_backs_off_at_most_15_seconds(module, worker, agent):
    module.first_hop("E11")
    module.sources.add("case:CASE-E11")
    # A review row with the first hop's final history (7 failures) waits 128 s; the loop below spans 92 s.
    module.review("slow", attempts=7, last_error="ConnectError", age_s=15.5)
    agent.failure = TimeoutError("agent busy")
    assert await worker.run_once() == 0
    for wait in (2, 4, 8, 15, 15, 15):                            # after 1..6 failures: 2, 4, 8, then capped
        claims = len(module.claimed())
        module.elapse(wait - 0.5)
        await worker.run_once()
        assert len(module.claimed()) == claims, f"retried before its {wait} s backoff"
        module.elapse(1)
        await worker.run_once()
        assert module.claimed()[claims:] == ["first_hop:E11"], f"not retried after {wait} s"
    assert module.outbox["first_hop:E11"]["attempts"] == 7
    assert "slow" not in module.claimed()
    agent.failure = None
    module.elapse(15)
    assert await worker.run_once() == 1
    assert module.dispatch["E11"]["status"] == "accepted"


@pytest.mark.parametrize("kind, attempts, seconds", [
    ("first_hop", 0, 0), ("first_hop", 1, 2), ("first_hop", 3, 8), ("first_hop", 4, 15), ("first_hop", 40, 15),
    ("review_followup", 0, 0), ("review_followup", 4, 16), ("review_flag", 8, 256), ("review_output", 9, 300),
    ("review_output", 29, 300), ("review_output", 10 ** 9, 300),
])
def test_backoff_doubles_from_two_seconds_with_caps(kind, attempts, seconds):
    assert backoff_seconds({"kind": kind, "attempts": attempts}) == seconds


async def test_only_review_rows_with_deterministic_errors_are_given_up_on(module, worker, agent):
    old = 400                                                     # past every backoff
    module.review("at-9", attempts=9, last_error="SourceUnavailable", age_s=old)
    module.review("at-10", attempts=10, last_error="SourceUnavailable", age_s=old)
    module.review("key-error", attempts=12, last_error="KeyError", age_s=old)
    module.review("connect", attempts=25, last_error="ConnectError", age_s=old)
    module.review("server", attempts=25, last_error="SpacetimeHTTPError 503", age_s=old)
    module.review("timeout-recent", attempts=25, last_error="TimeoutError", age_s=200)
    hop = module.first_hop("E12")
    hop.update(attempts=50, last_error="SourceUnavailable", updated_at=stamp(-16))
    module.sources.add("case:CASE-E12")
    assert await worker.run_once() == 4
    assert module.claimed()[0] == "first_hop:E12"
    assert set(module.claimed()[1:]) == {"at-9", "connect", "server"}
    assert module.outbox["at-10"]["status"] == "pending"
    assert module.outbox["at-10"]["last_error"] == "SourceUnavailable"   # left visible for an operator


async def test_review_limits_come_from_the_environment(module, stores, agent, monkeypatch):
    monkeypatch.setenv("REVIEW_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("DISPATCH_MAX_REVIEW_CLAIMS", "1")
    module.review("given-up", attempts=3, last_error="ValueError", age_s=400)
    module.review("first", attempts=2, last_error="ValueError", age_s=400, created_at=stamp(-900))
    module.review("second", attempts=2, last_error="ValueError", age_s=400, created_at=stamp(-800))
    worker = ReviewWorker(db=module.client(), store=stores[0], submit=agent)
    assert await worker.run_once() == 1
    assert module.claimed() == ["first"]

    monkeypatch.setenv("DISPATCH_MAX_REVIEW_CLAIMS", "0")         # a first-hop-only worker
    module.first_hop("E13")
    module.sources.add("case:CASE-E13")
    worker = ReviewWorker(db=module.client(), store=stores[0], submit=agent)
    assert await worker.run_once() == 1
    assert module.claimed() == ["first", "first_hop:E13"]


@pytest.mark.parametrize("name, value", [("DISPATCH_MAX_REVIEW_CLAIMS", "-1"), ("DISPATCH_MAX_REVIEW_CLAIMS", "x"),
                                         ("REVIEW_MAX_ATTEMPTS", "0"), ("REVIEW_MAX_ATTEMPTS", "2.5")])
async def test_invalid_review_limits_fail_before_polling(module, stores, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        ReviewWorker(db=module.client(), store=stores[0])


async def test_each_pass_logs_one_summary_line_and_idle_passes_log_at_debug(module, worker, caplog):
    module.review("waiting", attempts=3, last_error="ConnectError", age_s=1)
    caplog.set_level("DEBUG", logger="common.review_worker")
    await worker.run_once()
    await worker.run_once()
    records = [record for record in caplog.records if record.getMessage().startswith("outbox pass:")]
    assert len(records) == 2 and {record.levelname for record in records} == {"DEBUG"}
    assert "review claimed=0 delivered=0 backoff=1" in records[0].getMessage()


def test_worker_does_not_import_the_harness():
    probe = "import sys, common.review_worker; sys.exit(any(m.split('.')[0] == 'harness' for m in sys.modules))"
    assert subprocess.run([sys.executable, "-c", probe], check=False).returncode == 0


def test_poll_interval_defaults_to_one_second(monkeypatch):
    monkeypatch.delenv("DISPATCH_POLL_SECONDS", raising=False)
    assert poll_seconds() == 1
    monkeypatch.setenv("DISPATCH_POLL_SECONDS", "0.25")
    assert poll_seconds() == 0.25


@pytest.mark.parametrize("value", ["0", "-1", "soon", "nan", "inf", ""])
async def test_invalid_poll_interval_fails_before_polling(monkeypatch, value):
    monkeypatch.setenv("DISPATCH_POLL_SECONDS", value)
    with pytest.raises(ValueError, match="DISPATCH_POLL_SECONDS"):
        await review_worker.main()


@pytest.mark.parametrize("missing", ["A2A_TOKEN_ROUTER", "SPACETIME_TOKEN_ROUTER"])
async def test_live_mode_requires_the_router_credentials(monkeypatch, missing):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    monkeypatch.setenv("A2A_TOKEN_ROUTER", "a2a-token")
    monkeypatch.setenv("SPACETIME_TOKEN_ROUTER", "stdb-token")
    monkeypatch.delenv(missing)

    def no_worker():
        raise AssertionError("the credential check must run before the worker connects to anything")

    monkeypatch.setattr(review_worker, "ReviewWorker", no_worker)
    with pytest.raises(ValueError, match=missing):
        await review_worker.main()


async def test_disabled_spacetime_still_fails_fast(monkeypatch, tmp_path):
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    monkeypatch.setenv("A2A_RUNTIME_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="SPACETIME_DISABLED=0"):
        await review_worker.main()


async def test_main_polls_at_the_configured_interval(monkeypatch, tmp_path):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("SPACETIME_DISABLED", "0")
    monkeypatch.setenv("A2A_TOKEN_ROUTER", "a2a-token")
    monkeypatch.setenv("SPACETIME_TOKEN_ROUTER", "stdb-token")
    monkeypatch.setenv("A2A_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'runtime.sqlite'}")
    monkeypatch.setenv("DISPATCH_POLL_SECONDS", "0.5")
    monkeypatch.setattr(ReviewWorker, "run_once", AsyncMock(side_effect=[1, RuntimeError("outage")]))
    sleeps = []

    class Stop(Exception):
        pass

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise Stop

    monkeypatch.setattr(review_worker.asyncio, "sleep", sleep)
    with pytest.raises(Stop):
        await review_worker.main()
    assert sleeps == [0.5, 0.5]                                   # an outage is logged, not fatal
