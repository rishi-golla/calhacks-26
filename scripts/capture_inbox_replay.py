"""Capture an explicitly simulated inbox replay from nine real A2A services.

Run from the repository root: ``python -m scripts.capture_inbox_replay``.
No provider key, phone call, W2 write or live source is used. The output contains
recorded SDK status timestamps, durable delegation links and actual artifacts;
it never invents a conversation or claims the fixture was live work.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from a2a.types import StreamResponse, TaskState
from google.protobuf.json_format import MessageToDict

from common.a2a_transport import get_task, send_task
from common.config import AGENTS
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import extract_artifact_data

ROOT = Path(__file__).resolve().parents[1]
SECRET_FIELDS = {"phone", "email", "intended_to", "routed_to", "call_token", "api_key", "token"}


def sanitize(value):
    """Keep the replay public: contact targets and capabilities are not UI content."""
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items() if key.lower() not in SECRET_FIELDS}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def services(directory: Path):
    source = ROOT / "sim/fixtures/brokerage_source.json"
    if not json.loads(source.read_text()).get("fixture"):
        raise ValueError("capture requires an explicitly marked fixture dataset")
    settings = {
        "FREIGHTOPS_MODE": "fixture", "SPACETIME_DISABLED": "1", "OUTREACH_MODE": "demo",
        "SOURCE_DATA_PATH": str(source), "A2A_DATABASE_URL": f"sqlite+aiosqlite:///{directory / 'runtime.sqlite3'}",
        "DEMO_ALLOWLIST_PHONES": "+15555550100,+15555550101", "A2A_BIND_HOST": "127.0.0.1",
        "PYTHONPATH": str(ROOT),
    }
    ports = set()
    for name in AGENTS:
        port = free_port()
        while port in ports:
            port = free_port()
        ports.add(port)
        settings[f"AGENT_PORT_{name.upper()}"] = str(port)
        settings[f"AGENT_URL_{name.upper()}"] = f"http://127.0.0.1:{port}"
    previous = {key: os.environ.get(key) for key in settings}
    os.environ.update(settings)
    processes = []
    try:
        for name in AGENTS:
            log = (directory / f"{name}.log").open("w")
            process = subprocess.Popen([sys.executable, "-m", f"agents.{name}.main"],
                                       cwd=ROOT, env=dict(os.environ), stdout=log, stderr=subprocess.STDOUT)
            processes.append((process, log))
        yield
    finally:
        for process, _ in processes:
            process.terminate()
        for process, log in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log.close()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def wait_ready(url: str) -> None:
    async with httpx.AsyncClient(timeout=2) as client:
        for _ in range(100):
            try:
                if (await client.get(f"{url}/readyz")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError(f"fixture service failed to start: {url}")


async def run_task(agent: str, skill: str, case_id: str, *, subject: dict | None = None):
    task = await send_task(agent, TaskEnvelope(task_type=skill, case_id=case_id, subject=subject or {}),
                          DelegationMeta(case_id=case_id, from_agent="router",
                                         idempotency_key=f"inbox-capture:{agent}:{skill}:{case_id}"))
    for _ in range(200):
        if task.status.state == TaskState.TASK_STATE_COMPLETED:
            return task
        if task.status.state in {TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED,
                                 TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_INPUT_REQUIRED}:
            raise RuntimeError(f"capture task did not complete: {agent}/{skill}: {TaskState.Name(task.status.state)}")
        await asyncio.sleep(0.05)
        task = await get_task(agent, task.id, sender="router")
    raise TimeoutError(f"capture timed out: {agent}/{skill}")


async def run_sequence() -> None:
    await asyncio.gather(*(wait_ready(spec.url) for spec in AGENTS.values()))
    # Every result comes from the real handlers over real JSON-RPC transport.
    await run_task("carrier_ap", "process_carrier_invoice", "CASE-4821")
    await run_task("documents", "check_document_packet", "CASE-4821")
    await run_task("shipper_ar", "reconcile_customer_payment", "CASE-SHORT")
    cash_task = await run_task("cfo", "assess_financial_impact", "CASE-SHORT")
    source = await asyncio.to_thread(lambda: json.loads(
        (ROOT / "sim/fixtures/brokerage_source.json").read_text())["cases"]["CASE-SHORT"])
    await run_task("validator", "review_output", "CASE-SHORT", subject={
        "target_task": {"task_id": cash_task.id, "case_id": "CASE-SHORT", "agent": "cfo",
                        "skill": "assess_financial_impact", "a2a_state": "completed"},
        "artifact": extract_artifact_data(cash_task)[-1], "source": source,
    })


def export_capture(path: Path) -> dict:
    """Project persisted fixture records into the read-only dashboard table shape."""
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        executions = [dict(row) for row in db.execute("SELECT * FROM freight_execution")]
        final_steps = {row["task_id"]: json.loads(row["value"]) for row in db.execute(
            "SELECT task_id, value FROM freight_steps WHERE key = '__result'")}
        dependencies = [dict(row) for row in db.execute("SELECT * FROM freight_dependencies")]
        sdk_events = [dict(row) for row in db.execute("SELECT * FROM task_events ORDER BY seq")]
    by_id = {row["id"]: row for row in executions}
    statuses: dict[str, list[dict]] = {}
    audit = []
    for row in sdk_events:
        proto = StreamResponse.FromString(row["event_data"])
        if not proto.HasField("status_update"):
            continue
        event = MessageToDict(proto.status_update, preserving_proto_field_name=True)
        state = event["status"]["state"].removeprefix("TASK_STATE_").lower().replace("_", "-")
        at = event["status"].get("timestamp")
        if not at:
            continue
        business = event.get("metadata", {}).get("business_status", state)
        statuses.setdefault(row["task_id"], []).append({"at": at, "state": state, "business": business})
        execution = by_id[row["task_id"]]
        envelope = json.loads(execution["envelope"])
        audit.append({"id": f"sdk-event:{row['seq']}", "task_id": row["task_id"],
                      "case_id": envelope["case_id"], "agent": execution["agent"], "kind": "a2a_status",
                      "reason_code": "RECORDED_SDK_STATUS", "summary": f"Task {business.replace('_', ' ')}.",
                      "ts": at, "sequence": row["seq"], "evidence_refs": [], "policy_ids": [],
                      "a2a_state": state, "business_status": business, "simulated": True})
    tasks = []
    for row in executions:
        env, meta = json.loads(row["envelope"]), json.loads(row["delegation"])
        history = statuses[row["id"]]
        result = sanitize(final_steps[row["id"]])
        tasks.append({"task_id": row["id"], "case_id": env["case_id"], "agent": row["agent"],
                      "skill": env["task_type"], "parent_task_id": meta.get("parent_task_id", ""),
                      "display_id": f"{row['agent']} · {env['case_id']}",
                      "a2a_state": history[-1]["state"], "business_status": history[-1]["business"],
                      "started_at": history[0]["at"], "updated_at": history[-1]["at"],
                      "outcome": result.get("outcome", ""), "confidence": result.get("confidence", 0),
                      "artifact_json": json.dumps(result, separators=(",", ":")),
                      "model_used": result.get("model_used") or "fixture/deterministic",
                      "evidence_refs": result.get("evidence_refs", []), "policy_ids": result.get("policy_ids", []),
                      "simulated": True})
    task_index = {row["task_id"]: row for row in tasks}
    edges = []
    for row in dependencies:
        parent, child = task_index[row["parent_id"]], task_index[row["child_id"]]
        edges.append({"id": row["id"], "case_id": child["case_id"], "from_agent": parent["agent"],
                      "to_agent": child["agent"], "parent_task_id": parent["task_id"],
                      "task_id": child["task_id"], "skill": child["skill"], "ts": child["started_at"],
                      "simulated": True})
    source = json.loads((ROOT / "sim/fixtures/brokerage_source.json").read_text())
    cases = []
    for case_id in sorted({row["case_id"] for row in tasks}):
        record = source["cases"][case_id]
        case_tasks = [row for row in tasks if row["case_id"] == case_id]
        cases.append({"case_id": case_id, "load_id": record["load_id"],
                      "title": "Invoice exception review" if case_id == "CASE-4821" else "Short-payment reconciliation",
                      "opened_at": min(row["started_at"] for row in case_tasks), "status": "fixture_complete",
                      "exposure_usd": record["invoice_amount"], "priority": "P2", "simulated": True})
    represented = {row["agent"] for row in tasks}
    if represented != set(AGENTS):
        raise AssertionError(f"capture missing services: {set(AGENTS) - represented}")
    if any(row["a2a_state"] != "completed" for row in tasks):
        raise AssertionError("only successful fixture runs can be shipped as the sample capture")
    return {"metadata": {"mode": "fixture", "captured_at": iso(datetime.now(UTC).timestamp()),
            "description": "Recorded run of nine local A2A services. Inference and calls are deterministic fixtures; "
                           "no live provider, phone, financial write or SpacetimeDB activity occurred.",
            "protocol": "A2A 1.0", "sdk_version": "1.2.3", "agent_count": len(represented),
            "service_count": len(represented),
            "task_count": len(tasks), "status_events": len(audit),
            "source": "scripts/capture_inbox_replay.py", "timestamps": "Persisted SDK status timestamps",
            "delegations": "Persisted W3 dependencies; timestamp is the child task's first recorded working status"},
            "tables": {"case": cases, "task": tasks, "delegation_edge": edges,
                       "audit_event": audit, "call": [], "review": [], "agent": []}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "web/lib/agent-inbox-replay.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="freightops-inbox-") as temporary:
        directory = Path(temporary)
        with services(directory):
            asyncio.run(run_sequence())
        capture = export_capture(directory / "runtime.sqlite3")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(capture, indent=2) + "\n")
    print(f"Captured {capture['metadata']['task_count']} tasks across nine A2A services into {args.output}")


if __name__ == "__main__":
    main()
