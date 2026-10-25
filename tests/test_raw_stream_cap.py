"""Simulator `raw` stream cap: configurable MAXLEN and an early warning before unread arrivals are trimmed."""
import logging
import random
from types import SimpleNamespace

import pytest

from sim import generator


class RecordingRedis:
    """XINFO replies shaped as redis-py returns them: str keys, bytes values."""

    def __init__(self, length: int = 0, groups: list[dict] | None = None, first_id: bytes | None = b"1-0"):
        self.length = length
        self.groups = groups if groups is not None else [{"name": b"ingest", "lag": 0,
                                                          "last-delivered-id": b"1-0"}]
        self.first_id = first_id
        self.xadds: list[dict] = []
        self.checks = 0

    async def xadd(self, stream, fields, **kwargs):
        self.xadds.append({"stream": stream, **kwargs})

    async def xlen(self, stream):
        return self.length

    async def xinfo_groups(self, stream):
        self.checks += 1
        return self.groups

    async def xinfo_stream(self, stream):
        return {"length": self.length, "first-entry": (self.first_id, {}) if self.first_id else None}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RAW_STREAM_MAXLEN", raising=False)
    monkeypatch.setattr(generator, "_last_length_check", 0.0)


async def test_default_cap_is_200k_and_env_overrides_it(monkeypatch):
    r = RecordingRedis()
    await generator.publish(r, {"type": "gps.ping"})
    assert r.xadds[-1] == {"stream": generator.RAW_STREAM, "maxlen": 200_000, "approximate": True}
    monkeypatch.setenv("RAW_STREAM_MAXLEN", "50000")
    await generator.publish(r, {"type": "gps.ping"})
    assert r.xadds[-1]["maxlen"] == 50_000


@pytest.mark.parametrize("value", ["0", "-5", "lots"])
async def test_invalid_cap_is_refused(monkeypatch, value):
    monkeypatch.setenv("RAW_STREAM_MAXLEN", value)
    with pytest.raises(ValueError, match="RAW_STREAM_MAXLEN"):
        await generator.publish(RecordingRedis(), {"type": "gps.ping"})


async def test_invalid_cap_fails_before_the_answer_key_records_a_load(monkeypatch, tmp_path):
    monkeypatch.setenv("RAW_STREAM_MAXLEN", "0")
    load = generator.new_load(generator.offline_world(), random.Random(1))
    with pytest.raises(ValueError):
        await generator.stream_load(RecordingRedis(), load, "clean", random.Random(1))
    with pytest.raises(ValueError):
        await generator.launch(load, "clean", RecordingRedis(), {"injection_id": "INJ-1"})
    assert not (tmp_path / "answer_key.jsonl").exists() and not (tmp_path / "events.jsonl").exists()


def warnings(caplog) -> list[str]:
    return [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]


async def test_warns_on_unread_backlog_at_most_every_few_seconds(monkeypatch, caplog):
    monkeypatch.setenv("RAW_STREAM_MAXLEN", "1000")
    clock = iter([100.0, 101.0, 106.0])
    monkeypatch.setattr(generator, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    # A long stream that ingest has kept up with is fine; only the group's lag matters.
    r = RecordingRedis(length=990, groups=[{"name": b"ingest", "lag": 850, "last-delivered-id": b"5-0"}])
    with caplog.at_level(logging.WARNING, logger="sim"):
        for _ in range(3):
            await generator.publish(r, {"type": "gps.ping"})
    assert r.checks == 2  # the second publish fell inside the check interval
    assert len(warnings(caplog)) == 2 and "ingest is 850 entries behind" in warnings(caplog)[0]


@pytest.mark.parametrize(("redis_state", "expected"), [
    ({"length": 990, "groups": [{"name": b"ingest", "lag": 10, "last-delivered-id": b"9-0"}]}, (10, False)),
    ({"length": 900, "groups": []}, (900, False)),  # ingest has not joined: nothing read
    ({"length": 900, "groups": [{"name": b"other", "lag": 0}]}, (900, False)),
    # Redis < 7 has no lag: compare the group's position with the oldest retained entry.
    ({"length": 900, "groups": [{"name": b"ingest", "lag": None, "last-delivered-id": b"5-3"}],
      "first_id": b"7-0"}, (900, True)),
    ({"length": 900, "groups": [{"name": b"ingest", "lag": None, "last-delivered-id": b"0-0"}],
      "first_id": b"7-0"}, (900, False)),
    ({"length": 900, "groups": [{"name": b"ingest", "lag": None, "last-delivered-id": b"8-0"}],
      "first_id": b"7-0"}, (None, False)),
])
async def test_unread_backlog(redis_state, expected):
    assert await generator.unread_backlog(RecordingRedis(**redis_state)) == expected


async def test_trimmed_unread_entries_are_reported_and_a_failed_check_never_blocks(monkeypatch, caplog):
    monkeypatch.setenv("RAW_STREAM_MAXLEN", "1000")

    class Broken(RecordingRedis):
        async def xinfo_groups(self, stream):
            raise ConnectionError("gone")

    with caplog.at_level(logging.WARNING, logger="sim"):
        await generator.publish(RecordingRedis(length=500), {"type": "gps.ping"})
        assert warnings(caplog) == []
        monkeypatch.setattr(generator, "_last_length_check", 0.0)
        broken = Broken()
        await generator.publish(broken, {"type": "gps.ping"})
        assert broken.xadds and warnings(caplog) == []
        monkeypatch.setattr(generator, "_last_length_check", 0.0)
        await generator.publish(RecordingRedis(length=10, first_id=b"7-0", groups=[
            {"name": b"ingest", "lag": None, "last-delivered-id": b"5-0"}]), {"type": "gps.ping"})
    assert "trimmed entries ingest had not read" in warnings(caplog)[0]
