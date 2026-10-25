"""Simulator `raw` stream cap: configurable MAXLEN and an early warning before unread arrivals are trimmed."""
import logging
from types import SimpleNamespace

import pytest

from sim import generator


class RecordingRedis:
    def __init__(self, length: int = 0):
        self.length = length
        self.xadds: list[dict] = []
        self.xlen_calls = 0

    async def xadd(self, stream, fields, **kwargs):
        self.xadds.append({"stream": stream, **kwargs})

    async def xlen(self, stream):
        self.xlen_calls += 1
        return self.length


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


async def test_warns_near_the_cap_and_checks_length_at_most_every_few_seconds(monkeypatch, caplog):
    monkeypatch.setenv("RAW_STREAM_MAXLEN", "1000")
    clock = iter([100.0, 101.0, 106.0])
    monkeypatch.setattr(generator, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    r = RecordingRedis(length=850)
    with caplog.at_level(logging.WARNING, logger="sim"):
        for _ in range(3):
            await generator.publish(r, {"type": "gps.ping"})
    assert r.xlen_calls == 2  # the second publish fell inside the check interval
    warnings = [rec for rec in caplog.records if "RAW_STREAM_MAXLEN=1000" in rec.getMessage()]
    assert len(warnings) == 2 and "850 entries" in warnings[0].getMessage()


async def test_no_warning_below_threshold_and_a_failed_check_never_blocks_publishing(monkeypatch, caplog):
    monkeypatch.setenv("RAW_STREAM_MAXLEN", "1000")

    class Broken(RecordingRedis):
        async def xlen(self, stream):
            raise ConnectionError("gone")

    with caplog.at_level(logging.WARNING, logger="sim"):
        await generator.publish(RecordingRedis(length=500), {"type": "gps.ping"})
        monkeypatch.setattr(generator, "_last_length_check", 0.0)
        broken = Broken()
        await generator.publish(broken, {"type": "gps.ping"})
    assert broken.xadds and not caplog.records
