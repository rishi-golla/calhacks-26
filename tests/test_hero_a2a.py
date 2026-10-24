"""All nine independent services; actual JSON-RPC with explicitly simulated evidence/providers."""
import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from a2a.types import TaskState

from common.a2a_transport import get_task, resume_task, send_task
from common.config import AGENTS
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import extract_artifact_data


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture
def agents(tmp_path, monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
    monkeypatch.setenv("SPACETIME_DISABLED", "1")
    source_path = tmp_path / "source.json"
    source_path.write_text(Path("sim/fixtures/brokerage_source.json").read_text())
    monkeypatch.setenv("SOURCE_DATA_PATH", str(source_path))
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", "+15555550100,+15555550101")
    for name in AGENTS:
        port = free_port()
        monkeypatch.setenv(f"AGENT_PORT_{name.upper()}", str(port))
        monkeypatch.setenv(f"AGENT_URL_{name.upper()}", f"http://127.0.0.1:{port}")
    processes = []
    for name in AGENTS:
        env = {**os.environ, "PYTHONPATH": os.getcwd(), "A2A_RUNTIME_DIR": str(tmp_path / name)}
        log = (tmp_path / f"{name}.log").open("w")
        process = subprocess.Popen([sys.executable, "-m", f"agents.{name}.main"], env=env,
                                   stdout=log, stderr=subprocess.STDOUT)
        processes.append((process, log))
    try:
        yield tmp_path
    finally:
        for process, _log in processes:
            process.terminate()
        for process, log in processes:
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
            log.close()


async def _wait_ready(url: str) -> None:
    async with httpx.AsyncClient() as client:
        for _ in range(75):
            try:
                if (await client.get(f"{url}/.well-known/agent-card.json")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError(f"{url} not ready")


async def test_hero_case_ap_delegates_to_risk(agents):
    await asyncio.gather(*(_wait_ready(spec.url) for spec in AGENTS.values()))
    from scripts.hero_smoke import run
    results = await run()
    assert results, "No result; inspect agent logs in " + str(agents)
    final = results[-1]
    assert final["outcome"] == "hold", final
    assert final["findings"]["simulated"] is True
    assert "P-001" in final["policy_ids"]
    risk = final["findings"]["risk"][-1]
    assert risk["outcome"] == "suspicious"
    verification = risk["findings"]["verification"][-1]
    assert verification["findings"]["call_result"]["reached"] is True
    controller = final["findings"]["controller"][-1]
    assert controller["findings"]["hold"]["executed"] is False
    assert controller["findings"]["cash_impact"][-1]["findings"]["invoiced_margin"] == 450
    # Same accepted request returns the original result instead of redoing work.
    replay = await run()
    assert replay[-1] == final
    # A different amount follows the same workflow and needs no investigation.
    clean = await run("CASE-CLEAN")
    assert clean[-1]["outcome"] == "approve"
    assert clean[-1]["findings"]["payment_proposal"]["proposed_payload"]["amount"] == 1275.50


async def await_state(agent, task_id, wanted):
    for _ in range(150):
        task = await get_task(agent, task_id, sender="router")
        if task.status.state == wanted:
            return task
        assert task.status.state not in {TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED}, task
        await asyncio.sleep(0.1)
    raise AssertionError(f"{agent} did not reach {TaskState.Name(wanted)}")


async def test_document_ingestion_resumes_original_task(agents):
    await asyncio.gather(*(_wait_ready(spec.url) for spec in AGENTS.values()))
    source_path = agents / "source.json"
    dataset = json.loads(source_path.read_text())
    source = json.loads(json.dumps(dataset["cases"]["CASE-CLEAN"]))
    source["case_id"] = "CASE-DOCUMENTS"
    pod = source["documents"].pop()
    assert pod["kind"] == "pod"
    dataset["cases"][source["case_id"]] = source
    source_path.write_text(json.dumps(dataset))
    env = TaskEnvelope(task_type="collect_documents", case_id=source["case_id"])
    task = await send_task("documents", env, DelegationMeta(
        case_id=env.case_id, from_agent="router", idempotency_key="missing-documents"))
    parked = await await_state("documents", task.id, TaskState.TASK_STATE_INPUT_REQUIRED)
    assert not parked.artifacts
    # A callback is only a wakeup. The new evidence is read from the trusted source.
    dataset["cases"][env.case_id]["documents"].append(pod)
    source_path.write_text(json.dumps(dataset))
    accepted = await resume_task("documents", task.id, env.case_id, f"documents:{env.case_id}",
                                 {"ingestion_ref": pod["ref"]}, sender="router",
                                 idempotency_key="pod-ingestion-event")
    assert accepted.id == task.id
    complete = await await_state("documents", task.id, TaskState.TASK_STATE_COMPLETED)
    result = extract_artifact_data(complete)[-1]
    assert result["outcome"] == "verified"
    assert result["findings"]["complete"] is True
    assert pod["ref"] in result["evidence_refs"]
    replay = await resume_task("documents", task.id, env.case_id, f"documents:{env.case_id}",
                              {"ingestion_ref": pod["ref"]}, sender="router",
                              idempotency_key="pod-ingestion-event")
    assert extract_artifact_data(replay) == extract_artifact_data(complete)
