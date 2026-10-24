"""W4: continuous synthetic event stream -> Redis `events`. Stub emits the hero invoice once."""
import asyncio
import json
import os
import pathlib
import uuid
from datetime import UTC, datetime

import redis.asyncio as redis

HERO = json.loads((pathlib.Path(__file__).parent / "fixtures/hero_case.json").read_text())


async def main() -> None:
    r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    ev = {"event_id": f"EVT-{uuid.uuid4().hex[:8]}", "type": "carrier_invoice.received",
          "ts": datetime.now(UTC).isoformat(), "case_hint": HERO["load_id"], "priority": "P0",
          "payload": HERO, "source": "sim"}
    await r.xadd("events", {"json": json.dumps(ev)})
    print("published", ev["event_id"])


if __name__ == "__main__":
    asyncio.run(main())
