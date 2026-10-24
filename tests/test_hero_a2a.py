"""Boots all 8 agents as real processes and runs the hero case end to end over A2A:
AP -> Risk -> Verification -> Voice Desk (mock call, parks) -> resume chain -> Controller -> Validator."""
import asyncio
import os
import subprocess
import sys
import uuid

import httpx
import pytest

from common import a2a_client, store
from common.config import AGENTS
from common.parking import resume_parked
from router.main import dispatch
from scripts.hero_smoke import HERO


@pytest.fixture(scope="module")
def mesh(tmp_path_factory):
    d = tmp_path_factory.mktemp("mesh")
    env = {**os.environ, "PYTHONPATH": os.getcwd(), "OPS_DB": str(d / "ops.db"), "TASK_DB_DIR": str(d),
           "VOICE_MOCK_DELAY_S": "0.3", "HARNESS_URL": "http://127.0.0.1:1", "MOCK_INTEGRATIONS": "1",
           "DEMO_ALLOWLIST_PHONES": "+15555550100", "DEMO_ALLOWLIST_EMAILS": "team@example.com"}
    os.environ.update({k: env[k] for k in ("OPS_DB", "DEMO_ALLOWLIST_PHONES", "DEMO_ALLOWLIST_EMAILS")})
    procs = [subprocess.Popen([sys.executable, "-m", f"agents.{a}.main"], env=env) for a in AGENTS]
    yield
    for p in procs:
        p.terminate()
        p.wait(5)


async def _ready():
    async with httpx.AsyncClient() as c:
        for a in AGENTS.values():
            for _ in range(100):
                try:
                    if (await c.get(f"{a.url}/.well-known/agent-card.json")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.2)


async def _run(overrides: dict) -> tuple[str, str]:
    await _ready()
    run = uuid.uuid4().hex[:4]
    case_id = f"CASE-T-{run}"
    ev = {"event_id": f"EVT-{run}", "type": "carrier_invoice.received", "priority": "P0",
          "payload": {**HERO, "case_id": case_id, "invoice_id": f"INV-{run}", **overrides}}
    return case_id, await dispatch(ev)


async def _wait_done(task_id: str, timeout: float = 30):
    for _ in range(int(timeout / 0.25)):
        t = await a2a_client.get_task("carrier_ap", task_id)
        if a2a_client.state_name(t) in ("completed", "failed"):
            return t
        await asyncio.sleep(0.25)
    raise AssertionError(f"AP task stuck in {a2a_client.state_name(t)}")


async def test_hero_chain_call_short_pays_detention(mesh):
    case_id, task_id = await _run({})
    final = a2a_client.artifacts(await _wait_done(task_id))[-1]
    assert final["outcome"] == "short_pay"
    assert final["findings"]["approved_amount"] == 1850.0
    assert final["findings"]["disputed_amount"] == 300.0
    ver = final["findings"]["risk"]["findings"]["verification"]["findings"]
    assert ver["claim_confirmed"] is False
    assert ver["call"]["routed_to"] == "+15555550100"
    assert "P-001" in final["policy_ids"]
    actions = [cs["action"] for cs in store.change_sets(case_id)]
    assert {"record_invoice", "place_hold", "propose_short_pay", "post_journal_entry", "finalize_invoice"} <= set(actions)
    assert store.verify_chain()
    for _ in range(40):
        if store.reviews(case_id):
            break
        await asyncio.sleep(0.25)
    rv = store.reviews(case_id)
    assert rv and rv[-1]["verdict"] == "pass"
    assert store.memories("risk")


async def test_email_channel_and_human_approval(mesh):
    case_id, task_id = await _run({"verification_channel": "email", "force_human_approval": True})
    appr = None
    for _ in range(80):
        appr = next((p for p in store.list_parked("approval") if p["case_id"] == case_id), None)
        if appr:
            break
        await asyncio.sleep(0.25)
    assert appr, "controller never asked for approval"
    assert await resume_parked(appr["key"], {"kind": "approval", "decision": "approve", "approver": "alice"})
    final = a2a_client.artifacts(await _wait_done(task_id))[-1]
    assert final["outcome"] == "short_pay"
    assert final["findings"]["controller"]["findings"]["approver"] == "alice"
    assert await resume_parked(appr["key"], {"decision": "approve"}) is None  # duplicate webhook ignored
