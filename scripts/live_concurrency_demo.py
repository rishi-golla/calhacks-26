"""Run nine live A2A agents concurrently against explicitly synthetic source records.

Uses real OpenAI inference, authenticated A2A 1.0, PostgreSQL event persistence,
and SpacetimeDB projections. No outreach, payment proposal, hold, or journal is
requested. Services must already be running. See ``--help`` for repeatable runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "sim/fixtures/live_concurrency_source.json"
CASE_ID = "SYN-A2A-CONCURRENCY"
SKILLS = {
    "carrier_ap": "answer_payment_status",
    "shipper_ar": "chase_overdue",
    "cfo": "assess_financial_impact",
    "risk": "screen_counterparty",
    "controller": "check_already_paid",
    "verification": "verify_dispute",
    "voice_desk": "handle_inbound_call",
    "validator": "review_output",
    "documents": "check_document_packet",
}
FORBIDDEN_TABLES = ("payment_proposal", "hold", "ledger_entry", "journal_request", "call")


def source_record() -> dict:
    dataset = json.loads(SOURCE.read_text())
    if dataset.get("synthetic") is not True:
        raise ValueError("concurrency workload requires explicitly synthetic source data")
    return dataset["cases"][CASE_ID]


def seed(admin) -> None:
    """Only source case metadata is seeded; every task/result comes from an agent."""
    source = source_record()
    admin.call("ingest", "open_case", {
        "write_id": "live-concurrency-source:v1", "case_id": CASE_ID,
        "load_id": source["load_id"], "title": source["title"],
        "priority": "P2", "exposure_usd": 0,
    })


def peak_concurrency(intervals: list[dict]) -> dict:
    """Compute distinct active agents, treating intervals as [start, end)."""
    events = []
    for interval in intervals:
        if interval["end"] > interval["start"]:
            events.extend(((interval["start"], 1, interval["agent"]),
                           (interval["end"], -1, interval["agent"])))
    active: dict[str, int] = {}
    peak, at, agents = 0, None, []
    for timestamp, change, agent in sorted(events):
        active[agent] = active.get(agent, 0) + change
        current = sorted(key for key, count in active.items() if count > 0)
        if len(current) > peak:
            peak, at, agents = len(current), timestamp, current
    return {"distinct_agents": peak, "at": at, "agents": agents}


def sdk_intervals(rows: list[dict], agent_by_task: dict[str, str]) -> list[dict]:
    """Derive working windows from durable SDK transitions, excluding parked time."""
    from a2a.types import StreamResponse, TaskState
    starts, intervals = {}, []
    for row in rows:
        event = StreamResponse.FromString(bytes(row["event_data"]))
        if not event.HasField("status_update"):
            continue
        status = event.status_update.status
        if not status.HasField("timestamp"):
            continue
        stamp = status.timestamp.ToDatetime(tzinfo=UTC).timestamp()
        if status.state == TaskState.TASK_STATE_WORKING:
            starts.setdefault(row["task_id"], stamp)
        elif row["task_id"] in starts:
            # INPUT_REQUIRED is a parked parent, not an occupied worker. Close
            # its working segment and open a new one only on resumed WORKING.
            task_id = row["task_id"]
            start = starts.pop(task_id)
            if task_id in agent_by_task:
                intervals.append({"task_id": task_id, "agent": agent_by_task[task_id],
                                  "start": start, "end": stamp})
    return intervals


async def run_task(agent: str, skill: str, run_id: str, *, subject: dict | None = None,
                   phase: str = "concurrent", submitted: dict[str, str] | None = None):
    from a2a.types import TaskState

    from common.a2a_transport import get_task, send_task
    from common.envelope import DelegationMeta, TaskEnvelope

    task = await send_task(agent, TaskEnvelope(
        task_type=skill, case_id=CASE_ID, subject=subject or {}, deadline_s=180,
        display_id=f"{run_id} · {agent}",
    ), DelegationMeta(case_id=CASE_ID, from_agent="router",
                     idempotency_key=f"live-concurrency:{run_id}:{phase}:{agent}"))
    if submitted is not None:
        submitted[agent] = task.id
    deadline = time.monotonic() + 210
    while time.monotonic() < deadline:
        if task.status.state == TaskState.TASK_STATE_COMPLETED:
            return task
        if task.status.state in {TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_CANCELED,
                                 TaskState.TASK_STATE_REJECTED}:
            raise RuntimeError(f"{agent} task {task.id}: {TaskState.Name(task.status.state)}")
        # A parent parks as INPUT_REQUIRED while its child is running. The
        # runtime resumes that same durable task; observation must keep polling.
        await asyncio.sleep(0.3)
        task = await get_task(agent, task.id, sender="router")
    raise TimeoutError(f"{agent} task {task.id} did not complete before the acceptance deadline")


async def wait_projection(admin, task_id: str) -> None:
    for _ in range(60):
        tasks = await asyncio.to_thread(admin.rows, "task")
        if any(row["task_id"] == task_id and row["a2a_state"] == "completed" for row in tasks):
            return
        await asyncio.sleep(0.2)
    raise RuntimeError("completed task is not yet projected to SpacetimeDB")


def side_effect_counts(admin) -> dict:
    return {table: sum(row.get("case_id") == CASE_ID for row in admin.rows(table))
            for table in FORBIDDEN_TABLES}


def tool_activity(audits: list[dict], agent_by_task: dict[str, str]) -> dict:
    """Count paired model tool events from this run, without copying sensitive results."""
    calls: dict[tuple[str, str], dict] = {}
    for row in audits:
        task_id = row.get("task_id")
        if row.get("kind") != "tool_call" or task_id not in agent_by_task:
            continue
        try:
            event = json.loads(row.get("summary", ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("version") != 1:
            continue
        call_id, name = event.get("tool_call_id"), event.get("tool_name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            continue
        key = (task_id, call_id)
        call = calls.setdefault(key, {"agent": agent_by_task[task_id], "task_id": task_id,
                                     "tool_call_id": call_id, "tool_name": name, "statuses": set()})
        status = event.get("status")
        if isinstance(status, str):
            call["statuses"].add(status)
    completed = [call for call in calls.values() if {"started", "completed"} <= call["statuses"]]
    return {"completed_calls": len(completed),
            "agents_with_completed_calls": sorted({call["agent"] for call in completed}),
            "calls": [{**call, "statuses": sorted(call["statuses"])} for call in calls.values()]}


async def run(admin, run_id: str) -> dict:
    import httpx
    from sqlalchemy import bindparam, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from common.config import AGENTS
    from common.runtime import extract_artifact_data
    from common.runtime_store import runtime_url

    if os.getenv("FREIGHTOPS_MODE") != "live" or os.getenv("SPACETIME_DISABLED") != "0":
        raise ValueError("live concurrency requires FREIGHTOPS_MODE=live and enabled SpacetimeDB")
    configured_source = Path(os.environ.get("SOURCE_DATA_PATH", "")).resolve()
    source_dataset = json.loads(configured_source.read_text())
    if source_dataset.get("synthetic") is not True or source_dataset.get("cases", {}).get(CASE_ID) != source_record():
        raise ValueError("configured source must preserve the dedicated synthetic concurrency case unchanged")
    async with httpx.AsyncClient(timeout=10) as client:
        ready = await asyncio.gather(*(client.get(f"{spec.url}/readyz") for spec in AGENTS.values()))
    if any(response.status_code != 200 for response in ready):
        raise RuntimeError("all nine services must pass live readiness before this workload")
    before = await asyncio.to_thread(side_effect_counts, admin)
    # The reviewer consumes an actual, persisted result, never a fabricated target.
    warmup = await run_task("cfo", "assess_financial_impact", run_id, phase="review-target")
    await wait_projection(admin, warmup.id)
    print(f"Review target ready: {warmup.id}. Dispatching all nine agents concurrently.", flush=True)
    submitted: dict[str, str] = {}
    outcomes = await asyncio.gather(*(run_task(agent, skill, run_id, submitted=submitted,
        subject={"target_task_id": warmup.id} if agent == "validator" else None)
        for agent, skill in SKILLS.items()), return_exceptions=True)
    completed, failures = {}, {}
    for agent, outcome in zip(SKILLS, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            failures[agent] = str(outcome)[:500]
        else:
            completed[agent] = outcome
    for task in completed.values():
        await wait_projection(admin, task.id)
    # Failed roots still ran. Include every admitted ID in observed intervals
    # and handoffs; completion is a separate acceptance requirement below.
    agent_by_task = {task_id: agent for agent, task_id in submitted.items()}
    intervals = []
    if agent_by_task:
        engine = create_async_engine(runtime_url())
        try:
            async with engine.connect() as conn:
                statement = text("SELECT task_id, event_data FROM task_events "
                    "WHERE task_id IN :ids ORDER BY seq").bindparams(bindparam("ids", expanding=True))
                rows = (await conn.execute(statement, {"ids": list(agent_by_task)})).mappings().all()
            intervals = sdk_intervals([dict(row) for row in rows], agent_by_task)
        finally:
            await engine.dispose()
    models, summary_errors = {}, {}
    for agent, task in completed.items():
        artifacts = extract_artifact_data(task)
        models[agent] = artifacts[-1].get("model_used", "") if artifacts else ""
        if artifacts:
            error = artifacts[-1].get("findings", {}).get("tool_loop", {}).get("assessment_error")
            if error:
                summary_errors[agent] = error
    edges = await asyncio.to_thread(admin.rows, "delegation_edge")
    handoffs = [{key: edge[key] for key in ("from_agent", "to_agent", "parent_task_id", "task_id", "skill")}
               for edge in edges if edge.get("parent_task_id") in agent_by_task]
    after = await asyncio.to_thread(side_effect_counts, admin)
    tool_events = tool_activity(await asyncio.to_thread(admin.rows, "audit_event"), agent_by_task)
    peak = peak_concurrency(intervals)
    observed_tasks = {interval["task_id"] for interval in intervals}
    passed = (len(completed) == 9 and len(observed_tasks) == 9 and peak["distinct_agents"] == 9
              and all(value.startswith("openai/") for value in models.values())
              and set(tool_events["agents_with_completed_calls"]) == set(SKILLS)
              and len(handoffs) >= 2 and after == before and not failures and not summary_errors)
    return {"passed": passed, "run_id": run_id, "case_id": CASE_ID,
            "captured_at": datetime.now(UTC).isoformat(), "source_synthetic": True,
            "execution": "live", "transport": "authenticated A2A 1.0 JSON-RPC",
            "task_storage": "PostgreSQL", "activity_storage": "SpacetimeDB",
            "warmup_task_id": warmup.id, "completed_agents": sorted(completed),
            "submitted_tasks": submitted,
            "models": models, "failures": failures, "summary_errors": summary_errors,
            "root_execution_intervals": intervals,
            "peak_concurrency": peak, "handoffs": handoffs,
            "tool_activity": tool_events,
            "restricted_side_effects_before": before, "restricted_side_effects_after": after}


def main() -> None:
    from scripts.local_env import load_live_env

    load_live_env()
    from scripts.spacetime_admin import LocalAdmin

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.getenv("SPACETIME_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--database", default=os.getenv("SPACETIME_DB", "freightops-a2a-live"))
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:12],
                        help="Reuse only to inspect the same idempotent run; new ID executes new model calls.")
    parser.add_argument("--seed-only", action="store_true", help="Seed only the labeled source case; no agent calls.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.run_id):
        parser.error("run ID must be 1..64 alphanumeric, underscore, or hyphen characters")
    if args.database != "freightops-a2a-live":
        parser.error("this synthetic workload only targets the dedicated freightops-a2a-live database")
    admin = LocalAdmin(args.server, args.database)
    try:
        seed(admin)
        if args.seed_only:
            print(f"Seeded {CASE_ID}; no task results, calls, or financial actions were fabricated.")
            return
        report = asyncio.run(run(admin, args.run_id))
    finally:
        admin.client.close()
    output = args.output or ROOT / "data/live" / f"acceptance-{args.run_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"Acceptance report: {output}")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
