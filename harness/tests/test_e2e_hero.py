"""End-to-end Stage 1 on the hero case (PRD section 10): LOAD-4821 invoice document with personal data ->
redaction -> model A extraction -> 7.3 route -> model C check -> real A2A dispatch to Carrier AP -> hold.
Needs the three vLLM backends (harness/infra/serve_all.sh). Starts AP and Risk if they are not running."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import uuid

import pytest
from a2a.client import ClientConfig, create_client
from a2a.types import GetTaskRequest
from google.protobuf.json_format import MessageToDict

from common.config import AGENTS
from harness.ingest.dispatch import Dispatcher
from harness.ingest.memory import Memory
from harness.ingest.pipeline import process
from harness.ingest.vault import Vault
from harness.pool import Pool

HERO_TEXT = """CARRIER INVOICE  #SH-20614
Sierra Haul LLC (carrier C-311, USDOT 3141592)
Load: LOAD-4821   Lane: Stockton, CA -> Reno, NV   Shipper: S-07 Bay Produce Co

Linehaul (per rate con)              $1,850.00
Detention 2h @ $150/h                  $300.00
TOTAL DUE                            $2,150.00

Driver: Ray Delgado, cell (209) 555-0148
Remit to: billing@sierrahaul-demo.com
ACH: routing number 121000358, account # 7744120098
Mail: 1420 Harbor Way, Stockton, CA 95206
No detention receipt attached.
"""
PII = ["Ray Delgado", "Delgado", "(209) 555-0148", "billing@sierrahaul-demo.com", "121000358", "7744120098",
       "1420 Harbor Way"]


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
async def pool():
    p = Pool()
    await p.check_health()
    in_rotation = {k for chain in p.logical.values() for k in chain}
    down = [b.served_name for k, b in p.backends.items() if k in in_rotation and not b.healthy]
    if down:
        await p.aclose()
        pytest.skip(f"models down: {down}")
    yield p
    await p.aclose()


@pytest.fixture
def agents():
    env = {**os.environ, "PYTHONPATH": os.getcwd()}
    procs = [subprocess.Popen([sys.executable, "-m", f"agents.{a}.main"], env=env)
             for a in ("carrier_ap", "risk") if not _port_open(AGENTS[a].port)]
    for _ in range(50):
        if all(_port_open(AGENTS[a].port) for a in ("carrier_ap", "risk")):
            break
        asyncio.run(asyncio.sleep(0.2))
    yield
    for p in procs:
        p.terminate()
        p.wait(5)


async def _final_task(task_id: str) -> dict:
    client = await create_client(AGENTS["carrier_ap"].url, ClientConfig(streaming=False))
    try:
        for _ in range(100):
            t = MessageToDict(await client.get_task(GetTaskRequest(id=task_id)))
            if t["status"]["state"] in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"):
                return t
            await asyncio.sleep(0.2)
    finally:
        await client.close()
    raise TimeoutError(task_id)


async def test_hero_invoice_end_to_end(pool, agents, tmp_path):
    vault, dispatcher = Vault(tmp_path / "vault.sqlite"), Dispatcher(tmp_path / "ingest.sqlite")
    memory = Memory(tmp_path / "memory.sqlite")
    event = {"event_id": f"EVT-test-{uuid.uuid4().hex[:8]}", "type": "carrier_invoice.received",
             "ts": "2026-06-14T15:02:11Z", "case_hint": "LOAD-4821", "priority": "P0", "source": "sim",
             "payload": {"case_id": "CASE-4821", "load_id": "LOAD-4821",
                         "carrier": {"id": "C-311", "name": "Sierra Haul LLC"},
                         "contract_rate": 1850.0, "invoice_amount": 2150.0,
                         "line_items": [{"desc": "Linehaul", "amount": 1850.0},
                                        {"desc": "Detention 2h", "amount": 300.0}],
                         "detention_hours_claimed": 2, "free_time_min": 120, "gps_dwell_min": 47,
                         "receipt_attached": False, "text": HERO_TEXT}}

    res = await process(pool, vault, dispatcher, event, memory=memory)

    assert res["status"] == "accepted", res
    assert res["case_id"] == "CASE-4821"
    assert res["route"]["skill"] == "process_carrier_invoice" and res["route"]["classifier"] is None
    assert res["extractor"]
    # Either the personal-data audit ran, or (verifier timed out under load) the document is explicitly flagged.
    if res["verifier"]:
        assert res["verifier"]["model_used"]
    else:
        assert res["needs_review"] and any("verifier unavailable" in n for n in res["envelope"]["subject"]["review_notes"])
    env = res["envelope"]
    assert env["priority"] == "P0" and f"event:{event['event_id']}" in env["evidence_refs"]
    s = env["subject"]
    assert s["invoice_amount"] == 2150.0 and s["contract_rate"] == 1850.0
    assert s["load_id"] == "LOAD-4821" and "Sierra Haul LLC" in json.dumps(s)
    blob = json.dumps(res)
    leaked = [p for p in PII if p in blob]
    assert not leaked, f"PII left the box: {leaked}"
    assert vault.detokenize(s["text"]).count("121000358") == 1   # recoverable locally only

    # The agent owns the case from here. Accept any settled business state: completed, or parked waiting on
    # another agent/human (all awaiting_* map to INPUT_REQUIRED). The hero's missing detention receipt makes
    # Carrier AP delegate document collection first.
    task = await _final_task(res["dispatch"]["task_id"])
    state = task["status"]["state"]
    assert state != "TASK_STATE_FAILED", task["status"]
    if state == "TASK_STATE_INPUT_REQUIRED":
        meta = (task["status"].get("message") or {}).get("metadata") or task.get("metadata") or {}
        assert meta["business_status"].startswith("awaiting_"), meta
    else:
        art = task["artifacts"][0]["parts"][0]["data"]
        assert art["outcome"] in ("hold", "partial", "reject"), art
    received = json.dumps(task["history"][0])   # what the agent actually stored from our A2A message
    assert not [p for p in PII if p in received]
    assert task["history"][0]["metadata"]["idempotency_key"] == f"event:{event['event_id']}"

    again = await process(pool, vault, dispatcher, event, memory=memory)
    assert again["status"] == "duplicate" and again["prior"]["task_id"] == res["dispatch"]["task_id"]
