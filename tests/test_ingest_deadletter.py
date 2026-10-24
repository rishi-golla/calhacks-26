"""Bounded retries and the dead-letter stream keep one bad raw entry from stalling ingest."""
import json
import logging
import sqlite3

import httpx
import pytest

from ingest import main as ingest_main
from ingest import mirror as spacetime_mirror
from ingest.main import connect
from tests import test_ingest
from tests.test_ingest import _run
from tests.test_ingest_mirror import FakeSpacetime

world = test_ingest.world
RAW, DEAD = ingest_main.RAW_STREAM, ingest_main.DEAD_LETTER_STREAM
ACCOUNT, ROUTING = "000123456789", "021000021"  # what a bank-change payload carries; never logged


class Drained(Exception):
    """The fake stream has nothing left to deliver; ends `main()`."""


class StreamRedis:
    """One consumer group on `raw` with Redis' delivery semantics: `>` delivers new entries (times_delivered=1),
    `0` re-delivers this consumer's pending entries (times_delivered += 1), XACK clears them. Each list in
    `batches` becomes visible once the previous one has been read, like the simulator appending over time."""

    def __init__(self, *batches: list[dict], max_reads: int = 200):
        self.batches = list(batches)
        self.entries: list[tuple[bytes, dict]] = []
        self.delivered = 0
        self.pending: dict[bytes, int] = {}
        self.acked: list[bytes] = []
        self.streams: dict[str, list[dict]] = {}
        self.maxlen: dict[str, int] = {}
        self.reads = 0
        self.max_reads = max_reads
        self.sequence = 0

    def _append(self, fields: dict) -> bytes:
        self.sequence += 1
        entry_id = f"{self.sequence}-0".encode()
        self.entries.append((entry_id, {key.encode(): value.encode() if isinstance(value, str) else value
                                        for key, value in fields.items()}))
        return entry_id

    async def xgroup_create(self, *_args, **_kwargs):
        pass

    async def xreadgroup(self, group, consumer, streams, count, block=None):
        self.reads += 1
        if self.reads > self.max_reads:
            raise AssertionError(f"ingest re-read the stream {self.reads} times; it is stalled")
        (stream, position), = streams.items()
        assert (stream, group, consumer) == (RAW, ingest_main.GROUP, ingest_main.CONSUMER)
        if position == "0":
            history = [(entry_id, values) for entry_id, values in self.entries if entry_id in self.pending][:count]
            for entry_id, _values in history:
                self.pending[entry_id] += 1
            return [[stream.encode(), history]]
        if self.delivered == len(self.entries):
            if not self.batches:
                raise Drained
            for fields in self.batches.pop(0):
                self._append(fields)
        fresh = self.entries[self.delivered:self.delivered + count]
        self.delivered += len(fresh)
        for entry_id, _values in fresh:
            self.pending[entry_id] = 1
        return [[stream.encode(), fresh]]

    async def xpending_range(self, name, groupname, min, max, count, consumername=None):
        assert min == max and count == 1
        if min not in self.pending:
            return []
        return [{"message_id": min, "consumer": consumername.encode(), "time_since_delivered": 0,
                 "times_delivered": self.pending[min]}]

    async def xack(self, stream, _group, entry_id):
        assert stream == RAW and entry_id in self.pending
        del self.pending[entry_id]
        self.acked.append(entry_id)

    async def xadd(self, stream, fields, maxlen=None, approximate=True):
        self.streams.setdefault(stream, []).append(fields)
        self.maxlen[stream] = maxlen

    async def aclose(self):
        pass

    def published(self) -> list[dict]:
        return [json.loads(fields["json"]) for fields in self.streams.get(ingest_main.EVENTS_STREAM, [])]

    def dead(self) -> list[dict]:
        return self.streams.get(DEAD, [])


def _raw(event) -> dict:
    return {"json": event if isinstance(event, str) else json.dumps(event)}


def _dispatched(load_id: str) -> dict:
    return {"event_id": f"EVT-{load_id}", "type": "load.dispatched", "payload": {"load_id": load_id}}


async def _consume(monkeypatch, redis: StreamRedis) -> StreamRedis:
    db = connect()
    monkeypatch.setattr(ingest_main.redis, "from_url", lambda _url: redis)
    monkeypatch.setattr(ingest_main, "connect", lambda: db)
    monkeypatch.setattr(ingest_main, "RETRY_DELAY_S", 0)
    with pytest.raises(Drained):
        await ingest_main.main()
    return redis


@pytest.fixture(autouse=True)
def mirror_off(monkeypatch):
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    monkeypatch.delenv("INGEST_MAX_ATTEMPTS", raising=False)


async def test_malformed_entries_are_dead_lettered_at_once_and_the_next_entry_is_processed(
        world, monkeypatch, caplog):
    malformed = [
        "[1, 2, 3]",                                                       # a JSON list, not an object
        "{not json",                                                       # undecodable
        {"type": "bank_change.requested", "payload": f"account {ACCOUNT} routing {ROUTING}"},  # non-dict payload
        {"type": "load.tendered", "payload": {"load_id": ["LOAD-1"]}},    # non-string load_id
        {"type": "load.tendered", "payload": {"load_id": "LOAD-2"}, "evidence_refs": 7},  # nested wrong type
    ]
    calls = []
    original = ingest_main.handle

    async def counting_handle(event, db):
        calls.append(event.get("type"))
        return await original(event, db)

    monkeypatch.setattr(ingest_main, "handle", counting_handle)
    caplog.set_level(logging.DEBUG, logger="ingest")
    redis = await _consume(monkeypatch, StreamRedis([_raw(event) for event in malformed],
                                                    [_raw(_dispatched("LOAD-9"))]))

    dead = redis.dead()
    assert [fields["entry_id"] for fields in dead] == ["1-0", "2-0", "3-0", "4-0", "5-0"]
    assert [fields["attempts"] for fields in dead] == ["1"] * 5
    assert {fields["error_type"] for fields in dead} == {"MalformedEntry"}
    assert [fields["reason"] for fields in dead] == [
        "malformed: event is a JSON list, not an object",
        "malformed: json does not decode (JSONDecodeError)",
        "malformed: event payload is not an object",
        "malformed: payload load_id is not a string",
        "malformed: event field has the wrong type (TypeError)",
    ]
    # The original fields travel with the failure so the entry can be inspected or replayed.
    assert dead[0]["raw.json"] == b"[1, 2, 3]" and dead[0]["source_stream"] == RAW
    assert redis.maxlen[DEAD] == ingest_main.DEAD_LETTER_MAXLEN
    # Each malformed entry was tried once, acked, and the entry appended after them was routed.
    assert calls == ["load.tendered", "load.dispatched"]
    assert redis.acked == [f"{index}-0".encode() for index in range(1, 7)] and not redis.pending
    assert [event["event_id"] for event in redis.published()] == ["EVT-LOAD-9"]
    # The partial write of the nested-wrong-type entry was rolled back.
    db = connect()
    try:
        assert ingest_main._load(db, "LOAD-2") is None
    finally:
        db.close()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert sum(record.getMessage().startswith("dead-lettered raw entry") for record in warnings) == 5
    assert "dead-lettered raw entry b'3-0' after 1 attempt(s): malformed: event payload is not an object" in \
        [record.getMessage() for record in warnings]
    assert ACCOUNT not in caplog.text and ROUTING not in caplog.text


@pytest.mark.parametrize("configured, limit", [(None, 3), ("5", 5)])
async def test_transient_failure_is_retried_to_the_limit_then_dead_lettered(world, monkeypatch, configured, limit):
    if configured:
        monkeypatch.setenv("INGEST_MAX_ATTEMPTS", configured)
    attempts = []
    original = ingest_main.handle

    async def locked(event, db):
        if event.get("event_id") == "EVT-LOAD-1":
            attempts.append(event["event_id"])
            raise sqlite3.OperationalError("database is locked")
        return await original(event, db)

    monkeypatch.setattr(ingest_main, "handle", locked)
    redis = await _consume(monkeypatch, StreamRedis([_raw(_dispatched("LOAD-1"))], [_raw(_dispatched("LOAD-2"))]))

    assert len(attempts) == limit
    (dead,) = redis.dead()
    assert dead == {**dead, "entry_id": "1-0", "error_type": "OperationalError", "error": "database is locked",
                    "attempts": str(limit), "reason": f"OperationalError after {limit} attempts"}
    assert redis.acked == [b"1-0", b"2-0"] and not redis.pending
    assert [event["event_id"] for event in redis.published()] == ["EVT-LOAD-2"]


async def test_transient_failure_that_clears_is_published_not_dead_lettered(world, monkeypatch):
    failures = iter([sqlite3.OperationalError("database is locked")] * 2)
    original = ingest_main.handle

    async def flaky(event, db):
        if (failure := next(failures, None)) is not None:
            raise failure
        return await original(event, db)

    monkeypatch.setattr(ingest_main, "handle", flaky)
    redis = await _consume(monkeypatch, StreamRedis([_raw(_dispatched("LOAD-1"))]))
    assert not redis.dead() and redis.acked == [b"1-0"]
    assert [event["event_id"] for event in redis.published()] == ["EVT-LOAD-1"]


async def test_attempts_count_deliveries_from_before_a_restart(world, monkeypatch):
    """XPENDING's delivery count survives a restart, so a fresh process does not grant a new set of retries."""
    redis = StreamRedis()
    entry_id = redis._append(_raw(_dispatched("LOAD-1")))
    redis.delivered, redis.pending[entry_id] = 1, 2  # read by `>` and once from history before the restart

    async def locked(_event, _db):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ingest_main, "handle", locked)
    await _consume(monkeypatch, redis)
    assert [fields["attempts"] for fields in redis.dead()] == ["3"]  # one more delivery reached the limit


async def test_good_entries_in_the_same_batch_as_bad_ones_are_processed(world, monkeypatch):
    original = ingest_main.handle

    async def locked(event, db):
        if event.get("event_id") == "EVT-LOAD-2":
            raise sqlite3.OperationalError("database is locked")
        return await original(event, db)

    monkeypatch.setattr(ingest_main, "handle", locked)
    redis = StreamRedis([_raw("[]"), _raw(_dispatched("LOAD-1")), _raw(_dispatched("LOAD-2")),
                         _raw(_dispatched("LOAD-3"))])
    reads_at_publish = []
    xadd = redis.xadd

    async def tracking_xadd(stream, fields, **kwargs):
        if stream == ingest_main.EVENTS_STREAM:
            reads_at_publish.append(redis.reads)
        await xadd(stream, fields, **kwargs)

    redis.xadd = tracking_xadd
    await _consume(monkeypatch, redis)

    assert [event["event_id"] for event in redis.published()] == ["EVT-LOAD-1", "EVT-LOAD-3"]
    # Both good entries were published from the first `>` read (read 2), before the failing one was retried.
    assert reads_at_publish == [2, 2]
    assert [(fields["entry_id"], fields["error_type"]) for fields in redis.dead()] == \
        [("1-0", "MalformedEntry"), ("3-0", "OperationalError")]
    assert sorted(redis.acked) == [b"1-0", b"2-0", b"3-0", b"4-0"]


class DownSpacetime(FakeSpacetime):
    """SpacetimeDB rejects every invoice write, like a server that stays down for the whole retry window."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/upsert_invoice"):
            return httpx.Response(503)
        return super().handler(request)


async def test_mirror_write_failure_is_retried_then_dead_lettered_and_never_routed(world, monkeypatch):
    _gen, load, events = _run(world, "clean")
    invoice_mail = next(event["event_id"] for event in events if event["type"] == "email.received"
                        and any(str(name).startswith("INV-") for name in event["payload"]["attachments"]))
    spacetime = DownSpacetime()
    mirror = spacetime.mirror()
    monkeypatch.setattr(ingest_main.spacetime_mirror, "Mirror", lambda: mirror)

    async def no_directory_sync(_mirror):
        pass

    monkeypatch.setattr(spacetime_mirror, "sync_directory", no_directory_sync)
    redis = await _consume(monkeypatch, StreamRedis([_raw(event) for event in events]))

    raw_ids = {json.loads(values[b"json"])["event_id"]: entry_id for entry_id, values in redis.entries}
    (dead,) = redis.dead()
    assert dead["entry_id"] == raw_ids[invoice_mail].decode() and dead["attempts"] == "3"
    assert dead["error_type"].startswith("Spacetime")
    # Mirror before publish: the case the mirror could not write never reached the router.
    published = redis.published()
    assert not any(event["type"] == "carrier_invoice.received" for event in published)
    # Everything after the invoice email was still ingested.
    assert len(redis.acked) == len(events) and not redis.pending
    assert spacetime.writes("open_case") and load["case_id"]
