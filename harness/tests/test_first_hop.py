"""First hop through the SpacetimeDB outbox (FIRST_HOP_TRANSPORT=spacetime) and the unchanged A2A default.

The real SpacetimeClient runs over an httpx mock transport, so these tests see the exact bytes the reducer
would receive: the write-receipt rule rejects a reused write id whose contents changed."""
import json
import re
import sqlite3
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from a2a.helpers import new_task
from a2a.types import TaskState

from common.envelope import TaskEnvelope
from common.spacetime_client import SpacetimeClient, SpacetimeHTTPError
from harness.ingest import consumer, dispatch, pipeline
from harness.ingest.dispatch import Dispatcher, DispatchError
from harness.ingest.pipeline import process
from harness.ingest.telemetry import POLICY_VERSION, QUEUE_MS, queue_ms
from harness.ingest.vault import Vault
from harness.pool import Unavailable

SKILL = "process_carrier_invoice"


class FakeSpacetime:
    """Answers reducer calls with queued outcomes (HTTP status or a transport failure); 200 once they run out.
    Like the module's write receipts, a committed write id replays as a no-op and rejects changed contents (530);
    a timeout commits before the response is lost. SQL reads see an outbox row `first_hop:<event_id>` once its
    enqueue committed (that write id is the row id)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []   # reducer calls only
        self.queries: list[str] = []
        self.outcomes: list = []
        self.receipts: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sql"):
            query = request.content.decode()
            self.queries.append(query)
            rows = [[key] for key in re.findall(r"'([^']*)'", query) if key in self.receipts]
            return httpx.Response(200, json=[{"schema": {"elements": [{"name": {"some": "id"}}]}, "rows": rows}])
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else 200
        if outcome == "connect":
            raise httpx.ConnectError("refused", request=request)
        if outcome in (200, "timeout"):
            payload = json.loads(json.loads(request.content)[0])
            if self.receipts.setdefault(payload["write_id"], payload) != payload:
                return httpx.Response(530)
        if outcome == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(outcome)

    def client(self, **kw) -> SpacetimeClient:
        return SpacetimeClient(**kw, url="http://stdb.test", database="t", token="tok", disabled=False,
                               transport=httpx.MockTransport(self.handler))

    @property
    def bodies(self) -> list[bytes]:
        return [r.content for r in self.requests]

    def payloads(self) -> list[dict]:
        return [json.loads(json.loads(body)[0]) for body in self.bodies]


@pytest.fixture
def stdb(monkeypatch):
    fake = FakeSpacetime()
    monkeypatch.setattr(dispatch, "SpacetimeClient", fake.client)
    monkeypatch.setattr(dispatch, "send_task", AsyncMock(side_effect=AssertionError("no A2A in spacetime mode")))
    return fake


@pytest.fixture
def queue_time():
    token = QUEUE_MS.set(250)
    yield 250
    QUEUE_MS.reset(token)


def envelope(**subject) -> TaskEnvelope:
    return TaskEnvelope(task_type=SKILL, case_id="CASE-900", subject={"invoice_amount": 987.32, **subject},
                        evidence_refs=["event:EVT-1"], priority="P0")


def event(event_id: str = "EVT-1") -> dict:
    return {"event_id": event_id, "type": "carrier_invoice.received", "priority": "P0",
            "payload": {"case_id": "CASE-900", "invoice_amount": 987.32,
                        "remit_email": "billing@example-carrier.com"}}


def stored_enqueue(path, event_id: str) -> str | None:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT enqueue FROM dispatch WHERE event_id=?", (event_id,)).fetchone()[0]


# Transport selection

def test_default_transport_is_a2a_and_unknown_values_fail(monkeypatch, tmp_path):
    monkeypatch.delenv("FIRST_HOP_TRANSPORT", raising=False)
    assert Dispatcher(tmp_path / "a.sqlite").transport == "a2a"
    monkeypatch.setenv("FIRST_HOP_TRANSPORT", " Spacetime ")
    assert Dispatcher(tmp_path / "b.sqlite").transport == "spacetime"
    monkeypatch.setenv("FIRST_HOP_TRANSPORT", "redis")
    with pytest.raises(ValueError, match="FIRST_HOP_TRANSPORT"):
        Dispatcher(tmp_path / "c.sqlite")


async def test_a2a_mode_never_touches_spacetimedb(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch, "SpacetimeClient", MagicMock(side_effect=AssertionError("no SpacetimeDB")))
    sender = AsyncMock(return_value=new_task("task-1", "CASE-900", TaskState.TASK_STATE_SUBMITTED))
    monkeypatch.setattr(dispatch, "send_task", sender)
    d = Dispatcher(tmp_path / "d.sqlite", transport="a2a")
    res = await d.send("EVT-1", SKILL, envelope(), reason="7.3 table")
    assert res == {"event_id": "EVT-1", "skill": SKILL, "agent": "carrier_ap", "task_id": "task-1",
                   "status": "accepted", "duplicate": False}
    assert sender.await_args.args[2].idempotency_key == "event:EVT-1"
    assert d.get("EVT-1")["status"] == "accepted"


# Spacetime mode

async def test_enqueue_sends_the_contract_payload_and_records_queued(stdb, queue_time, tmp_path):
    env = envelope()
    d = Dispatcher(tmp_path / "d.sqlite", transport="spacetime")
    res = await d.send("EVT-1", SKILL, env, reason="7.3 table")
    assert res == {"event_id": "EVT-1", "skill": SKILL, "agent": "carrier_ap", "status": "queued",
                   "duplicate": False}
    (request,) = stdb.requests
    assert request.url.path == "/v1/database/t/call/enqueue_first_hop"
    assert request.headers["authorization"] == "Bearer tok"
    assert stdb.payloads() == [{
        "write_id": "first_hop:EVT-1", "event_id": "EVT-1", "case_id": "CASE-900", "agent": "carrier_ap",
        "skill": SKILL, "envelope": env.model_dump(mode="json"), "queue_ms": queue_time,
        "policy_version": POLICY_VERSION, "reason": "7.3 table"}]
    row = d.get("EVT-1")
    assert row["status"] == "queued" and row["task_id"] is None and row["agent"] == "carrier_ap"


async def test_enqueue_uses_the_router_identity(monkeypatch, tmp_path):
    db = MagicMock(disabled=False)
    db.mutate = AsyncMock()
    factory = MagicMock(return_value=db)
    monkeypatch.setattr(dispatch, "SpacetimeClient", factory)
    await Dispatcher(tmp_path / "d.sqlite", transport="spacetime").send("EVT-1", SKILL, envelope())
    factory.assert_called_once_with(actor="router")
    assert db.mutate.await_args.args[0] == "enqueue_first_hop"
    assert db.mutate.await_args.args[1]["reason"] == "routed"   # the reducer requires a nonempty reason


async def test_redelivered_queued_event_is_a_duplicate_without_task(stdb, queue_time, tmp_path, monkeypatch):
    d = Dispatcher(tmp_path / "d.sqlite", transport="spacetime")
    await d.send("EVT-1", SKILL, envelope())
    again = await d.send("EVT-1", SKILL, envelope())
    assert again["duplicate"] is True and again["status"] == "queued" and again["task_id"] is None
    assert len(stdb.requests) == 1
    # Switching to A2A must not push an event the outbox already holds.
    sender = AsyncMock()
    monkeypatch.setattr(dispatch, "send_task", sender)
    a2a = await Dispatcher(tmp_path / "d.sqlite", transport="a2a").send("EVT-1", SKILL, envelope())
    assert a2a["duplicate"] is True
    sender.assert_not_awaited()


async def test_retry_resends_the_stored_payload_byte_for_byte(stdb, tmp_path):
    stdb.outcomes = ["timeout"]   # may have committed: the retry must be a receipt no-op, not a rejection
    d = Dispatcher(tmp_path / "d.sqlite", transport="spacetime")
    token = QUEUE_MS.set(250)
    try:
        with pytest.raises(DispatchError, match="unavailable"):
            await d.send("EVT-1", SKILL, envelope(), reason="7.3 table")
    finally:
        QUEUE_MS.reset(token)
    first = d.get("EVT-1")
    assert first["status"] == "retry" and "SpacetimeTransportError" in first["reason"]
    # The reclaim re-runs the pipeline: a different envelope, queue time, reason and even route.
    token = QUEUE_MS.set(120_250)
    try:
        res = await d.send("EVT-1", "reconcile_customer_payment",
                           envelope(review_notes=["verifier pii: name"]).model_copy(
                               update={"task_type": "reconcile_customer_payment"}), reason="classifier 0.71")
    finally:
        QUEUE_MS.reset(token)
    assert stdb.bodies[0] == stdb.bodies[1]
    assert stdb.payloads()[1]["queue_ms"] == 250 and stdb.payloads()[1]["envelope"]["subject"] == {
        "invoice_amount": 987.32}
    assert res["status"] == "queued" and res["skill"] == SKILL and res["agent"] == "carrier_ap"
    row = d.get("EVT-1")
    assert row["status"] == "queued" and row["skill"] == SKILL
    with sqlite3.connect(tmp_path / "d.sqlite") as db:   # the local envelope is the one that was sent
        sent = json.loads(db.execute("SELECT envelope FROM dispatch WHERE event_id='EVT-1'").fetchone()[0])
    assert sent["subject"] == {"invoice_amount": 987.32} and sent["task_type"] == SKILL


async def test_the_fake_receipt_rejects_changed_contents(stdb):
    # Guards the fake the byte-for-byte tests rely on: it must behave like the module's write receipt.
    db = stdb.client(actor="router")
    payload = {"write_id": "first_hop:EVT-1", "event_id": "EVT-1", "queue_ms": 1}
    await db.mutate("enqueue_first_hop", payload)
    await db.mutate("enqueue_first_hop", dict(reversed(payload.items())))   # same contents, other key order
    with pytest.raises(SpacetimeHTTPError) as rejected:
        await db.mutate("enqueue_first_hop", {**payload, "queue_ms": 2})
    assert rejected.value.status_code == 530


async def test_reclaim_resends_the_pinned_payload_without_rerunning_the_models(stdb, queue_time, dead_pool,
                                                                               tmp_path, monkeypatch):
    calls = AsyncMock(side_effect=Unavailable("models down"))
    monkeypatch.setattr(pipeline, "complete", calls)
    vault, d = Vault(tmp_path / "v.sqlite"), Dispatcher(tmp_path / "d.sqlite", transport="spacetime")
    stdb.outcomes = ["timeout"]   # committed, response lost
    first = await process(dead_pool, vault, d, event())
    assert first["status"] == "dispatch_failed" and calls.await_count == 1   # the verifier call
    # The re-run would now classify differently (here: no case at all, so triage); the pinned hop wins.
    changed = event()
    changed["payload"].pop("case_id")
    again = await process(dead_pool, vault, d, changed)
    assert again["status"] == "queued" and calls.await_count == 1
    assert stdb.bodies[0] == stdb.bodies[1] and again["route"]["skill"] == SKILL
    assert again["envelope"] == first["envelope"] and d.get("EVT-1")["status"] == "queued"


@pytest.mark.parametrize("delivered", [("queued", None), ("accepted", "task-1")])
def test_a_late_failure_never_downgrades_a_delivered_row(tmp_path, delivered):
    status, task_id = delivered
    d = Dispatcher(tmp_path / "d.sqlite")
    d.record("EVT-1", "CASE-900", SKILL, "carrier_ap", task_id, status, "ok", envelope())
    d.record("EVT-1", "CASE-900", SKILL, "carrier_ap", None, "retry", "enqueue unavailable: timeout", envelope())
    row = d.get("EVT-1")
    assert (row["status"], row["task_id"], row["reason"]) == (status, task_id, "ok")


async def test_reducer_rejection_records_retry_and_stays_retryable(stdb, queue_time, tmp_path):
    stdb.outcomes = [530]   # e.g. ingest has not opened the case yet
    d = Dispatcher(tmp_path / "d.sqlite", transport="spacetime")
    with pytest.raises(DispatchError, match="rejected"):
        await d.send("EVT-1", SKILL, envelope())
    row = d.get("EVT-1")
    assert row["status"] == "retry" and "rejected" in row["reason"] and "530" in row["reason"]
    assert (await d.send("EVT-1", SKILL, envelope()))["status"] == "queued"
    assert stdb.bodies[0] == stdb.bodies[1]


@pytest.mark.parametrize("outcome", [429, 503, "timeout", "connect"])
async def test_transient_failures_stay_pending(stdb, queue_time, tmp_path, outcome):
    stdb.outcomes = [outcome]
    d = Dispatcher(tmp_path / "d.sqlite", transport="spacetime")
    with pytest.raises(DispatchError, match="unavailable"):
        await d.send("EVT-1", SKILL, envelope())
    row = d.get("EVT-1")
    assert row["status"] == "retry" and row["reason"].startswith("enqueue unavailable")
    assert stored_enqueue(tmp_path / "d.sqlite", "EVT-1") is not None


async def test_spacetime_mode_with_disabled_client_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    sender = AsyncMock()
    monkeypatch.setattr(dispatch, "send_task", sender)
    d = Dispatcher(tmp_path / "d.sqlite", transport="spacetime")
    with pytest.raises(DispatchError, match="SPACETIME_DISABLED=0"):
        await d.send("EVT-1", SKILL, envelope())
    sender.assert_not_awaited()   # never a silent fallback to A2A
    assert d.get("EVT-1")["status"] == "retry"
    assert stored_enqueue(tmp_path / "d.sqlite", "EVT-1") is None   # no attempt, so nothing to pin yet


async def test_old_ingest_sqlite_gains_the_enqueue_column(stdb, queue_time, tmp_path):
    path = tmp_path / "ingest.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE dispatch(event_id TEXT PRIMARY KEY, case_id TEXT, skill TEXT, agent TEXT,"
                   " task_id TEXT, status TEXT, attempts INTEGER, reason TEXT, envelope TEXT, updated_at REAL)")
        db.execute("INSERT INTO dispatch VALUES('EVT-0','CASE-1',?,'carrier_ap','task-0','accepted',1,'ok',NULL,0)",
                   (SKILL,))
        db.execute("INSERT INTO dispatch VALUES('EVT-1','CASE-900',?,'carrier_ap',NULL,'retry',1,'send: x',NULL,0)",
                   (SKILL,))
    d = Dispatcher(path, transport="spacetime")
    assert d.get("EVT-0")["task_id"] == "task-0"
    d.record("EVT-2", "CASE-1", None, None, None, "triage", "unlabeled", None)
    assert d.get("EVT-2")["status"] == "triage" and stored_enqueue(path, "EVT-2") is None
    # An A2A retry row from before the switch gets its enqueue payload pinned on the first spacetime attempt.
    assert (await d.send("EVT-1", SKILL, envelope()))["status"] == "queued"
    assert json.loads(stored_enqueue(path, "EVT-1")) == stdb.payloads()[0]
    assert Dispatcher(path).pinned("EVT-1") == stdb.payloads()[0]   # reopening keeps the column and the pin


# Pipeline and consumer

async def test_pipeline_queues_the_redacted_envelope(stdb, queue_time, dead_pool, tmp_path):
    res = await process(dead_pool, Vault(tmp_path / "v.sqlite"),
                        Dispatcher(tmp_path / "d.sqlite", transport="spacetime"), event())
    assert res["status"] == "queued" and res["dispatch"]["status"] == "queued"
    (payload,) = stdb.payloads()
    assert b"billing@example-carrier.com" not in stdb.bodies[0]
    env = payload["envelope"]
    assert env["subject"]["remit_email"].startswith("[[EMAIL_") and env == res["envelope"]
    assert env["case_id"] == payload["case_id"] == "CASE-900" and env["task_type"] == payload["skill"] == SKILL
    assert payload["reason"] == res["route"]["reason"]


async def test_consumer_acks_queued_without_a_second_dispatch_row(stdb, dead_pool, tmp_path, monkeypatch):
    telemetry = AsyncMock()
    monkeypatch.setattr(consumer, "record_dispatch", telemetry)
    r = AsyncMock()
    vault, d = Vault(tmp_path / "v.sqlite"), Dispatcher(tmp_path / "d.sqlite", transport="spacetime")
    kw = {"stream": "events", "group": "router", "triage": "triage", "handler": process}
    entry = f"{int(time.time() * 1000) - 400}-0".encode()
    await consumer._handle(r, dead_pool, vault, d, entry, event(), **kw)
    r.xack.assert_awaited_once_with("events", "router", entry)
    telemetry.assert_not_awaited()   # enqueue_first_hop wrote the queued row with the outbox row
    assert 400 <= stdb.payloads()[0]["queue_ms"] < 10_000

    # Redis re-delivers the same event under a new entry: a duplicate row, no second outbox row.
    redelivered = f"{int(time.time() * 1000)}-1".encode()
    await consumer._handle(r, dead_pool, vault, d, redelivered, event(), **kw)
    assert len(stdb.requests) == 1
    telemetry.assert_awaited_once()
    assert telemetry.await_args.args[2] == "duplicate"
    assert telemetry.await_args.kwargs | {"received_ms": None} == {
        "agent": "carrier_ap", "skill": SKILL, "task_id": "", "received_ms": None, "pins": d}
    r.xack.assert_awaited_with("events", "router", redelivered)


async def test_failed_enqueue_is_not_acknowledged(stdb, dead_pool, tmp_path, monkeypatch):
    stdb.outcomes = [530]
    telemetry = AsyncMock()
    monkeypatch.setattr(consumer, "record_dispatch", telemetry)
    r = AsyncMock()
    await consumer._handle(r, dead_pool, Vault(tmp_path / "v.sqlite"),
                           Dispatcher(tmp_path / "d.sqlite", transport="spacetime"), b"1-0", event(),
                           stream="events", group="router", triage="triage", handler=process)
    r.xack.assert_not_awaited()
    telemetry.assert_not_awaited()


async def test_duplicate_of_an_accepted_event_keeps_its_task(monkeypatch):
    telemetry = AsyncMock()
    monkeypatch.setattr(consumer, "record_dispatch", telemetry)
    handler = AsyncMock(return_value={"event_id": "EVT-1", "case_id": "CASE-900", "status": "duplicate",
                                      "prior": {"agent": "carrier_ap", "skill": SKILL, "task_id": "task-1",
                                                "status": "accepted"}})
    await consumer._handle(AsyncMock(), None, None, None, b"1-0", event(), stream="events", group="router",
                           triage="triage", handler=handler)
    assert telemetry.await_args.kwargs["task_id"] == "task-1"


async def test_duplicate_of_an_unrouted_event_writes_no_row(monkeypatch):
    telemetry = AsyncMock()
    monkeypatch.setattr(consumer, "record_dispatch", telemetry)
    r = AsyncMock()
    handler = AsyncMock(return_value={"event_id": "EVT-1", "case_id": None, "status": "duplicate",
                                      "prior": {"agent": None, "skill": None, "task_id": None, "status": "triage"}})
    await consumer._handle(r, None, None, None, b"1-0", event(), stream="events", group="router",
                           triage="triage", handler=handler)
    telemetry.assert_not_awaited()   # the reducer requires a route for a duplicate row
    r.xack.assert_awaited_once()


async def test_queue_time_is_scoped_to_the_entry_being_handled():
    seen = []

    async def handler(pool, vault, dispatcher, ev):
        seen.append(QUEUE_MS.get())
        return {"event_id": ev["event_id"], "case_id": None, "status": "not_routed",
                "route": {"reason": "gps.ping is not routed (7.3)"}}

    entry = f"{int(time.time() * 1000) - 1500}-0".encode()
    await consumer._handle(AsyncMock(), None, None, None, entry, {"event_id": "GPS-1", "type": "gps.ping"},
                           stream="events", group="router", triage="triage", handler=handler)
    assert 1500 <= seen[0] < 10_000 and QUEUE_MS.get() == 0


def test_queue_ms_matches_the_telemetry_arithmetic():
    assert queue_ms(b"1000-0", 1123) == 123 and queue_ms("1000-3", 900) == 0
    assert queue_ms("0-0", 2 ** 40) == 4294967295


# Shared helper

def test_first_hop_meta_is_shared_and_harness_free():
    import common.first_hop
    from harness.ingest import firsthop
    assert firsthop.first_hop_meta is common.first_hop.first_hop_meta is dispatch.first_hop_meta
    meta = common.first_hop.first_hop_meta("EVT-1", envelope())
    assert (meta.idempotency_key, meta.from_agent, meta.correlation_id, meta.causation_id, meta.case_id,
            meta.priority, meta.visited, meta.delegation_depth) == (
        "event:EVT-1", "router", "CASE-900", "EVT-1", "CASE-900", "P0", [], 0)
    # The dispatch worker imports it without the harness (which pulls in the vault).
    probe = "import sys, common.first_hop; print(sorted(m for m in sys.modules if m.split('.')[0] == 'harness'))"
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert loaded.stdout.strip() == "[]"
