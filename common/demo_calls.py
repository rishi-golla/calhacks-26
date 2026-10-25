"""Authenticated team-demo admission and single-use, server-owned hours grants.

No model input grants permission. A grant authorizes exactly the workflow's
reserved Verification -> Voice task, with a verified team destination, for ten
minutes. Directory, policy and routing controls still apply. W2 permits the
explicit team demo to repeat after its prior call ends, with one active call.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid

from sqlalchemy import Column, Float, MetaData, String, Table, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from common import policy
from common.a2a_transport import send_task
from common.envelope import DelegationMeta, TaskEnvelope
from common.outreach import route_target
from common.runtime_store import RuntimeStore, dependencies, digest, task_id_for, wire_form
from common.source_repository import SourceRepository
from common.spacetime_client import SpacetimeClient

CASE_ID = "SYN-CARTESIA-TEAM-CALL"
COMPANY_ID = "DEMO-TEAM"
TEAM_SOURCE = "operator://explicit-team-demo-destination"
GRANT_SECONDS = 600
TERMINAL = {"completed", "failed", "canceled", "cancelled", "rejected"}
metadata = MetaData()
requests = Table("w3_demo_call_requests", metadata,
    Column("request_id", String(36), primary_key=True),
    Column("task_id", String(36), nullable=False, unique=True),
    Column("active_slot", String(16), nullable=True, unique=True),
    Column("case_id", String(36), nullable=False), Column("company_id", String(36), nullable=False),
    Column("destination_hash", String(64), nullable=False), Column("destination_suffix", String(4), nullable=False),
    Column("created_at", Float, nullable=False), Column("expires_at", Float, nullable=False),
    Column("voice_task_id", String(36), nullable=False),
    Column("dispatch_state", String(16), nullable=False), Column("last_send_at", Float, nullable=False))


class DemoCallBlocked(RuntimeError):
    """Safe displayable reason, never a provider error body or destination."""


def canonical_request_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("request_id must be a UUID")
    parsed = str(uuid.UUID(value))
    if value != parsed:
        raise ValueError("request_id must be a canonical UUID")
    return parsed


def enabled() -> bool:
    return (os.getenv("DEMO_PHONE_ENABLED") == "1" and os.getenv("FREIGHTOPS_MODE") == "live"
            and os.getenv("OUTREACH_MODE") == "demo" and bool(os.getenv("DEMO_CONTROL_TOKEN")))


def destination_hash(phone: str) -> str:
    return hashlib.sha256(phone.encode()).hexdigest()


def request_key(request_id: str) -> str:
    return f"team-demo-call:{request_id}"


def root_task_id(request_id: str) -> str:
    return task_id_for("verification", "router", CASE_ID, request_key(request_id))


def child_task_id(root_id: str) -> str:
    return task_id_for("voice_desk", "verification", CASE_ID, f"{root_id}:verified_callback")


def envelope(request_id: str) -> TaskEnvelope:
    return TaskEnvelope(task_type="verify_claim", case_id=CASE_ID, deadline_s=300,
        display_id=f"Team demo call · {request_id[:8]}", requested_output=
        "The operator requested one brief team demonstration phone call. Inspect the configured source "
        "and outreach readiness, then use execute_current_skill to run verify_claim and its Voice Desk "
        "delegation once. Report the call outcome and caller statements as unverified evidence.")


async def verified_destination() -> dict:
    repository = SourceRepository()
    source = await repository.load(TaskEnvelope(task_type="verify_claim", case_id=CASE_ID))
    company = await repository.get_company(COMPANY_ID)
    contact = await repository.get_contact(COMPANY_ID)
    target = contact if contact.get("company_id") else company
    phone = target.get("phone")
    if (source.get("carrier_id") != COMPANY_ID or company.get("verified") is not True
            or company.get("source_url") != TEAM_SOURCE or company.get("phone") != phone
            or target.get("verified") is not True or not target.get("source") or not target.get("source_url")
            or not isinstance(phone, str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone)
            or target.get("company_id") and float(target.get("confidence", 0)) < 0.7
            or route_target("phone", phone).routed_to != phone):
        raise DemoCallBlocked("The verified team directory must match the enabled demo destination.")
    if policy.check("contact_counterparty", "voice_desk", {"directory_verified": True}).result != "allow":
        raise DemoCallBlocked("Team calling is blocked by policy.")
    return {"phone": phone, "contact_id": target.get("id", "") if target.get("company_id") else ""}


async def check_ledger(destination: dict) -> None:
    """Read-only preflight; W2 repeats its checks atomically when the call is queued."""
    client = SpacetimeClient(actor="voice_desk")
    company_rows, target_rows, call_rows, case_rows, config_rows = await asyncio.gather(
        client.query_rows(f"SELECT * FROM company WHERE id = '{COMPANY_ID}'"),
        client.query_rows(f"SELECT * FROM demo_target WHERE phone = '{destination['phone']}'"),
        client.query_rows(f"SELECT * FROM call WHERE company_id = '{COMPANY_ID}'"),
        client.query_rows(f"SELECT * FROM \"case\" WHERE case_id = '{CASE_ID}'"),
        client.query_rows("SELECT * FROM agent_config"))
    if (not company_rows or company_rows[0].get("source_url") != TEAM_SOURCE
            or company_rows[0].get("phone") != destination["phone"]):
        raise DemoCallBlocked("The team directory is not configured in the live database.")
    if not target_rows or target_rows[0].get("enabled") is not True:
        raise DemoCallBlocked("The team destination is not enabled in the live database.")
    if not case_rows or case_rows[0].get("status") == "human_owned":
        raise DemoCallBlocked("The team demo case is unavailable for agent work.")
    if any(row.get("agent") in {"verification", "voice_desk"} and row.get("enabled") is False for row in config_rows):
        raise DemoCallBlocked("A required team calling agent is disabled.")
    if destination["contact_id"]:
        contact_id = destination["contact_id"].replace("'", "''")
        contacts = await client.query_rows(f"SELECT * FROM contact WHERE id = '{contact_id}'")
        target = contacts[0] if contacts else {}
        if (target.get("company_id") != COMPANY_ID or float(target.get("confidence", 0)) < 0.7
                or not target.get("source_url")):
            raise DemoCallBlocked("The team contact is not verified in the live database.")
    else:
        target = company_rows[0]
    if target.get("phone") != destination["phone"]:
        raise DemoCallBlocked("The source directory and live database team destinations differ.")
    for call in call_rows:
        if call.get("status") in {"queued", "ringing", "in_progress"}:
            raise DemoCallBlocked("An existing team call is still pending. Reconcile it before starting another.")


async def readiness() -> tuple[dict, dict | None]:
    from common.voice import voice_configuration_status
    result = {"enabled": enabled(), "ready": False, "reason": "Team demo calling is disabled.",
              "destination_suffix": "", "hours_override": True, "case_id": CASE_ID}
    if not result["enabled"]:
        return result, None
    if not voice_configuration_status("verification")["available"] or not os.getenv("A2A_TOKEN_ROUTER"):
        result["reason"] = "Cartesia or agent service configuration is incomplete."
        return result, None
    try:
        destination = await verified_destination()
        result["destination_suffix"] = destination["phone"][-4:]
        await check_ledger(destination)
    except DemoCallBlocked as exc:
        result["reason"] = str(exc)
        return result, None
    except Exception:  # noqa: BLE001 -- preflight failures never disclose private data
        result["reason"] = "The live team directory or database is unavailable."
        return result, None
    result.update(ready=True, reason="Ready for one team demo call, including outside normal calling hours.")
    return result, destination


class DemoCallStore:
    def __init__(self, runtime: RuntimeStore | None = None):
        self.runtime = runtime or RuntimeStore()
        self.engine = self.runtime.engine

    async def initialize(self):
        from common.voice import metadata as voice_metadata
        await self.runtime.initialize()
        async with self.runtime.schema_lock(), self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.run_sync(voice_metadata.create_all)

    async def get(self, request_id: str) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(requests).where(requests.c.request_id == request_id))).mappings().first()
        return dict(row) if row else None

    async def latest(self, *, active: bool = False) -> dict | None:
        query = select(requests)
        if active:
            query = query.where(requests.c.active_slot == "team")
        async with self.engine.connect() as conn:
            row = (await conn.execute(query.order_by(requests.c.created_at.desc()).limit(1))).mappings().first()
        return dict(row) if row else None

    async def state(self, row: dict) -> tuple[dict, bool]:
        from common.voice import attempts
        root = await self.runtime.get(row["task_id"])
        child = await self.runtime.get(child_task_id(row["task_id"]))
        async with self.engine.connect() as conn:
            call = (await conn.execute(select(attempts).where(
                attempts.c.task_id == child_task_id(row["task_id"])))).mappings().first()
        state = root["status"] if root else "uncertain" if row["dispatch_state"] == "uncertain" else "queued"
        state = state if state in TERMINAL | {"queued", "working", "waiting", "uncertain"} else "working"
        message = "The agent task is processing this call request."
        live_call = bool(call and call["status"] in {"dialing", "ringing", "uncertain"})
        active_execution = any(task and task["status"] not in TERMINAL for task in (root, child))
        finished = bool(root and root["status"] in TERMINAL and not active_execution and not live_call)
        unused_expired = not row["voice_task_id"] and not call and row["expires_at"] <= time.time()
        # An accepted root that no longer exists was removed by a runtime reset; nothing can resume
        # it, and authorize_voice() refuses a missing root, so only a live provider call keeps the slot.
        orphaned = row["dispatch_state"] == "accepted" and not root and not child and not live_call
        if unused_expired:
            state, message, finished = "failed", "The unused call authorization expired. Start a new request.", True
        if call:
            if call["status"] in {"dialing", "uncertain"}:
                state, message = "uncertain", "The provider may have accepted the call. Do not start another call."
            elif call["status"] == "ringing":
                state, message = "working", "The call was accepted by the provider and is ringing or in progress."
            elif call["status"] in {"completed", "no_answer", "failed", "cancelled"}:
                message = {"completed": "The conversation finished; agents are processing the outcome.",
                           "no_answer": "The call ended without reaching the team contact.",
                           "failed": "The call failed.", "cancelled": "The call was canceled."}[call["status"]]
        elif finished and not unused_expired:
            message = "The agent task ended without starting a phone call."
        if orphaned:
            finished = True
            state = "completed" if call and call["status"] == "completed" else "failed"
            if state == "completed":
                message = "The conversation finished; this request's agent task is no longer available."
            elif not call:
                message = "The agent task for this request is no longer available. Start a new request."
        if finished and state not in TERMINAL:
            state = "completed" if root and root["status"] == "completed" else "failed"
        if not finished and active_execution and state in TERMINAL:
            state, message = "waiting", "A delegated agent is still finishing this request."
        public = {"request_id": row["request_id"], "task_id": row["task_id"], "state": state, "message": message}
        public["retryable"] = bool(not orphaned and not root and not child and not call and row["active_slot"] == "team"
            and row["expires_at"] > time.time()
            and (row["dispatch_state"] in {"prepared", "uncertain"} or row["last_send_at"] < time.time() - 30))
        if call:
            public["call_status"] = call["status"]
        return public, finished

    async def snapshot(self, request_id: str | None = None) -> dict:
        result, _ = await readiness()
        active = await self.latest(active=True)
        row = await self.get(request_id) if request_id else active or await self.latest()
        if row:
            result["request"], _ = await self.state(row)
            result["request"]["retryable"] = result["request"]["retryable"] and result["ready"]
            result["destination_suffix"] = row["destination_suffix"]
        if active and not (await self.state(active))[1]:
            result.update(ready=False, reason="A team call request is already pending. Resume the same request if needed.")
        return result

    async def submit(self, request_id: str) -> tuple[dict, int]:
        request_id = canonical_request_id(request_id)
        if not enabled():
            return await self.snapshot(request_id), 503
        row = await self.get(request_id)
        if row is None:
            active = await self.latest(active=True)
            if active:
                if not (await self.state(active))[1]:
                    return await self.snapshot(active["request_id"]), 409
                async with self.engine.begin() as conn:
                    await conn.execute(update(requests).where(requests.c.request_id == active["request_id"]).values(active_slot=None))
            result, destination = await readiness()
            if not result["ready"]:
                return result, 503
            if policy.check("request_team_demo_call", "router", {"case_id": CASE_ID,
                    "company_id": COMPANY_ID, "directory_verified": True}).result != "allow":
                result.update(ready=False, reason="Team calling is blocked by policy.")
                return result, 403
            now = time.time()
            try:
                async with self.engine.begin() as conn:
                    await conn.execute(insert(requests).values(request_id=request_id, task_id=root_task_id(request_id),
                        active_slot="team", case_id=CASE_ID, company_id=COMPANY_ID,
                        destination_hash=destination_hash(destination["phone"]), destination_suffix=destination["phone"][-4:],
                        created_at=now, expires_at=now + GRANT_SECONDS, voice_task_id="", dispatch_state="prepared", last_send_at=0))
            except IntegrityError:
                row = await self.get(request_id)
                if row is None:
                    return await self.snapshot(), 409
            row = row or await self.get(request_id)
        public, finished = await self.state(row)
        if finished or await self.runtime.get(row["task_id"]):
            return await self.snapshot(request_id), 200
        if row["expires_at"] <= time.time() or row["active_slot"] != "team":
            return await self.snapshot(request_id), 409
        result, destination = await readiness()
        if not result["ready"] or destination_hash(destination["phone"]) != row["destination_hash"]:
            result.update(ready=False, reason="Call readiness changed. This request cannot be dispatched.", request=public)
            return result, 503
        now = time.time()
        async with self.engine.begin() as conn:
            claimed = await conn.execute(update(requests).where(requests.c.request_id == request_id,
                requests.c.expires_at > now, requests.c.active_slot == "team",
                or_(requests.c.dispatch_state.in_(("prepared", "uncertain")), requests.c.last_send_at < now - 30)).values(
                    dispatch_state="sending", last_send_at=now))
        if claimed.rowcount:
            try:
                task = await send_task("verification", envelope(request_id), DelegationMeta(
                    case_id=CASE_ID, from_agent="router", idempotency_key=request_key(request_id)))
                if task.id != row["task_id"]:
                    raise DemoCallBlocked("Agent admission identity mismatch")
                dispatch = "accepted"
            except Exception:  # noqa: BLE001 -- acceptance may be durable; retry only the same A2A key
                dispatch = "uncertain"
            async with self.engine.begin() as conn:
                await conn.execute(update(requests).where(requests.c.request_id == request_id,
                    requests.c.last_send_at == now).values(dispatch_state=dispatch))
        return await self.snapshot(request_id), 202

    async def authorize_voice(self, env: TaskEnvelope, meta: DelegationMeta, task_id: str,
                              company_id: str, intended: str, routed: str) -> bool:
        """False means ordinary hours apply; a matched but invalid grant raises."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(requests).where(requests.c.task_id == meta.parent_task_id))).mappings().first()
        if row is None:
            return False
        if (not enabled() or row["case_id"] != env.case_id or env.case_id != CASE_ID
                or row["company_id"] != company_id or company_id != COMPANY_ID
                or meta.from_agent != "verification" or task_id != child_task_id(row["task_id"])
                or intended != routed or destination_hash(routed) != row["destination_hash"]):
            raise DemoCallBlocked("Team call authorization does not match this task or destination.")
        root, child = await self.runtime.get(row["task_id"]), await self.runtime.get(task_id)
        if (not root or root["agent"] != "verification" or root["sender"] != "router"
                or root["status"] in TERMINAL or root["request_key"] != request_key(row["request_id"])
                or digest(root["envelope"]) != digest(envelope(row["request_id"]).model_dump())
                or not child or child["agent"] != "voice_desk" or child["sender"] != "verification"
                or child["status"] in TERMINAL
                or digest(wire_form(child["envelope"])) != digest(wire_form(env.model_dump()))
                or digest(wire_form(child["delegation"])) != digest(wire_form(meta.model_dump(exclude_none=True)))):
            raise DemoCallBlocked("Team call authorization has no active durable delegation.")
        async with self.engine.connect() as conn:
            reservation = (await conn.execute(select(dependencies).where(
                dependencies.c.parent_id == row["task_id"], dependencies.c.child_id == task_id,
                dependencies.c.invocation == "verified_callback", dependencies.c.target == "voice_desk"))).mappings().first()
        if (not reservation
                or digest(wire_form(json.loads(reservation["envelope"]))) != digest(wire_form(env.model_dump()))
                or digest(wire_form(json.loads(reservation["delegation"])))
                != digest(wire_form(meta.model_dump(exclude_none=True)))):
            raise DemoCallBlocked("Team call authorization does not match the reserved delegation.")
        if policy.check("consume_team_demo_call", "voice_desk", {"case_id": CASE_ID,
                "company_id": COMPANY_ID, "task_id": task_id}).result != "allow":
            raise DemoCallBlocked("Team calling is blocked by policy.")
        async with self.engine.begin() as conn:
            claimed = await conn.execute(update(requests).where(requests.c.request_id == row["request_id"],
                requests.c.active_slot == "team", requests.c.expires_at > time.time(),
                requests.c.voice_task_id.in_(("", task_id))).values(voice_task_id=task_id))
        if not claimed.rowcount:
            raise DemoCallBlocked("Team call authorization expired or was already consumed.")
        return True


async def authorize_demo_voice(env: TaskEnvelope, meta: DelegationMeta, task_id: str,
                               company_id: str, intended: str, routed: str) -> bool:
    if not meta.parent_task_id:
        return False
    store = DemoCallStore()
    try:
        await store.initialize()
        return await store.authorize_voice(env, meta, task_id, company_id, intended, routed)
    finally:
        await store.runtime.close()
