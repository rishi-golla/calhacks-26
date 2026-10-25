"""Durable outbound-call attempts. An uncertain creation is never blindly retried."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError
from sqlalchemy import Column, Float, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from common.config import CALL_PURPOSES
from common.envelope import CallResult, DelegationMeta, PlaceCall, TaskEnvelope
from common.outreach import route_target
from common.policy import check
from common.providers import infer, mode
from common.spacetime_client import SpacetimeClient, SpacetimeHTTPError

metadata = MetaData()
attempts = Table("w3_voice_attempts", metadata,
                 Column("id", String(36), primary_key=True), Column("task_id", String(36), unique=True),
                 Column("case_id", Text, nullable=False), Column("payload", Text, nullable=False),
                 Column("status", String(30), nullable=False), Column("provider_id", Text, nullable=False),
                 Column("token_hash", Text, nullable=False), Column("created_at", Float, nullable=False),
                 Column("result", Text, nullable=False), Column("delivered", String(1), nullable=False))


class VoiceError(RuntimeError):
    pass


class CallRefused(VoiceError):
    """A condition that makes this call impossible as requested: caller purpose, case, directory, calling hours or
    configuration. Retrying the same call is refused again. The message is fixed text, safe to record."""


CARTESIA_VERSION = "2026-08-14"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,200}")
_PHONE = re.compile(r"\+[1-9][0-9]{7,14}")


def voice_configuration_status(purpose: str) -> dict:
    """Read configuration names only; this never calls the provider or reveals credentials."""
    if purpose not in {"verification", "collections", "approvals", "cash_brief"}:
        return {"available": False, "missing_configuration": [], "invalid_configuration": ["purpose"]}
    webhook_tools = os.getenv("CARTESIA_WEBHOOK_TOOLS_ENABLED", "0") == "1"
    required = ("CARTESIA_API_KEY", f"CARTESIA_AGENT_ID_{purpose.upper()}", "CARTESIA_FROM_NUMBER_ID",
                "DEMO_ALLOWLIST_PHONES")
    if webhook_tools:
        required += ("PUBLIC_BASE_URL",)
    missing = [name for name in required if not os.getenv(name, "").strip()]
    invalid = []
    for name in (f"CARTESIA_AGENT_ID_{purpose.upper()}", "CARTESIA_FROM_NUMBER_ID"):
        if os.getenv(name) and not _IDENTIFIER.fullmatch(os.environ[name]):
            invalid.append(name)
    phones = [value.strip() for value in os.getenv("DEMO_ALLOWLIST_PHONES", "").split(",") if value.strip()]
    if not phones and "DEMO_ALLOWLIST_PHONES" not in missing:
        missing.append("DEMO_ALLOWLIST_PHONES")
    if any(not _PHONE.fullmatch(value) or value in {"+15555550100", "+15555550101"} for value in phones):
        invalid.append("DEMO_ALLOWLIST_PHONES")
    callback = os.getenv("PUBLIC_BASE_URL", "").strip()
    if callback and webhook_tools:
        try:
            parsed = urlsplit(callback)
            host = (parsed.hostname or "").lower().rstrip(".")
            bad_host = host in {"localhost", "example.com", "example.org", "example.net"} or any(
                host.endswith(suffix) for suffix in (".localhost", ".example.com", ".example.org", ".example.net",
                                                      ".example", ".invalid", ".test"))
            try:
                bad_host = bad_host or not ipaddress.ip_address(host).is_global
            except ValueError:
                pass
            if (parsed.scheme != "https" or not host or bad_host or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                invalid.append("PUBLIC_BASE_URL")
        except ValueError:
            invalid.append("PUBLIC_BASE_URL")
    return {"available": not missing and not invalid, "missing_configuration": missing,
            "invalid_configuration": invalid, "completion_mode": "polling", "webhook_tools_enabled": webhook_tools,
            "note": "Configuration readiness only; verified directory, provider resources, calling hours and policy also apply."}


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
        raise CallRefused("verified contact timezone is required for live calls")
    hour = datetime.now(ZoneInfo(timezone)).hour
    start, end = int(os.getenv("OUTREACH_START_HOUR", "8")), int(os.getenv("OUTREACH_END_HOUR", "18"))
    if not 0 <= start < end <= 24 or not start <= hour < end:
        raise CallRefused("outreach is outside the configured calling hours")


def call_config(purpose: str) -> tuple[dict, str, str]:
    status = voice_configuration_status(purpose)
    if not status["available"]:
        raise CallRefused("Cartesia voice configuration is incomplete or invalid")
    key = os.getenv("CARTESIA_API_KEY", "")
    persona = os.getenv(f"CARTESIA_AGENT_ID_{purpose.upper()}", "")
    number = os.getenv("CARTESIA_FROM_NUMBER_ID", "")
    return {"X-API-Key": key, "Cartesia-Version": CARTESIA_VERSION}, persona, number


async def _call_target(env: TaskEnvelope, request: PlaceCall):
    """Resolve and route the verified destination for a call, before anything is recorded or dialed."""
    from common.source_repository import SourceRepository

    sources = SourceRepository()
    source = None
    if mode() == "live":
        source = await sources.load(env)
        if request.counterparty_id not in {source.get("carrier_id"), source.get("shipper_id"),
                                          source.get("counterparty_id")}:
            raise CallRefused("call counterparty is not part of the assigned case")
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
        raise CallRefused("contact must come from a verified directory")
    routed = route_target("phone", target["phone"], env.subject.get("demo_target"))
    return source, sources, company, contact, target, routed


def _requested_call(env: TaskEnvelope, meta: DelegationMeta) -> PlaceCall:
    request = PlaceCall.model_validate(env.subject)
    if CALL_PURPOSES.get(meta.from_agent) != request.purpose:
        raise CallRefused("caller is not permitted to request this call purpose")
    return request


async def call_preflight(env: TaskEnvelope, meta: DelegationMeta) -> str | None:
    """Why this call cannot be placed, or None when it may proceed. Read-only: nothing is recorded or dialed.

    It repeats start_call's fixed gates (purpose, case, verified directory, demo routing, calling hours and voice
    configuration) and the ledger's routing gate: W2 refuses any call whose routed number has no enabled
    `demo_target` row. An impossible call then ends at once, before a model turn, instead of failing in a tool loop.
    A transient read failure returns None; start_call still enforces every gate."""
    from common.demo_calls import CASE_ID as TEAM_CASE
    from common.outreach import OutreachBlocked
    from common.source_repository import NotInDirectory, SourceUnavailable

    try:
        request = _requested_call(env, meta)
        status = voice_configuration_status(request.purpose)
        if not status["available"]:
            names = status["missing_configuration"] + status["invalid_configuration"]
            return "outbound voice configuration is incomplete or invalid: " + ", ".join(names)
        _source, _sources, company, _contact, target, routed = await _call_target(env, request)
        if env.case_id != TEAM_CASE:  # the team demo may hold an operator's hours grant; start_call decides
            check_call_window({**company, "timezone": target.get("timezone") or company.get("timezone")})
    except (CallRefused, NotInDirectory) as refusal:
        return str(refusal)
    except OutreachBlocked:
        return "the call has no allowlisted demo destination"
    except ValidationError:
        return "the call request is malformed"
    except SourceUnavailable:
        return None  # the source could not be read just now: not a fact about this call
    client = SpacetimeClient(actor="voice_desk")
    if client.disabled or not _PHONE.fullmatch(routed.routed_to):
        return None
    try:
        targets = await client.query_rows(f"SELECT * FROM demo_target WHERE phone = '{routed.routed_to}'")
    except Exception:  # noqa: BLE001 -- a read outage leaves the decision to start_call and W2
        return None
    if not any(row.get("enabled") is True for row in targets):
        return "the routed demo number is not enabled in the live database (demo_target), so W2 refuses the call"
    return None


async def start_call(env: TaskEnvelope, meta: DelegationMeta, task_id: str) -> tuple[str, CallResult | None]:
    request = _requested_call(env, meta)
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
    source, sources, company, contact, target, routed = await _call_target(env, request)
    if mode() == "fixture":
        fixture = dict(company.get("fixture_call_result", {}))
        fixture_findings = fixture.get("findings", {})
        return attempt_id, CallResult(
            reached=bool(fixture.get("reached", True)), intended_to=routed.intended_to,
            routed_to=routed.routed_to,
            findings={"simulated": True, "claim_confirmed": False, "receipt_provided": False, **fixture_findings},
            transcript_ref=f"fixture://calls/{attempt_id}")
    from common.voice_context import build_voice_context
    # Cartesia is a separate conversational model. Explicitly pass a complete,
    # allowlisted source brief, never arbitrary facts supplied by a task/model.
    facts = await build_voice_context(source, sources, purpose=request.purpose)
    from common.demo_calls import authorize_demo_voice
    hours_grant = await authorize_demo_voice(env, meta, task_id, request.counterparty_id,
                                             routed.intended_to, routed.routed_to)
    if not hours_grant:
        check_call_window({**company, "timezone": target.get("timezone") or company.get("timezone")})
    headers, persona, number = call_config(request.purpose)
    store = CallStore()
    await store.initialize()
    token = secrets.token_urlsafe(32)
    payload = {"purpose": request.purpose, "company_id": request.counterparty_id,
               "contact_id": contact.get("id", "") if contact else "",
               "intended_to": routed.intended_to, "routed_to": routed.routed_to,
               "questions": request.questions, "facts": facts,
               "evidence_refs": env.evidence_refs, "persona": persona,
               "team_demo_authorized": hours_grant}
    try:
        row = await store.prepare(attempt_id, env, task_id, payload, token)
        if row["result"]:
            return attempt_id, CallResult.model_validate_json(row["result"])
        if row["status"] != "prepared":
            return attempt_id, None
        payload = json.loads(row["payload"])
        # W2 enforces company/hour, human ownership, verified directory and demo targets atomically.
        try:
            await SpacetimeClient(actor="voice_desk").mutate("upsert_call", {
                "write_id": f"call:{attempt_id}:queued", "id": attempt_id, "case_id": env.case_id,
                "task_id": task_id, "company_id": request.counterparty_id,
                "contact_id": payload["contact_id"], "routed_to": routed.routed_to,
                "persona": request.purpose, "direction": "outbound", "status": "queued",
                **({"team_demo_authorized": True} if hours_grant else {}),
            })
        except SpacetimeHTTPError as error:
            from common.runtime import projection_rejected
            if not projection_rejected(error.status_code):
                raise
            # W2 refused to queue this call (routing allowlist, one call per company per hour, directory, human
            # takeover). Nothing was dialed; the same attempt would be refused again, so it ends unreached.
            reason = getattr(error, "reason", "") or f"SpacetimeDB HTTP {error.status_code}"
            result = CallResult(reached=False, intended_to=routed.intended_to, routed_to=routed.routed_to,
                                findings={"end_reason": "ledger_refused", "reason": reason[:200],
                                          "verified": False, "call_accepted": False})
            await store.set(attempt_id, status="refused", result=result.model_dump_json(), delivered="1")
            return attempt_id, result
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
                        "questions": json.dumps(payload["questions"]), "facts": json.dumps(payload["facts"]),
                        "load_reference": payload["facts"].get("load_reference") or "the assigned load",
                    }}],
                })
                if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                    return attempt_id, await reject_attempt(store, row, "provider_rejected")
                response.raise_for_status()
                body = response.json()
                calls = body.get("calls") if isinstance(body, dict) else None
                if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
                    raise VoiceError("Cartesia returned an invalid call response")
                call = calls[0]
                if call.get("number", routed.routed_to) != routed.routed_to:
                    raise VoiceError("Cartesia returned a different call destination")
                if call.get("error") and not call.get("agent_call_id"):
                    return attempt_id, await reject_attempt(store, row, "provider_rejected")
                if not isinstance(call.get("agent_call_id"), str) or not _IDENTIFIER.fullmatch(call["agent_call_id"]):
                    raise VoiceError("Cartesia returned an invalid call identifier")
                await store.set(attempt_id, provider_id=call["agent_call_id"], status="ringing")
        except (httpx.HTTPError, KeyError, ValueError, IndexError, VoiceError):
            await store.set(attempt_id, status="uncertain")
            # A request may have succeeded remotely. Never redial an uncertain attempt automatically.
        return attempt_id, None
    finally:
        await store.close()


def redact_transcript(transcript: list[dict]) -> str:
    text = "\n".join(f"{item.get('role', 'speaker')}: {item['text']}" for item in transcript
                     if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip())
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
    text = re.sub(r"(?<!\w)\+?\d[\d ()-]{7,}\d", "[number]", text)
    return text[:50_000]


async def fetch_call(provider_id: str) -> dict:
    if not isinstance(provider_id, str) or not _IDENTIFIER.fullmatch(provider_id):
        raise VoiceError("invalid Cartesia call identifier")
    return await cartesia_get(f"/agents/calls/{provider_id}")


async def cartesia_get(path: str, *, params: dict | None = None) -> dict:
    """Bounded reads from Cartesia. Errors intentionally omit response bodies and credentials."""
    key = os.getenv("CARTESIA_API_KEY", "")
    if not key or not path.startswith("/agents/calls"):
        raise VoiceError("invalid Cartesia call configuration")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"https://api.cartesia.ai{path}", params=params, headers={
                "X-API-Key": key, "Cartesia-Version": CARTESIA_VERSION})
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise VoiceError("Cartesia call response exceeds limit")
            result = response.json()
            if not isinstance(result, dict):
                raise TypeError
            return result
    except httpx.HTTPStatusError as exc:
        raise VoiceError(f"Cartesia HTTP {exc.response.status_code}") from None
    except (httpx.TransportError, ValueError, TypeError):
        raise VoiceError("Cartesia transport or response failure") from None


async def recover_uncertain_call(store: CallStore, row: dict) -> dict | None:
    """Find an accepted call after a lost create response; never dial a second time."""
    payload = json.loads(row["payload"])
    persona = payload.get("persona")
    if row["provider_id"] or row["status"] not in {"dialing", "uncertain"} or not isinstance(persona, str):
        return None
    params = {"agent_id": persona, "limit": 100,
              "start_time_gte": datetime.fromtimestamp(row["created_at"] - 60, UTC).isoformat()}
    for _ in range(3):
        page = await cartesia_get("/agents/calls", params=params)
        calls = page.get("data", [])
        if not isinstance(calls, list):
            raise VoiceError("Cartesia call list is malformed")
        matches = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            variables = call.get("dynamic_variables") or {}
            if not isinstance(variables, dict):
                continue
            token = variables.get("call_token")
            if (variables.get("attempt_id") == row["id"] and variables.get("task_id") == row["task_id"]
                    and variables.get("case_id") == row["case_id"] and isinstance(token, str)
                    and hmac.compare_digest(row["token_hash"], _digest(token))):
                matches.append(call)
        if len(matches) > 1:
            raise VoiceError("multiple provider calls matched one attempt; manual reconciliation required")
        if matches:
            call_id = matches[0].get("id")
            if not isinstance(call_id, str) or not _IDENTIFIER.fullmatch(call_id):
                raise VoiceError("Cartesia returned an invalid call identifier")
            await store.set(row["id"], provider_id=call_id)
            return await fetch_call(call_id)
        cursor = page.get("next_page")
        if not page.get("has_more") or not isinstance(cursor, str) or not _IDENTIFIER.fullmatch(cursor):
            break
        params["starting_after"] = cursor
    return None


async def reject_attempt(store: CallStore, row: dict, reason: str) -> CallResult:
    payload = json.loads(row["payload"])
    result = CallResult(reached=False, intended_to=payload["intended_to"], routed_to=payload["routed_to"],
                        findings={"end_reason": reason, "verified": False, "call_accepted": False})
    finalization = await store.freeze_finalization(row, {"projection": {
        "write_id": f"call:{row['id']}:finished", "id": row["id"], "case_id": row["case_id"],
        "task_id": row["task_id"], "status": "failed", "findings_json": result.model_dump(),
        "transcript_redacted": "", "disclosure_given": False,
    }, "result": result.model_dump_json(), "status": "failed"})
    await project_finalization(store, row, finalization)
    return result


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
    turns = external.get("transcript") or []
    if not isinstance(turns, list) or len(turns) > 10_000:
        raise VoiceError("Cartesia transcript is malformed or exceeds limit")
    transcript = redact_transcript(turns)
    counterparty = redact_transcript([item for item in turns if isinstance(item, dict) and item.get("role") == "user"])
    agent_speech = redact_transcript([item for item in turns if isinstance(item, dict) and item.get("role") == "assistant"])
    reason = external.get("end_reason", "")
    reached = bool(counterparty) and external["status"] == "completed" and reason not in {
        "dial_no_answer", "dial_busy", "dial_failed", "dial_timeout", "voicemail_detected", "api_cancelled"}
    findings: dict = {"end_reason": reason, "verified": False}
    if reached:
        env = TaskEnvelope(task_type="place_call", case_id=row["case_id"],
                           evidence_refs=[f"call://{row['id']}"])
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
        "disclosure_given": any(phrase in agent_speech.lower() for phrase in ("ai assistant", "automated assistant")),
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
