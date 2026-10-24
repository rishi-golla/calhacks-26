"""Durable outbound-call attempts. An uncertain creation is never blindly retried."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Column, Float, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from common.config import CALL_PURPOSES
from common.envelope import CallResult, DelegationMeta, PlaceCall, TaskEnvelope
from common.outreach import route_target
from common.policy import check
from common.providers import infer, mode
from common.spacetime_client import SpacetimeClient

metadata = MetaData()
attempts = Table("w3_voice_attempts", metadata,
                 Column("id", String(36), primary_key=True), Column("task_id", String(36), unique=True),
                 Column("case_id", Text, nullable=False), Column("payload", Text, nullable=False),
                 Column("status", String(30), nullable=False), Column("provider_id", Text, nullable=False),
                 Column("token_hash", Text, nullable=False), Column("created_at", Float, nullable=False),
                 Column("result", Text, nullable=False), Column("delivered", String(1), nullable=False))


class VoiceError(RuntimeError):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class CallStore:
    def __init__(self, url: str | None = None):
        if not url:
            from common.runtime_store import runtime_url
            url = runtime_url()
        self.engine = create_async_engine(url)

    async def initialize(self):
        from common.runtime_store import RuntimeStore
        async with RuntimeStore(engine=self.engine).schema_lock(), self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def close(self):
        await self.engine.dispose()

    async def get(self, attempt_id: str) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(attempts).where(attempts.c.id == attempt_id))).mappings().first()
        return dict(row) if row else None

    async def by_provider(self, provider_id: str) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(attempts).where(attempts.c.provider_id == provider_id))).mappings().first()
        return dict(row) if row else None

    async def pending(self) -> list[dict]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(attempts).where(attempts.c.delivered == "0"))).mappings().all()
        return [dict(row) for row in rows]

    async def prepare(self, attempt_id: str, env: TaskEnvelope, task_id: str, payload: dict,
                      token: str) -> dict:
        existing = await self.get(attempt_id)
        if existing:
            return existing
        try:
            async with self.engine.begin() as conn:
                await conn.execute(insert(attempts).values(
                    id=attempt_id, task_id=task_id, case_id=env.case_id, payload=json.dumps(payload),
                    status="prepared", provider_id="", token_hash=_digest(token), created_at=time.time(),
                    result="", delivered="0"))
        except IntegrityError:
            pass
        row = await self.get(attempt_id)
        if not row:
            raise VoiceError("conflicting voice attempt")
        return row

    async def claim_dial(self, attempt_id: str) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(update(attempts).where(
                attempts.c.id == attempt_id, attempts.c.status == "prepared").values(status="dialing"))
        return result.rowcount == 1

    async def set(self, attempt_id: str, **values):
        async with self.engine.begin() as conn:
            await conn.execute(update(attempts).where(attempts.c.id == attempt_id).values(**values))

    async def freeze_finalization(self, row: dict, finalization: dict) -> dict:
        """Persist one immutable projection before attempting the remote write."""
        payload = json.loads(row["payload"])
        payload["finalization"] = finalization
        async with self.engine.begin() as conn:
            await conn.execute(update(attempts).where(
                attempts.c.id == row["id"], attempts.c.payload == row["payload"]
            ).values(payload=json.dumps(payload)))
        current = await self.get(row["id"])
        return json.loads(current["payload"])["finalization"]


def check_call_window(company: dict) -> None:
    timezone = company.get("timezone") or os.getenv("OUTREACH_TIMEZONE")
    if not timezone:
        raise VoiceError("verified contact timezone is required for live calls")
    hour = datetime.now(ZoneInfo(timezone)).hour
    start, end = int(os.getenv("OUTREACH_START_HOUR", "8")), int(os.getenv("OUTREACH_END_HOUR", "18"))
    if not 0 <= start < end <= 24 or not start <= hour < end:
        raise VoiceError("outreach is outside the configured calling hours")


def call_config(purpose: str) -> tuple[dict, str, str]:
    key = os.getenv("CARTESIA_API_KEY", "")
    persona = os.getenv(f"CARTESIA_AGENT_ID_{purpose.upper()}", "")
    number = os.getenv("CARTESIA_FROM_NUMBER_ID", "")
    if not all((key, persona, number, os.getenv("CARTESIA_WEBHOOK_SECRET"), os.getenv("PUBLIC_BASE_URL"))):
        raise VoiceError("Cartesia credentials, persona, number, webhook secret and public URL are required")
    return {"X-API-Key": key, "Cartesia-Version": "2026-08-14"}, persona, number


async def start_call(env: TaskEnvelope, meta: DelegationMeta, task_id: str) -> tuple[str, CallResult | None]:
    from common.source_repository import SourceRepository

    request = PlaceCall.model_validate(env.subject)
    if CALL_PURPOSES.get(meta.from_agent) != request.purpose:
        raise VoiceError("caller is not permitted to request this call purpose")
    attempt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"freightops:call:{task_id}"))
    if mode() == "live":
        replay_store = CallStore()
        try:
            await replay_store.initialize()
            previous = await replay_store.get(attempt_id)
            if previous and previous["result"]:
                return attempt_id, CallResult.model_validate_json(previous["result"])
            if previous and previous["status"] != "prepared":
                return attempt_id, None
        finally:
            await replay_store.close()
    sources = SourceRepository()
    company = await sources.get_company(request.counterparty_id)
    contact = await sources.get_contact(request.counterparty_id)
    if not contact.get("company_id"):
        contact = None
    target = contact or company
    verified = bool(target and target.get("phone") and target.get("source_url"))
    if contact and float(contact.get("confidence", 0)) < 0.7:
        verified = False
    decision = check("contact_counterparty", "voice_desk", {"directory_verified": verified})
    if decision.result != "allow":
        raise VoiceError("contact must come from a verified directory")
    routed = route_target("phone", target["phone"], env.subject.get("demo_target"))
    if mode() == "fixture":
        fixture = dict(company.get("fixture_call_result", {}))
        fixture_findings = fixture.get("findings", {})
        return attempt_id, CallResult(
            reached=bool(fixture.get("reached", True)), intended_to=routed.intended_to,
            routed_to=routed.routed_to,
            findings={"simulated": True, "claim_confirmed": False, "receipt_provided": False, **fixture_findings},
            transcript_ref=f"fixture://calls/{attempt_id}")
    check_call_window(company)
    headers, persona, number = call_config(request.purpose)
    store = CallStore()
    await store.initialize()
    token = secrets.token_urlsafe(32)
    payload = {"purpose": request.purpose, "company_id": request.counterparty_id,
               "contact_id": contact.get("id", "") if contact else "",
               "intended_to": routed.intended_to, "routed_to": routed.routed_to,
               "questions": request.questions, "facts": request.facts,
               "evidence_refs": env.evidence_refs, "persona": persona}
    try:
        row = await store.prepare(attempt_id, env, task_id, payload, token)
        if row["result"]:
            return attempt_id, CallResult.model_validate_json(row["result"])
        if row["status"] != "prepared":
            return attempt_id, None
        # W2 enforces company/hour, human ownership, verified directory and demo targets atomically.
        await SpacetimeClient(actor="voice_desk").mutate("upsert_call", {
            "write_id": f"call:{attempt_id}:queued", "id": attempt_id, "case_id": env.case_id,
            "task_id": task_id, "company_id": request.counterparty_id,
            "contact_id": payload["contact_id"], "routed_to": routed.routed_to,
            "persona": request.purpose, "direction": "outbound", "status": "queued",
        })
        # A restart before claim can generate a replacement capability; store only its hash.
        if not await store.claim_dial(attempt_id):
            return attempt_id, None
        await store.set(attempt_id, token_hash=_digest(token))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post("https://api.cartesia.ai/agents/calls", headers=headers, json={
                    "agent_id": persona, "from_number_id": number, "ringing_timeout_seconds": 25,
                    "outbound_calls": [{"to_number": routed.routed_to, "dynamic_variables": {
                        "case_id": env.case_id, "task_id": task_id, "attempt_id": attempt_id,
                        "call_token": token, "purpose": request.purpose,
                        "questions": json.dumps(request.questions), "facts": json.dumps(request.facts),
                    }}],
                })
                response.raise_for_status()
                call = response.json()["calls"][0]
                if call.get("error") or not call.get("agent_call_id"):
                    raise VoiceError("Cartesia rejected the call")
                await store.set(attempt_id, provider_id=call["agent_call_id"], status="ringing")
        except (httpx.HTTPError, KeyError, ValueError, IndexError, VoiceError):
            await store.set(attempt_id, status="uncertain")
            # A request may have succeeded remotely. Never redial an uncertain attempt automatically.
        return attempt_id, None
    finally:
        await store.close()


def redact_transcript(transcript: list[dict]) -> str:
    text = "\n".join(f"{item.get('role', 'speaker')}: {item.get('text', '')}" for item in transcript)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
    text = re.sub(r"(?<!\w)\+?\d[\d ()-]{7,}\d", "[number]", text)
    return text[:50_000]


async def fetch_call(provider_id: str) -> dict:
    key = os.getenv("CARTESIA_API_KEY", "")
    if not key or not re.fullmatch(r"[A-Za-z0-9_-]+", provider_id):
        raise VoiceError("invalid Cartesia call configuration")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"https://api.cartesia.ai/agents/calls/{provider_id}", headers={
            "X-API-Key": key, "Cartesia-Version": "2026-08-14"})
        response.raise_for_status()
        return response.json()


async def finish_call(store: CallStore, row: dict, external: dict) -> bool:
    if external.get("status") not in {"completed", "failed"}:
        return False
    if external.get("id") != row["provider_id"]:
        raise VoiceError("call result identity mismatch")
    if row["result"]:
        return True
    payload = json.loads(row["payload"])
    if payload.get("finalization"):
        return await project_finalization(store, row, payload["finalization"])
    transcript = redact_transcript(external.get("transcript") or [])
    reason = external.get("end_reason", "")
    reached = bool(transcript) and external["status"] == "completed" and reason not in {
        "dial_no_answer", "dial_busy", "dial_failed", "dial_timeout", "voicemail_detected", "api_cancelled"}
    findings: dict = {"end_reason": reason, "verified": False}
    if reached:
        env = TaskEnvelope(task_type="place_call", case_id=row["case_id"],
                           evidence_refs=[f"call://{row['id']}"])
        counterparty = redact_transcript([item for item in external.get("transcript", [])
                                          if item.get("role") == "user"])
        assessment = await infer("voice_desk", env, {"transcript": transcript,
                                                      "counterparty_transcript": counterparty,
                                                      "questions": payload["questions"]}, purpose="call_findings")
        findings["assessment"] = assessment["assessment"]
        findings["model_used"] = assessment["model_used"]
        findings.update({key: assessment["assessment"].get(key) for key in
                         ("claim_confirmed", "authorized_change", "supporting_quote")})
        findings["receipt_provided"] = False  # a conversation cannot supply documentary evidence
    result = CallResult(reached=reached, intended_to=payload["intended_to"], routed_to=payload["routed_to"],
                        findings=findings, transcript_ref=f"call://{row['id']}" if transcript else None)
    status = "completed" if reached else "failed" if external["status"] == "failed" else "no_answer"
    projection = {
        "write_id": f"call:{row['id']}:finished", "id": row["id"], "case_id": row["case_id"],
        "task_id": row["task_id"], "status": status, "findings_json": result.model_dump(),
        "transcript_redacted": transcript,
        "disclosure_given": any(phrase in transcript.lower() for phrase in ("ai assistant", "automated assistant")),
    }
    finalization = await store.freeze_finalization(row, {
        "projection": projection, "result": result.model_dump_json(), "status": status,
    })
    return await project_finalization(store, row, finalization)


async def project_finalization(store: CallStore, row: dict, finalization: dict) -> bool:
    await SpacetimeClient(actor="voice_desk").mutate("upsert_call", finalization["projection"])
    await store.set(row["id"], result=finalization["result"], status=finalization["status"])
    return True


def valid_call_token(row: dict, token: str) -> bool:
    return bool(token) and hmac.compare_digest(row["token_hash"], _digest(token)) \
        and time.time() - row["created_at"] < 3600 and not row["result"]
