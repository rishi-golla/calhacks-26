"""Prepare labeled sponsor demos, run public Apify checks, or explicitly request a team call."""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import time
from pathlib import Path

from scripts.local_env import ROOT, load_live_env
from scripts.setup_cartesia import update_env

LOOKUP_CASE = "SYN-SPONSOR-LOOKUPS"
VOICE_CASE = "SYN-CARTESIA-TEAM-CALL"
PUBLIC_SOURCE = "https://safer.fmcsa.dot.gov/query.asp?query_param=USDOT&query_string=80806&query_type=queryCarrierSnapshot"


def prepare() -> Path:
    """Keep the operator's phone in ignored local data, never in the public fixture."""
    from scripts.spacetime_admin import LocalAdmin, save_private
    dataset = json.loads((ROOT / "sim/fixtures/live_concurrency_source.json").read_text())
    base = dataset["cases"]["SYN-A2A-CONCURRENCY"]
    target = os.environ.get("DEMO_ALLOWLIST_PHONES", "").split(",")[0].strip()
    for case_id, carrier_id, title in (
        (LOOKUP_CASE, "PUBLIC-JBHUNT", "[Live synthetic] Apify public carrier and website check"),
    ):
        record = copy.deepcopy(base)
        record.update(case_id=case_id, carrier_id=carrier_id, title=title,
                      load_id=f"{case_id}-LOAD", invoice_id=f"{case_id}-INVOICE")
        record.pop("inbound_call", None)
        record["evidence_refs"] = [f"synthetic:{case_id}:scenario"]
        for document in record["documents"]:
            document["load_id"] = record["load_id"]
        dataset["cases"][case_id] = record
    team = json.loads((ROOT / "sim/fixtures/team_call_source.json").read_text())
    for key in ("cases", "companies", "contacts"):
        dataset.setdefault(key, {}).update(copy.deepcopy(team[key]))
    dataset["companies"]["PUBLIC-JBHUNT"] = {
        "id": "PUBLIC-JBHUNT", "name": "J B HUNT TRANSPORT INC", "usdot": "80806",
        "website": "https://www.jbhunt.com/", "source": "fmcsa", "source_url": PUBLIC_SOURCE,
        "verified": False,
    }
    dataset["companies"]["DEMO-TEAM"]["phone"] = target
    # Preserve prior source artifacts so an already-running demo retains its evidence.
    encoded = json.dumps(dataset, indent=2) + "\n"
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    destination = ROOT / "data/live" / f"sponsor-source-{fingerprint}.json"
    if not destination.exists():
        save_private(destination, encoded)
    elif destination.read_text() != encoded:
        raise ValueError("the immutable sponsor source artifact does not match its fingerprint")
    admin = LocalAdmin(os.environ["SPACETIME_URL"], os.environ["SPACETIME_DB"])
    try:
        for case_id in (LOOKUP_CASE, VOICE_CASE):
            record = dataset["cases"][case_id]
            # The original receipt/case load remains stable across scenario refreshes.
            title = ("[Live synthetic] Cartesia team phone test" if case_id == VOICE_CASE else record["title"])
            admin.call("ingest", "open_case", {"write_id": f"sponsor-case:v1:{case_id}",
                "case_id": case_id, "load_id": record["load_id"], "title": title,
                "priority": "P2", "exposure_usd": 0})
        for company_id in ("PUBLIC-JBHUNT", "DEMO-TEAM"):
            company = dataset["companies"][company_id]
            admin.call("ingest", "upsert_company", {k: v for k, v in company.items()
                       if k in {"id", "name", "phone", "source_url", "website", "usdot"}})
        invoice = dataset["cases"][VOICE_CASE]
        admin.call("ingest", "upsert_invoice", {"write_id": f"sponsor-invoice:gg1042:v1:{invoice['invoice_id']}",
            "id": invoice["invoice_id"], "case_id": VOICE_CASE, "carrier_id": invoice["carrier_id"],
            "load_id": invoice["load_id"], "amount": invoice["invoice_amount"],
            "contract_amount": invoice["contract_rate"], "issued_at": invoice["issued_at"]})
        if target:
            admin.call("admin", "set_demo_target", {"phone": target, "enabled": True,
                       "label": "Operator-provided FreightLine team demo"})
    finally:
        admin.client.close()
    update_env(ROOT / ".env", {"SOURCE_DATA_PATH": str(destination)})
    update_env(ROOT / "data/live/runtime.env", {"SOURCE_DATA_PATH": str(destination)})
    return destination


async def run_task(case_id: str, skill: str, run_id: str, requested_output: str) -> dict:
    from a2a.types import TaskState

    from common.a2a_transport import get_task, send_task
    from common.envelope import DelegationMeta, TaskEnvelope
    from common.runtime import extract_artifact_data

    task = await send_task("verification", TaskEnvelope(task_type=skill, case_id=case_id,
        requested_output=requested_output, deadline_s=300, display_id=f"Sponsor demo · {run_id}"),
        DelegationMeta(case_id=case_id, from_agent="router", idempotency_key=f"sponsor-demo:{case_id}:{run_id}"))
    deadline = time.monotonic() + 360
    print(f"Admitted sponsor task: {task.id}", flush=True)
    while time.monotonic() < deadline:
        if task.status.state == TaskState.TASK_STATE_COMPLETED:
            artifacts = extract_artifact_data(task)
            return {"task_id": task.id, "state": "completed", "artifact": artifacts[-1] if artifacts else {}}
        if task.status.state in {TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_CANCELED,
                                 TaskState.TASK_STATE_REJECTED}:
            return {"task_id": task.id, "state": TaskState.Name(task.status.state)}
        await asyncio.sleep(0.5)
        task = await get_task("verification", task.id, sender="router")
    return {"task_id": task.id, "state": "still_waiting"}


def voice_preflight() -> dict:
    from common.envelope import TaskEnvelope
    from common.outreach import OutreachBlocked, route_target
    from common.source_repository import SourceRepository, SourceUnavailable
    from common.voice import VoiceError, check_call_window, voice_configuration_status

    async def directory():
        repository = SourceRepository()
        source = await repository.load(TaskEnvelope(task_type="verify_claim", case_id=VOICE_CASE))
        company = await repository.get_company("DEMO-TEAM")
        contact = await repository.get_contact("DEMO-TEAM")
        return source, company, contact if contact.get("company_id") else company

    status = voice_configuration_status("verification")
    status.update(directory_ready=False, calling_window_open=False, destination_suffix="")
    try:
        source, company, target = asyncio.run(directory())
        phone = target.get("phone")
        if (source.get("carrier_id") != "DEMO-TEAM" or company.get("verified") is not True
                or target.get("verified") is not True or not target.get("source") or not target.get("source_url")
                or not isinstance(phone, str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone)
                or target.get("company_id") and float(target.get("confidence", 0)) < 0.7):
            raise VoiceError("team demo directory is incomplete")
        routed = route_target("phone", phone)
        if routed.routed_to != phone:
            raise VoiceError("team demo directory does not match the configured destination")
        status.update(directory_ready=True, destination_suffix=phone[-4:])
        try:
            check_call_window({**company, "timezone": target.get("timezone") or company.get("timezone")})
            status["calling_window_open"] = True
        except (VoiceError, ValueError, KeyError):
            pass
    except (SourceUnavailable, VoiceError, OutreachBlocked, ValueError, TypeError):
        status["directory_error"] = "The team demo case needs a verified source phone matching the demo destination."
    status["available"] = status["available"] and status["directory_ready"]
    status["gateway_health"] = "Check http://127.0.0.1:9000/healthz before calling."
    return status


def main():
    load_live_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "lookups", "check-voice", "call"])
    parser.add_argument("--run-id", default="sponsors-001")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
        print("Prepared two labeled cases and private source data. Restart live services before running tasks.")
        return
    if args.action in {"call", "check-voice"}:
        status = voice_preflight()
        print(json.dumps(status, indent=2))
        if args.action == "check-voice":
            return
        if not status["available"] or not status["calling_window_open"]:
            raise SystemExit("Call not submitted: voice configuration or calling hours are not ready.")
    result = asyncio.run(run_task(
        VOICE_CASE if args.action == "call" else LOOKUP_CASE,
        "verify_claim" if args.action == "call" else "verify_dispute", args.run_id,
        "Run one brief team verification call using the supplied scenario, then report the caller's response."
        if args.action == "call" else
        "Use lookup_carrier_authority and inspect_company_website to inspect this case's public carrier "
        "registration and website through Apify. Report sources and limitations. Do not initiate outreach."))
    path = ROOT / "data/live" / f"sponsor-{args.action}-{args.run_id}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    path.chmod(0o600)
    print(json.dumps({"task_id": result["task_id"], "state": result["state"], "report": str(path)}))
    if result["state"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
