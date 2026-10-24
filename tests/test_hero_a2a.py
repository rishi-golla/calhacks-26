"""Boots Carrier AP + Risk as real processes and runs the hero case over A2A JSON-RPC."""
import asyncio
import os
import subprocess
import sys

import httpx
import pytest

from common.config import AGENTS


@pytest.fixture
def agents():
    env = {**os.environ, "PYTHONPATH": os.getcwd()}
    procs = [subprocess.Popen([sys.executable, "-m", f"agents.{a}.main"], env=env) for a in ("carrier_ap", "risk")]
    yield
    for p in procs:
        p.terminate()
        p.wait(5)


async def _wait_ready(url: str) -> None:
    async with httpx.AsyncClient() as c:
        for _ in range(50):
            try:
                if (await c.get(f"{url}/.well-known/agent-card.json")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError(f"{url} not ready")


async def test_hero_case_ap_delegates_to_risk(agents):
    for a in ("carrier_ap", "risk"):
        await _wait_ready(AGENTS[a].url)
    from scripts.hero_smoke import run
    results = await run()
    final = results[-1]
    assert final["outcome"] == "hold"
    assert "P-001" in final["policy_ids"]
    assert final["findings"]["risk"][0]["outcome"] == "suspicious"
