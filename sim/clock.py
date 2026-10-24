"""Shared business clock for the simulation. Business time runs SIM_TIME_RATIO times faster than wall time
(default 600: one wall second = ten business minutes), so a 20 h transit plays out in ~2 minutes and every
document, GPS ping and bank line carries a consistent business timestamp. The anchor lives in Redis so the
simulator and gateway agree; if the simulator was down the clock resumes from where it stopped instead of
jumping ahead."""
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

KEY = "sim:clock"
STALE_S = 30


def ratio() -> float:
    return float(os.getenv("SIM_TIME_RATIO", "600"))


@dataclass
class Clock:
    wall: float
    biz: float
    ratio: float

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.biz + (time.time() - self.wall) * self.ratio, UTC)

    def wall_delay(self, business: timedelta) -> float:
        return business.total_seconds() / self.ratio

    def dump(self) -> str:
        return json.dumps({"wall": self.wall, "biz": self.biz, "ratio": self.ratio})


def fresh() -> Clock:
    return Clock(time.time(), time.time(), ratio())


async def load(r, heartbeat: bool = False) -> Clock:
    """Current clock from Redis. With heartbeat=True (the simulator), re-anchor a stale clock so it resumes
    where it stopped, and persist."""
    raw = await r.get(KEY)
    c = Clock(**json.loads(raw)) if raw else fresh()
    if heartbeat:
        if not raw or time.time() - c.wall > STALE_S:
            c = Clock(time.time(), c.biz, ratio())
        else:
            c = Clock(time.time(), c.now().timestamp(), c.ratio)
        await r.set(KEY, c.dump())
    return c
