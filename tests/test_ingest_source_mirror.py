"""Ingest mirrors source records into SpacetimeDB ahead of routing, pipelines its writes and classifies retries
(T2; source-records plan, cutover plan H2 and H3)."""
import asyncio
import copy
import hashlib
import json
import logging
import random

import httpx
import pytest

from common import policy
from common.spacetime_client import (
    SpacetimeClient,
    SpacetimeConfigurationError,
    SpacetimeHTTPError,
    SpacetimePolicyError,
    SpacetimeTransportError,
)
from ingest import main as ingest_main
from ingest import mirror as spacetime_mirror
from ingest import source_export
from ingest.main import connect
from tests import test_ingest, test_ingest_mirror
from tests.test_ingest import _run
from tests.test_ingest_mirror import FakeRedis, FakeSpacetime, _process

world = test_ingest.world
clean_environment = test_ingest_mirror.clean_environment
SOURCE = spacetime_mirror.SOURCE_REDUCER


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """The digest cache lives in $SIM_DATA_DIR; never let a test touch the checkout's data/."""
    monkeypatch.setenv("SIM_DATA_DIR", str(tmp_path))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """The monotonic clock of the mirror's backoff and both throttles (asyncio's own clock is left alone)."""
    fake = Clock()
    monkeypatch.setattr(spacetime_mirror, "monotonic", fake)
    monkeypatch.setattr(ingest_main, "monotonic", fake)
    return fake


class Server(FakeSpacetime):
    """FakeSpacetime plus SpacetimeDB's failure answers: a persistent status per reducer (`status`, e.g. 530 for a
    reducer that refused the write), connection failures (`down`), per-record refusals (`refuse`), and random
    latency. Every request is logged as ("start", reducer, payload) before its outcome."""

    def __init__(self, log: list | None = None, delay: float = 0.0, seed: int = 7):
        super().__init__(log)
        self.status: dict[str, int] = {}
        self.refuse: set[str] = set()
        self.down = False
        self.delay = delay
        self.random = random.Random(seed)
        self.requests: list[tuple[str, dict]] = []
        self.inflight = self.peak = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        reducer = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(json.loads(request.content)[0])
        self.requests.append((reducer, payload))
        self.log.append(("start", reducer, payload))
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.random.uniform(0, self.delay))
            if self.down:
                raise httpx.ConnectError("refused", request=request)
            if reducer in self.status:
                return httpx.Response(self.status[reducer])
            if reducer == SOURCE and payload["id"] in self.refuse:
                return httpx.Response(530)
            if reducer == SOURCE:
                # The module's own check: SHA-256 over record_json exactly as sent.
                assert hashlib.sha256(payload["record_json"].encode()).hexdigest() == payload["digest"]
            return self.handler(request)
        finally:
            self.inflight -= 1

    def mirror(self, enrichment=None) -> spacetime_mirror.Mirror:
        client = SpacetimeClient(actor="ingest", disabled=False, transport=httpx.MockTransport(self.handle))
        return spacetime_mirror.Mirror(client, enrichment)

    def sources(self) -> dict[str, dict]:
        return {payload["id"]: payload for (reducer, _key), payload in self.rows.items() if reducer == SOURCE}

    def sent(self) -> list[str]:
        return [payload["id"] for reducer, payload in self.requests if reducer == SOURCE]


class Pipeline:
    def __init__(self, redis, transaction: bool):
        self.redis, self.transaction, self.ops = redis, transaction, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def xadd(self, stream, fields, **_kwargs):
        self.ops.append(("xadd", stream, dict(fields)))

    def xack(self, stream, group, entry_id):
        self.ops.append(("xack", stream, group, entry_id))

    async def execute(self):
        self.redis.transactions.append((self.transaction, list(self.ops)))
        for op in self.ops:
            (self.redis.acked.append(op[3]) if op[0] == "xack" else self.redis.dead.append(op))


class DeadLetterRedis(FakeRedis):
    def __init__(self, log: list):
        super().__init__(log)
        self.transactions: list = []
        self.dead: list = []

    def pipeline(self, transaction: bool = True) -> Pipeline:
        return Pipeline(self, transaction)


class Sleeps(list):
    """Recorded in place of asyncio.sleep; advances the fake clock when there is one."""

    def __init__(self, clock: Clock | None = None):
        super().__init__()
        self.clock = clock

    async def __call__(self, seconds: float) -> None:
        self.append(seconds)
        if self.clock:
            self.clock.now += seconds


async def _ingest(events: list[dict], db) -> None:
    for event in events:
        await ingest_main.handle(event, db)
    db.commit()


async def _up_to_invoice(world, server: Server, scenario: str = "clean"):
    """Ingest everything before the carrier invoice email; returns the db, the load and that email's raw entry."""
    _gen, load, events = _run(world, scenario)
    index = next(index for index, event in enumerate(events) if event["type"] == "email.received"
                 and any(str(name).startswith("INV-") for name in event["payload"]["attachments"]))
    db = connect()
    assert all(await _process(events[:index], db, server, FakeRedis([]), mirror=server.mirror()))
    return db, load, {"json": json.dumps(events[index])}


def _in_flight(log: list, index: int) -> int:
    before = log[:index]
    return sum(entry[0] == "start" for entry in before) - sum(entry[0] in ("write", "noop") for entry in before)


def _small_dataset(cases: int = 30) -> dict:
    return {"schema": 1, "generated_at": "2026-10-24T00:00:00+00:00", "source_kind": "w4_ingest_export",
            "cases": {f"CASE-{n}": {"case_id": f"CASE-{n}", "n": n} for n in range(cases)},
            "companies": {"C-1": {"id": "C-1"}}, "contacts": {"C-1": [{"id": "CT-C-1"}]}}


# Payload shape and digest


def test_source_record_is_the_exported_object_hashed_exactly_as_sent():
    case = {"case_id": "CASE-1", "carrier": {"name": "Café Ñandú"}, "amount": 2150.1, "nested": {"b": 1, "a": [3, 1]}}
    record = spacetime_mirror.source_record("case:CASE-1", case)
    assert set(record) == {"id", "kind", "record_json", "digest"}
    assert record["id"] == "case:CASE-1" and record["kind"] == "case"
    assert record["digest"] == hashlib.sha256(record["record_json"].encode("utf-8")).hexdigest()
    assert json.loads(record["record_json"]) == case
    # ASCII only, so the module's UTF-8 hash of the decoded JS string sees the bytes Python hashed.
    assert record["record_json"].isascii()
    assert record["record_json"] == json.dumps(case, sort_keys=True, separators=(",", ":"))
    assert spacetime_mirror.source_record("case:CASE-1", dict(reversed(case.items())))["digest"] == record["digest"]
    for record_id, kind in (("company:C-1", "company"), ("contacts:C-1", "contacts"), ("dataset:meta", "dataset")):
        assert spacetime_mirror.source_record(record_id, {})["kind"] == kind


def test_case_records_cover_the_case_its_counterparties_and_meta_only():
    dataset = {"schema": 1, "generated_at": "2026-10-24T00:00:00+00:00", "source_kind": "w4_ingest_export",
               "cases": {"CASE-1": {"carrier_id": "C-1", "shipper_id": "S-1"}, "CASE-2": {"carrier_id": "C-2"}},
               "companies": {"C-1": {"id": "C-1"}, "S-1": {"id": "S-1"}, "C-2": {"id": "C-2"}},
               "contacts": {"C-1": [{"id": "CT-C-1"}]}}
    ids = [record["id"] for record in spacetime_mirror.case_records(dataset, ["CASE-1", "CASE-MISSING"])]
    # No contacts row for a company the export has none for, and nothing invented for an unknown case.
    assert ids == ["case:CASE-1", "company:C-1", "contacts:C-1", "company:S-1", "dataset:meta"]
    [meta] = spacetime_mirror.case_records(dataset, [])
    assert json.loads(meta["record_json"]) == {"schema": 1, "generated_at": "2026-10-24T00:00:00+00:00",
                                               "source_kind": "w4_ingest_export"}
    assert {record["id"] for record in spacetime_mirror.dataset_records(dataset)} == {
        "case:CASE-1", "case:CASE-2", "company:C-1", "company:S-1", "company:C-2", "contacts:C-1", "dataset:meta"}


async def test_mirrored_records_are_what_source_json_carries(world, tmp_path):
    _gen, load, events = _run(world, "clean")
    server = Server()
    mirror = server.mirror()
    exporter = source_export.Exporter(tmp_path / "source.json")
    db = connect()
    try:
        assert all(await _process(events, db, server, FakeRedis([]), exporter, mirror))
        exporter.flush(db)
        await mirror.sync_sources(exporter.take())  # the main loop's pass beside the flush
    finally:
        db.close()
    assert not server.rejected
    exported = json.loads((tmp_path / "source.json").read_text())
    sources = server.sources()
    carrier, shipper = load["carrier"]["id"], load["shipper"]["id"]
    expected = {f"case:{load['case_id']}": exported["cases"][load["case_id"]],
                f"company:{carrier}": exported["companies"][carrier],
                f"company:{shipper}": exported["companies"][shipper],
                f"contacts:{carrier}": exported["contacts"][carrier],
                f"contacts:{shipper}": exported["contacts"][shipper],
                "dataset:meta": {key: exported[key] for key in ("generated_at", "schema", "source_kind")}}
    for record_id, record in expected.items():
        payload = sources[record_id]
        assert set(payload) == {"write_id", "id", "kind", "record_json", "digest"}
        assert payload["kind"] == record_id.split(":")[0]
        assert payload["write_id"] == f"ingest:{SOURCE}:{record_id}:{payload['digest'][:16]}"
        assert json.loads(payload["record_json"]) == record, record_id
    assert isinstance(json.loads(sources[f"contacts:{carrier}"]["record_json"]), list)
    assert set(sources) == {f"{kind}:{key}" for name, kind in spacetime_mirror.SOURCE_MAPS
                            for key in exported[name]} | {"dataset:meta"}


# Ordering and pipelining (H3)


@pytest.mark.parametrize(("scenario", "seed"), [("clean", 1), ("clean", 2), ("detention_unsupported", 3),
                                                ("bank_change", 4), ("shipper_short_pay", 5)])
async def test_source_and_counterparties_land_before_open_case_and_open_case_before_invoice(world, scenario, seed):
    _gen, _load, events = _run(world, scenario)
    log: list = []
    server = Server(log, delay=0.01, seed=seed)
    db = connect()
    try:
        assert all(await _process(events, db, server, FakeRedis(log), mirror=server.mirror()))
    finally:
        db.close()
    assert not server.rejected
    assert 1 < server.peak <= spacetime_mirror.DIRECTORY_CONCURRENCY  # concurrent, and bounded by the gate
    sources_written: set[str] = set()
    routed = 0
    opened: set[str] = set()  # cases whose open_case went out since the last published event
    for index, entry in enumerate(log):
        if entry[0] == "xadd":
            opened.clear()
        if entry[0] == "start" and entry[1] == "open_case":
            opened.add(entry[2]["case_id"])
        if entry[0] == "start" and entry[1] == SOURCE:
            assert entry[2]["id"].removeprefix("case:") not in opened, "source record sent after open_case"
        if entry[0] in ("write", "noop") and entry[1] == SOURCE:
            sources_written.add(entry[2]["id"])
        if entry[0] == "start" and entry[1] in ("open_case", "upsert_invoice"):
            # Nothing else is in flight: every source record and company write of this case has completed,
            # and an invoice is sent only after its case's open_case has.
            assert _in_flight(log, index) == 0, entry[1]
        if entry[0] == "start" and entry[1] == "upsert_invoice":
            assert any(item[0] == "write" and item[1] == "open_case" and item[2]["case_id"] == entry[2]["case_id"]
                       for item in log[:index])
        if entry[0] == "xadd" and entry[2]["type"] in spacetime_mirror.ROUTES:
            assert _in_flight(log, index) == 0
        if entry[0] == "xadd" and entry[2]["type"] in ingest_main.CASE_EVENTS:
            # A case-opening event always has a case in the export, and its source record is already committed.
            assert f"case:{entry[2]['payload']['case_id']}" in sources_written, entry[2]["type"]
            routed += 1
    assert routed


async def test_with_the_mirror_off_a_routed_case_is_exported_before_its_event(world, tmp_path):
    """Unrouted progress flushes the export at most once per max_age_s; a routed case never waits for that."""
    _gen, _load, events = _run(world, "clean")
    target = tmp_path / "source.json"
    exporter = source_export.Exporter(target, max_age_s=10 ** 9)
    exporter.last_flush = spacetime_mirror.monotonic()  # nothing is ever due on its own
    readable: dict[str, bool] = {}

    class CheckingRedis(FakeRedis):
        async def xadd(self, stream, fields, **kwargs):
            event = json.loads(fields["json"])
            if event["type"] == "carrier_invoice.received":
                payload = event["payload"]
                exported = json.loads(target.read_text())["cases"] if target.exists() else {}
                readable[payload["case_id"]] = exported.get(payload["case_id"], {}).get("invoice_id") == \
                    payload["invoice_id"]
            await super().xadd(stream, fields, **kwargs)

    mirror = spacetime_mirror.Mirror()
    assert mirror.disabled
    db = connect()
    try:
        assert all(await _process(events, db, Server(), CheckingRedis([]), exporter, mirror))
    finally:
        db.close()
    assert readable and all(readable.values())


async def test_a_failed_source_record_keeps_the_case_unopened_and_the_event_unpublished(world):
    log: list = []
    server = Server(log)
    db, _load, entry = await _up_to_invoice(world, server)
    redis = FakeRedis(log)
    try:
        server.status[SOURCE] = 503
        start = len(log)
        assert await ingest_main._process_entry(redis, db, "9-0", entry, None, server.mirror()) is False
    finally:
        db.close()
    attempted = log[start:]
    assert any(item[0] == "start" and item[1] == SOURCE for item in attempted)
    assert not any(item[0] == "start" and item[1] in ("open_case", "upsert_invoice") for item in attempted)
    assert not any(item[0] == "xadd" for item in attempted) and "9-0" not in redis.acked


# Digest cache


async def test_the_digest_cache_is_persisted_and_a_restart_skips_unchanged_records(world, tmp_path):
    _gen, load, events = _run(world, "clean")
    server = Server()
    mirror = server.mirror()
    db = connect()
    try:
        assert all(await _process(events, db, server, FakeRedis([]), mirror=mirror))
        dataset = source_export.build(db)
    finally:
        db.close()
    await mirror.sync_sources(dataset)
    stored = json.loads((tmp_path / "source_mirror.json").read_text())
    assert set(stored) == {"target", "digests"} and stored["target"] == "http://localhost:3000/freightops"
    assert stored["digests"] == {record_id: payload["digest"] for record_id, payload in server.sources().items()}
    assert not [path.name for path in tmp_path.iterdir() if path.name.startswith(".source_mirror")]

    restarted = Server()
    again = restarted.mirror()  # loads $SIM_DATA_DIR/source_mirror.json
    await again.source_records(dataset, [load["case_id"]])
    await again.sync_sources(dataset)
    assert restarted.requests == []
    changed = copy.deepcopy(dataset)
    changed["cases"][load["case_id"]]["deduction_reason"] = "late delivery"
    await again.source_records(changed, [load["case_id"]])
    assert restarted.sent() == [f"case:{load['case_id']}"]
    assert json.loads((tmp_path / "source_mirror.json").read_text())["digests"][f"case:{load['case_id']}"] == \
        restarted.sources()[f"case:{load['case_id']}"]["digest"]


async def test_a_cache_kept_for_another_database_or_unreadable_is_ignored(world, tmp_path):
    path = tmp_path / "source_mirror.json"
    target = "http://localhost:3000/freightops"
    record = spacetime_mirror.source_record("case:CASE-1", {"case_id": "CASE-1"})
    spacetime_mirror.SourceDigests(target, path).stored(record)
    assert spacetime_mirror.SourceDigests(target, path).current(record)
    assert not spacetime_mirror.SourceDigests("https://maincloud.spacetimedb.com/at350-freightops", path).current(record)
    for broken in ("not json", "[]", '{"target": "http://localhost:3000/freightops", "digests": 3}'):
        path.write_text(broken)
        assert spacetime_mirror.SourceDigests(target, path).digests == {}

    spacetime_mirror.SourceDigests(target, path).stored(record)
    server = Server()
    client = SpacetimeClient(actor="ingest", disabled=False, database="freightops-other",
                             transport=httpx.MockTransport(server.handle))
    mirror = spacetime_mirror.Mirror(client)
    assert mirror.digests.target == "http://localhost:3000/freightops-other"
    assert await mirror.put_source(record) is True and server.sent() == ["case:CASE-1"]
    assert await mirror.put_source(record) is False and server.sent() == ["case:CASE-1"]


async def test_a_retried_entry_resends_no_source_record_that_landed(world):
    log: list = []
    server = Server(log)
    db, load, entry = await _up_to_invoice(world, server)
    mirror = server.mirror()
    redis = FakeRedis(log)
    try:
        server.status["upsert_invoice"] = 503
        assert await ingest_main._process_entry(redis, db, "9-0", entry, None, mirror) is False
        first = [payload["id"] for payload in server.sources().values()]
        del server.status["upsert_invoice"]
        start = len(server.requests)
        assert await ingest_main._process_entry(redis, db, "9-0", entry, None, mirror) is True
    finally:
        db.close()
    assert f"case:{load['case_id']}" in first
    # Only dataset:meta is new (its generated_at moved); nothing the module would treat as a duplicate.
    assert [payload["id"] for name, payload in server.requests[start:] if name == SOURCE] == ["dataset:meta"]
    assert not [item for item in log if item[0] == "noop" and item[1] == SOURCE]


# Full-dataset diffing


async def test_each_flush_mirrors_every_changed_record_and_then_nothing(world, tmp_path):
    _gen, _load, events = _run(world, "shipper_short_pay")
    db = connect()
    try:
        await _ingest(events, db)
        exporter = source_export.Exporter(tmp_path / "source.json")
        exporter.flush(db)
        dataset = exporter.take()
        assert dataset is not None and exporter.take() is None
        assert json.loads((tmp_path / "source.json").read_text())["generated_at"] == dataset["generated_at"]
        server = Server(delay=0.005)
        mirror = server.mirror()
        await mirror.sync_sources(dataset)
        expected = {record["id"] for record in spacetime_mirror.dataset_records(dataset)}
        assert set(server.sources()) == expected and sorted(server.sent()) == sorted(expected)
        assert 1 < server.peak <= spacetime_mirror.DIRECTORY_CONCURRENCY
        assert mirror.backlog == {}
        server.requests.clear()
        await mirror.sync_sources(dataset)
        await mirror.sync_sources(None)
        exporter.flush(db)  # not dirty: nothing new to take
        await mirror.sync_sources(exporter.take())
        assert server.requests == []
        exporter.changed()
        exporter.flush(db)
        await mirror.sync_sources(exporter.take())
        assert server.sent() == ["dataset:meta"]
    finally:
        db.close()


async def test_an_outage_halts_the_pass_keeps_the_backlog_and_backs_off(clock, caplog):
    server = Server()
    mirror = server.mirror()
    dataset = _small_dataset()
    total = len(spacetime_mirror.dataset_records(dataset))
    server.down = True
    with caplog.at_level(logging.WARNING, "ingest.mirror"):
        await mirror.sync_sources(dataset)  # never raises
        assert len(mirror.backlog) == total
        assert 0 < len(server.requests) <= spacetime_mirror.DIRECTORY_CONCURRENCY  # halted after the first wave
        assert mirror.retry_at == clock.now + 1
        attempts = len(server.requests)
        await mirror.sync_sources(None)  # still backing off: nothing sent
        assert len(server.requests) == attempts
        waits = []
        for _ in range(6):
            clock.now = mirror.retry_at
            await mirror.sync_sources(None)
            waits.append(mirror.retry_at - clock.now)
        assert waits == [2, 4, 8, 16, 30, 30]
    # Outage warnings repeat at most once a minute: at 0 s and at 61 s of the 61 s simulated.
    assert len([record for record in caplog.records if "not mirrored yet" in record.message]) == 2
    server.down = False
    clock.now = mirror.retry_at
    await mirror.sync_sources(None)
    assert len(server.sources()) == total and mirror.backlog == {} and mirror.retry_s == 1.0


@pytest.mark.parametrize("status", [402, 408, 503])
async def test_an_exhausted_or_overloaded_server_halts_the_pass_instead_of_refusing_records(clock, status):
    server = Server()
    mirror = server.mirror()
    dataset = _small_dataset(5)
    server.status[SOURCE] = status
    await mirror.sync_sources(dataset)
    assert mirror.rejected == {} and len(mirror.backlog) == len(spacetime_mirror.dataset_records(dataset))
    assert mirror.retry_at == clock.now + 1
    del server.status[SOURCE]
    clock.now = mirror.retry_at
    await mirror.sync_sources(None)
    assert mirror.backlog == {} and len(server.sources()) == len(spacetime_mirror.dataset_records(dataset))


async def test_a_refused_record_is_skipped_until_it_changes(caplog):
    server = Server()
    mirror = server.mirror()
    dataset = _small_dataset(5)
    server.refuse.add("case:CASE-3")
    with caplog.at_level(logging.WARNING, "ingest.mirror"):
        await mirror.sync_sources(dataset)
    assert "case:CASE-3" not in server.sources() and len(server.sources()) == len(dataset["cases"]) + 2
    assert mirror.backlog == {} and mirror.retry_at == 0
    assert any("refused 1 source records" in record.message and "case:CASE-3" in record.message
               and "HTTP 530" in record.message for record in caplog.records)
    server.requests.clear()
    await mirror.sync_sources(copy.deepcopy(dataset))
    assert server.requests == []
    changed = copy.deepcopy(dataset)
    changed["cases"]["CASE-3"]["n"] = "fixed"
    server.refuse.clear()
    await mirror.sync_sources(changed)
    assert server.sent() == ["case:CASE-3"]


async def test_a_routed_write_supersedes_an_older_backlog_entry():
    server = Server()
    mirror = server.mirror()
    old = _small_dataset(3)
    server.down = True
    await mirror.sync_sources(old)
    assert "case:CASE-1" in mirror.backlog
    server.down = False
    new = copy.deepcopy(old)
    new["cases"]["CASE-1"]["n"] = "newer"
    await mirror.source_records(new, ["CASE-1"])
    assert "case:CASE-1" not in mirror.backlog
    routed_at = len(server.requests)
    mirror.retry_at = 0
    await mirror.sync_sources(None)
    assert "case:CASE-1" not in [payload["id"] for _name, payload in server.requests[routed_at:]]
    assert len(server.requests) > routed_at  # the rest of the backlog did go out
    assert json.loads(server.sources()["case:CASE-1"]["record_json"])["n"] == "newer"


async def test_an_oversized_record_is_refused_without_a_request():
    server = Server()
    mirror = server.mirror()
    huge = spacetime_mirror.source_record("case:CASE-BIG", {"text": "x" * spacetime_mirror.MAX_PAYLOAD_CHARS})
    with pytest.raises(spacetime_mirror.RecordTooLarge) as raised:
        await mirror.put_source(huge)
    assert raised.value.write_id.startswith(f"ingest:{SOURCE}:case:CASE-BIG:")
    assert server.requests == [] and ingest_main.failure_class(raised.value) == ingest_main.DETERMINISTIC
    assert await mirror.put_source(spacetime_mirror.source_record("case:CASE-OK", {"text": "x" * 200_000}))


# Retry classification (H2)


@pytest.mark.parametrize(("error", "expected"), [
    (SpacetimeHTTPError(530, mutation=True), "deterministic"),  # the reducer refused the write
    (SpacetimeHTTPError(400, mutation=True), "deterministic"),
    (SpacetimeHTTPError(401, mutation=True), "deterministic"),
    (SpacetimeHTTPError(403, mutation=True), "deterministic"),
    (SpacetimeHTTPError(404, mutation=True), "deterministic"),
    (SpacetimeHTTPError(500, mutation=True), "transient"),
    (SpacetimeHTTPError(502, mutation=True), "transient"),
    (SpacetimeHTTPError(503, mutation=True), "transient"),
    (SpacetimeHTTPError(504, mutation=True), "transient"),
    (SpacetimeHTTPError(429, mutation=True), "transient"),
    (SpacetimeHTTPError(402, mutation=True), "transient"),  # Maincloud energy budget exhausted
    (SpacetimeHTTPError(408, mutation=True), "transient"),
    (SpacetimeHTTPError(422, mutation=True), "deterministic"),
    (SpacetimeTransportError(timeout=True, may_have_committed=True), "transient"),
    (SpacetimeTransportError(timeout=False, may_have_committed=False), "transient"),
    (SpacetimeHTTPError(409, mutation=True), "other"),
    (SpacetimeConfigurationError("bad"), "other"),
    (SpacetimePolicyError("open_case", policy.Decision("deny")), "other"),
    (ValueError("bad json"), "other"),
    (OSError("disk"), "other"),
])
def test_failure_classes(error, expected):
    assert ingest_main.failure_class(error) == expected


@pytest.mark.parametrize("status", [530, 400, 401, 403, 404])
async def test_a_deterministic_failure_is_retried_twice_then_dead_lettered_atomically(world, status):
    log: list = []
    server = Server(log)
    db, _load, entry = await _up_to_invoice(world, server)
    redis = DeadLetterRedis(log)
    retry = ingest_main.Retry()
    mirror = server.mirror()
    server.status["open_case"] = status
    try:
        start = len(log)
        assert await ingest_main._process_entry(redis, db, "9-0", entry, None, mirror, retry) is False
        assert await ingest_main._process_entry(redis, db, "9-0", entry, None, mirror, retry) is False
        assert redis.transactions == [] and "9-0" not in redis.acked and retry.failures == {"9-0": 2}
        assert await ingest_main._process_entry(redis, db, "9-0", entry, None, mirror, retry) is True
        invoices = db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    finally:
        db.close()
    assert [item[1] for item in log[start:] if item[0] == "start"].count("open_case") == 3
    [(transaction, ops)] = redis.transactions
    assert transaction is True
    [(_xadd, stream, fields), ack] = ops
    assert stream == ingest_main.DEAD_LETTER_STREAM == f"{ingest_main.RAW_STREAM}:dead_letter"
    assert set(fields) == {"entry_id", "reason", "json"}
    assert fields["entry_id"] == "9-0" and fields["json"] == entry["json"]
    assert f"HTTP {status}" in fields["reason"] and "after 3 attempts" in fields["reason"]
    assert "writing ingest:open_case:" in fields["reason"]
    assert ack == ("xack", ingest_main.RAW_STREAM, ingest_main.GROUP, "9-0")
    assert not any(item[0] == "xadd" for item in log[start:])  # none of the poison entry's events went out
    assert invoices == 0  # its ingest state was rolled back with it
    assert retry.failures == {} and not retry.outage


async def test_a_success_clears_the_deterministic_count(world):
    server = Server()
    db, _load, entry = await _up_to_invoice(world, server)
    redis = DeadLetterRedis([])
    retry = ingest_main.Retry()
    mirror = server.mirror()
    try:
        server.status["open_case"] = 530
        for _ in range(2):
            assert await ingest_main._process_entry(redis, db, "9-0", entry, None, mirror, retry) is False
        del server.status["open_case"]
        assert await ingest_main._process_entry(redis, db, "9-0", entry, None, mirror, retry) is True
    finally:
        db.close()
    assert redis.dead == [] and redis.acked == ["9-0"] and retry.failures == {}


@pytest.mark.parametrize("failure", ["down", 500, 503, 429, 402, 408])
async def test_an_outage_backs_off_without_ever_dead_lettering(world, clock, caplog, failure):
    log: list = []
    server = Server(log)
    db, _load, entry = await _up_to_invoice(world, server)
    redis = DeadLetterRedis(log)
    sleeps = Sleeps(clock)
    retry = ingest_main.Retry(sleeps)
    mirror = server.mirror()
    if failure == "down":
        server.down = True
    else:
        server.status[SOURCE] = failure
    try:
        with caplog.at_level(logging.INFO, "ingest"):
            for _ in range(12):
                assert await ingest_main._process_entry(redis, db, "9-0", entry, None, mirror, retry) is False
                assert retry.outage
                await retry.wait()
            server.down = False
            server.status.clear()
            assert await ingest_main._process_entry(redis, db, "9-0", entry, None, mirror, retry) is True
    finally:
        db.close()
    assert sleeps == [1, 2, 4, 8, 16, 30, 30, 30, 30, 30, 30, 30]
    assert redis.transactions == [] and redis.dead == [] and redis.acked == ["9-0"]
    # Attempts at 0, 1, 3, 7, 15, 31, 61, 91, 121, 151, 181, 211 s: warned at 0, 61, 121 and 181 s, no tracebacks.
    assert len([record for record in caplog.records if "SpacetimeDB unavailable" in record.message]) == 4
    assert all(record.exc_info is None for record in caplog.records)
    assert any("reachable again after 241s" in record.message for record in caplog.records)
    assert not retry.outage and retry.backoff_s == 1.0
    assert any(item[0] == "xadd" and item[2]["type"] == "carrier_invoice.received" for item in log)


async def test_other_failures_keep_the_one_second_retry_forever(world):
    server = Server()
    db, _load, entry = await _up_to_invoice(world, server)
    redis = DeadLetterRedis([])
    sleeps = Sleeps()
    retry = ingest_main.Retry(sleeps)
    server.status["open_case"] = 409
    try:
        for _ in range(5):
            assert await ingest_main._process_entry(redis, db, "9-0", entry, None, server.mirror(), retry) is False
            await retry.wait()
        assert await ingest_main._process_entry(redis, db, "8-0", {"json": "not json"}, None, None, retry) is False
        await retry.wait()
    finally:
        db.close()
    assert sleeps == [1] * 6 and redis.dead == [] and redis.acked == [] and not retry.outage


async def test_an_outage_stops_the_batch_so_later_entries_stay_pending_in_order(monkeypatch):
    processed = []
    failure = {"error": SpacetimeTransportError(timeout=False, may_have_committed=False)}

    async def process_entry(_r, _db, entry_id, _values, _exporter, _mirror, retry):
        processed.append(entry_id)
        if entry_id == "2-0":
            return await retry.failed(None, entry_id, "{}", failure["error"])
        retry.succeeded(entry_id)
        return True

    monkeypatch.setattr(ingest_main, "_process_entry", process_entry)
    entries = [(f"{n}-0", {"json": "{}"}) for n in range(1, 5)]
    assert await ingest_main._process_batch(None, None, entries, None, None, ingest_main.Retry()) is False
    assert processed == ["1-0", "2-0"]
    # A deterministic failure does not stop the batch, as before.
    processed.clear()
    failure["error"] = SpacetimeHTTPError(530, mutation=True)
    assert await ingest_main._process_batch(None, None, entries, None, None, ingest_main.Retry()) is False
    assert processed == ["1-0", "2-0", "3-0", "4-0"]


async def test_a_dead_letter_that_cannot_be_written_leaves_the_entry_pending():
    class BrokenRedis:
        def pipeline(self, transaction=True):
            raise ConnectionError("redis down")

    retry = ingest_main.Retry()
    retry.failures["9-0"] = 2
    assert await retry.failed(BrokenRedis(), "9-0", "{}", SpacetimeHTTPError(530, mutation=True)) is False
    assert retry.failures["9-0"] == 3  # the next deterministic failure tries the dead letter again


# The ingest loop


async def test_the_ingest_loop_holds_the_queue_through_an_outage_and_mirrors_the_whole_export(
        world, monkeypatch, clock):
    _gen, load, events = _run(world, "clean")
    log: list = []
    server = Server(log)
    mirror = server.mirror()
    db = connect()

    class StopLoop(Exception):
        pass

    class LoopRedis(DeadLetterRedis):
        def __init__(self):
            super().__init__(log)
            self.pending = [(f"{n}-0", {b"json": json.dumps(event).encode()}) for n, event in enumerate(events)]
            self.reads = 0

        async def xgroup_create(self, *_args, **_kwargs):
            pass

        async def xack(self, _stream, _group, entry_id):
            self.acked.append(entry_id)
            self.pending = [item for item in self.pending if item[0] != entry_id]

        async def xreadgroup(self, _group, _consumer, streams, count, block=None):
            self.reads += 1
            if self.reads > 200:
                raise StopLoop
            if streams[ingest_main.RAW_STREAM] == "0" and self.pending:
                return [(ingest_main.RAW_STREAM, self.pending[:count])]
            return []

        async def aclose(self):
            pass

    redis = LoopRedis()
    sleeps = Sleeps(clock)
    retry_class = ingest_main.Retry

    async def sleep_then_recover(seconds):
        await sleeps(seconds)
        server.down = len(sleeps) < 4  # SpacetimeDB is back after the fourth backoff

    server.down = True
    monkeypatch.setattr(ingest_main.redis, "from_url", lambda _url: redis)
    monkeypatch.setattr(ingest_main, "connect", lambda: db)
    monkeypatch.setattr(ingest_main.spacetime_mirror, "Mirror", lambda: mirror)
    monkeypatch.setattr(ingest_main, "Retry", lambda: retry_class(sleep_then_recover))
    monkeypatch.setattr(ingest_main.DirectoryWatch, "check", lambda self: None)
    with pytest.raises(StopLoop):
        await ingest_main.main()
    assert sleeps == [1, 2, 4, 8]
    assert redis.pending == [] and redis.dead == [] and not server.rejected
    assert sorted(redis.acked, key=lambda entry_id: int(entry_id.split("-")[0])) == \
        [f"{n}-0" for n in range(len(events))]
    check = connect()
    try:
        exported = source_export.build(check)
    finally:
        check.close()
    # The flush beside the loop mirrored every record of the export, not only the routed case's.
    assert set(server.sources()) == {record["id"] for record in spacetime_mirror.dataset_records(exported)}
    assert json.loads(server.sources()[f"case:{load['case_id']}"]["record_json"]) == \
        json.loads(json.dumps(exported["cases"][load["case_id"]], default=str))
