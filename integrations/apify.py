"""Bounded Apify lookups with durable starts and public-source provenance.

The supported actors and input schemas were checked against their public build
OpenAPI definitions. A submitted run is never blindly retried after an ambiguous
response. External evidence does not approve a counterparty, contact or payment.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
import phonenumbers
from sqlalchemy import Column, Float, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.exc import IntegrityError

from common import policy
from common.runtime_store import RuntimeStore, canonical

API = "https://api.apify.com/v2"
CACHE_SECONDS = 24 * 60 * 60
NORMALIZATION_VERSION = 2
POLL_SECONDS = 65
MAX_RESPONSE_BYTES = 1_000_000
ID = re.compile(r"^[A-Za-z0-9]{5,40}$")
ACTORS = {
    "carrier": {"actor": "humble-echidna~fmcsa-carrier-check", "build": "0.1.2", "budget": 0.05,
                "actor_env": "APIFY_FMCSA_ACTOR_ID", "build_env": "APIFY_FMCSA_BUILD"},
    "company": {"actor": "vdrmota~contact-info-scraper", "build": "0.2.242", "budget": 0.50,
                "actor_env": "APIFY_CONTACT_ACTOR_ID", "build_env": "APIFY_CONTACT_BUILD"},
}
metadata = MetaData()
runs = Table("w3_apify_lookups", metadata,
    Column("key", String(64), primary_key=True), Column("attempt", String(36), nullable=False),
    Column("actor", Text, nullable=False), Column("status", String(24), nullable=False),
    Column("run_id", Text, nullable=False), Column("dataset_id", Text, nullable=False),
    Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False), Column("result", Text, nullable=False))


class ApifyError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


def apify_configuration_status(kind: str) -> dict:
    if kind not in ACTORS:
        raise ValueError("unsupported Apify lookup kind")
    spec = ACTORS[kind]
    actor = os.getenv(spec["actor_env"], spec["actor"]).replace("/", "~")
    build = os.getenv(spec["build_env"], spec["build"])
    invalid = []
    # Other actors have different schemas and require a reviewed adapter.
    if actor != spec["actor"]:
        invalid.append(spec["actor_env"])
    if not re.fullmatch(r"\d+\.\d+\.\d+", build):
        invalid.append(spec["build_env"])
    missing = [] if os.getenv("APIFY_TOKEN", "").strip() else ["APIFY_TOKEN"]
    return {"available": not missing and not invalid, "missing_configuration": missing,
            "invalid_configuration": invalid, "actor_id": actor, "build": build,
            "max_charge_usd": spec["budget"], "max_run_seconds": 60}


def _unavailable(code: str, **extra) -> dict:
    return {"available": False, "provider": "apify", "error": code,
            "contact_authority_verified": False, "financial_authorization": False,
            "evidence_refs": [], **extra}


def _dot(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise TypeError("USDOT must be a registration identifier")
    text = re.sub(r"^USDOT\s*", "", str(value).strip(), flags=re.IGNORECASE)
    if not re.fullmatch(r"\d{1,8}", text) or int(text) == 0:
        raise ValueError("USDOT must contain 1..8 digits")
    return str(int(text))


def _docket(value) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip().upper().replace("-", "").replace(" ", "")
    if re.fullmatch(r"\d{1,8}", text):
        text = "MC" + text
    match = re.fullmatch(r"(MC|MX|FF)(\d{1,8})", text)
    if not match or int(match[2]) == 0:
        raise ValueError("MC must be a carrier docket identifier")
    return match[1] + str(int(match[2]))


async def public_website(url: str) -> str:
    """Only an ordinary public company homepage/contact URL can leave this service."""
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("a public company website is required")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("invalid company website port") from None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.query
            or parsed.fragment or port not in (None, 443) or "." not in host
            or host.endswith((".localhost", ".local", ".internal", ".test", ".example"))
            or host in {"localhost", "example.com", "example.org", "example.net", "linktr.ee"}):
        raise ValueError("only public HTTPS company URLs without credentials or query parameters are allowed")
    try:
        answers = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, 443,
                                                          type=socket.SOCK_STREAM), timeout=5)
        addresses = {ipaddress.ip_address(answer[4][0]) for answer in answers}
    except (OSError, ValueError, TimeoutError):
        raise ValueError("company website could not be resolved safely") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("company website must resolve only to public addresses")
    return urlunsplit(("https", host, parsed.path or "/", "", ""))


class LookupStore:
    def __init__(self, runtime: RuntimeStore):
        self.runtime, self.engine = runtime, runtime.engine

    async def initialize(self):
        async with self.runtime.schema_lock(), self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def get(self, key: str) -> dict | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(runs).where(runs.c.key == key))).mappings().first()
            return dict(row) if row else None

    async def prepare(self, key: str, actor: str) -> dict:
        now = time.time()
        values = {"attempt": str(uuid.uuid4()), "actor": actor, "status": "prepared", "run_id": "",
                  "dataset_id": "", "created_at": now, "updated_at": now,
                  "expires_at": now + CACHE_SECONDS, "result": ""}
        try:
            async with self.engine.begin() as conn:
                await conn.execute(insert(runs).values(key=key, **values))
        except IntegrityError:
            # Completed cache entries can refresh after 24h. Unknown/failed starts
            # require operator reconciliation, never an automatic second charge.
            async with self.engine.begin() as conn:
                await conn.execute(update(runs).where(runs.c.key == key, runs.c.status == "completed",
                                                      runs.c.expires_at < now).values(**values))
        return await self.get(key)

    async def claim(self, row: dict) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(update(runs).where(runs.c.key == row["key"],
                runs.c.attempt == row["attempt"], runs.c.status == "prepared").values(
                    status="starting", updated_at=time.time()))
            return result.rowcount == 1

    async def set(self, row: dict, **values):
        async with self.engine.begin() as conn:
            await conn.execute(update(runs).where(runs.c.key == row["key"],
                runs.c.attempt == row["attempt"]).values(**values, updated_at=time.time()))


async def _request(client: httpx.AsyncClient, method: str, path: str, *, params=None, body=None):
    """Never expose response bodies, request headers or credentials in errors."""
    try:
        async with client.stream(method, API + path, params=params, json=body,
                                 headers={"Authorization": f"Bearer {os.environ['APIFY_TOKEN']}"}) as response:
            if response.status_code >= 400:
                raise ApifyError(f"apify_http_{response.status_code}",
                                 uncertain=method == "POST" and (response.status_code >= 500
                                                                 or response.status_code == 408))
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ApifyError("apify_response_too_large", uncertain=method == "POST")
            result = json.loads(content)
            return result
    except ApifyError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise ApifyError("apify_transport_or_protocol_error", uncertain=method == "POST") from None


def _text(value, limit=500):
    return value[:limit] if isinstance(value, str) else None


def normalize_carriers(items: list, *, usdot: str | None, mc: str | None) -> list[dict]:
    records = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        try:
            dot = _dot(item.get("dotNumber"))
            dockets = list(dict.fromkeys(filter(None, (_docket(x) for x in item.get("docketNumbers", [])[:20]))))
        except (ValueError, TypeError):
            continue
        if not dot or usdot and dot != usdot or mc and mc not in dockets:
            continue
        source = "https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type=queryCarrierSnapshot&query_param=USDOT&query_string=" + dot
        fields = {"legal_name": "legalName", "dba_name": "dbaName", "authority_status": "operatingAuthorityStatus",
                  "usdot_status": "usdotStatus", "safety_rating": "safetyRating", "phone": "phone", "email": "email",
                  "checked_at": "checkedAt", "live_check": "liveCheck"}
        record = {key: _text(item.get(value)) for key, value in fields.items()}
        record.update(usdot=dot, docket_numbers=dockets, source_url=source,
                      out_of_service=item.get("outOfService") if isinstance(item.get("outOfService"), bool) else None,
                      contact_authority_verified=False, financial_authorization=False, identity_match=True)
        authority = str(item.get("operatingAuthorityStatus", "")).strip().lower()
        record["authority_confirmed_active"] = True if authority == "authorized" else (
            False if authority in {"not authorized", "out-of-service"} else None)
        insurance = item.get("insurance")
        record["insurance"] = {k: v for k, v in insurance.items()
            if (k in {"bipdRequired", "bipdOnFile"} and isinstance(v, (int, float))
                and not isinstance(v, bool) and math.isfinite(v))
            or (k in {"cargoRequired", "cargoOnFile", "bondRequired", "bondOnFile"}
                and isinstance(v, bool))} if isinstance(insurance, dict) else {}
        record["flags"] = [x[:200] for x in item.get("flags", [])[:20] if isinstance(x, str)] \
            if isinstance(item.get("flags"), list) else []
        record["freshness"] = {k: {"source": _text(v.get("source")), "as_of": _text(v.get("asOf"))}
            for k, v in list(item.get("sources", {}).items())[:20] if isinstance(v, dict)} \
            if isinstance(item.get("sources"), dict) else {}
        records.append(record)
    return records


def _public_phone_candidates(values: list[str]) -> list[str]:
    """Keep plausible public phone observations, never evidence of contact authority.

    Unprefixed numbers use the brokerage's North American numbering context;
    international numbers must identify their country with an explicit '+'.
    Avoid the parser's permissive text extraction and vanity-number conversion.
    """
    candidates, seen = [], set()
    for raw in values:
        value = raw.strip()
        match = re.fullmatch(r"(\+?[0-9().\s-]+?)(?:\s*(?:ext\.?|x|#)\s*([0-9]{1,6}))?", value,
                             flags=re.IGNORECASE)
        if not match:
            continue
        digits = re.sub(r"[^0-9]", "", match[1])
        if not value.startswith("+") and not (len(digits) == 10 or len(digits) == 11 and digits[0] == "1"):
            continue
        try:
            number = phonenumbers.parse(value, None if value.startswith("+") else "US")
        except phonenumbers.NumberParseException:
            continue
        if not phonenumbers.is_valid_number(number):
            continue
        identity = (phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164), number.extension)
        if identity not in seen:
            seen.add(identity)
            candidates.append(value)
    return candidates


def normalize_companies(items: list, url: str) -> list[dict]:
    records = []
    origin = urlsplit(url).hostname
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        returned_url = item.get("url") or item.get("startUrl") or url
        if not isinstance(returned_url, str) or urlsplit(returned_url).hostname != origin:
            continue
        def strings(*keys, item=item):
            values = []
            for key in keys:
                found = item.get(key, [])
                if isinstance(found, list):
                    values.extend(x[:200] for x in found[:30] if isinstance(x, str))
            return list(dict.fromkeys(values))[:30]
        records.append({"url": url, "source_url": url, "emails": strings("emails"),
                        "phones": _public_phone_candidates(strings("phones", "phoneNumbers", "phonesUncertain")),
                        "contact_authority_verified": False, "financial_authorization": False})
    return records


async def _run_lookup(agent, env, task_id: str, kind: str, actor_input: dict, identity: dict, *,
                      client: httpx.AsyncClient | None = None) -> dict:
    config = apify_configuration_status(kind)
    if not config["available"]:
        return _unavailable("apify_not_configured", **config)
    if os.getenv("FREIGHTOPS_MODE", "fixture") != "live":
        return _unavailable("live_apify_requires_live_mode")
    policy_result = policy.check("start_external_lookup", agent.name, {
        "case_id": env.case_id, "task_id": task_id, "provider": "apify", "kind": kind,
        "actor_id": config["actor_id"], "max_charge_usd": config["max_charge_usd"],
    })
    if policy_result.result != "allow":
        return _unavailable("apify_lookup_denied", policy_ids=policy_result.policy_ids)
    await agent._assert_execution(task_id)
    lookup_key = hashlib.sha256(canonical({"kind": kind, "actor": config["actor_id"],
        "build": config["build"], "input": actor_input}).encode()).hexdigest()
    store = LookupStore(agent.store)
    await store.initialize()
    row = await store.prepare(lookup_key, config["actor_id"])
    if row["status"] == "completed":
        cached = json.loads(row["result"])
        if cached.get("normalization_version") == NORMALIZATION_VERSION:
            return {**cached, "cached": True}
    owned = client is None
    http = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
    try:
        if await store.claim(row):
            await agent._assert_execution(task_id)
            try:
                response = await _request(http, "POST", f"/acts/{config['actor_id']}/runs", body=actor_input,
                    params={"build": config["build"], "timeout": 60, "memory": 512,
                            "maxTotalChargeUsd": config["max_charge_usd"]})
                run = response.get("data", {}) if isinstance(response, dict) else {}
                if not isinstance(run, dict):
                    raise ApifyError("apify_invalid_run_response", uncertain=True)
                if not ID.fullmatch(str(run.get("id", ""))):
                    raise ApifyError("apify_invalid_run_response", uncertain=True)
                await store.set(row, status="running", run_id=run["id"])
            except ApifyError as exc:
                await store.set(row, status="uncertain" if exc.uncertain else "failed")
                return _unavailable(exc.code, lookup_id=lookup_key,
                                    requires_reconciliation=exc.uncertain)
        deadline = time.monotonic() + POLL_SECONDS
        while time.monotonic() < deadline:
            row = await store.get(lookup_key)
            if row["status"] == "completed":
                cached = json.loads(row["result"])
                if cached.get("normalization_version") == NORMALIZATION_VERSION:
                    return {**cached, "cached": True}
            if row["status"] in {"uncertain", "failed"}:
                return _unavailable("apify_previous_start_requires_reconciliation", lookup_id=lookup_key,
                                    requires_reconciliation=True)
            if row["status"] == "starting":
                # A concurrent caller may still be receiving the start response.
                if time.time() - row["updated_at"] > 20:
                    return _unavailable("apify_start_uncertain", lookup_id=lookup_key, requires_reconciliation=True)
                await asyncio.sleep(0.5)
                continue
            run_id = row["run_id"]
            if not ID.fullmatch(run_id):
                raise ApifyError("apify_invalid_stored_run")
            response = await _request(http, "GET", f"/actor-runs/{run_id}", params={"waitForFinish": 5})
            run = response.get("data", {}) if isinstance(response, dict) else {}
            if not isinstance(run, dict):
                raise ApifyError("apify_invalid_run_response")
            if run.get("id") != run_id:
                raise ApifyError("apify_run_identity_mismatch")
            status = run.get("status")
            if status == "SUCCEEDED":
                dataset_id = run.get("defaultDatasetId", "")
                if not isinstance(dataset_id, str) or not ID.fullmatch(dataset_id):
                    raise ApifyError("apify_invalid_dataset")
                items = await _request(http, "GET", f"/datasets/{dataset_id}/items",
                                       params={"format": "json", "clean": "true", "limit": 5})
                if not isinstance(items, list):
                    raise ApifyError("apify_invalid_dataset_items")
                records = normalize_carriers(items, **identity) if kind == "carrier" else normalize_companies(
                    items, identity["url"])
                source_urls = list(dict.fromkeys(record["source_url"] for record in records))
                # Re-normalize an old cached dataset without another paid Actor
                # start or extending the source observation's freshness window.
                old_cache = json.loads(row["result"]) if row["status"] == "completed" else {}
                result = {"available": bool(records), "provider": "apify", "actor_id": config["actor_id"],
                    "run_id": run_id, "dataset_id": dataset_id, "cached": bool(old_cache),
                    "normalization_version": NORMALIZATION_VERSION,
                    "fetched_at": old_cache.get("fetched_at") or datetime.now(UTC).isoformat(),
                    "source_url": source_urls[0] if source_urls else None,
                    "source_kind": "external_public_record" if kind == "carrier" else "external_website",
                    "source_simulated": False, "records": records,
                    "identity_match": bool(records) if kind == "carrier" else None,
                    "evidence_refs": [f"apify://runs/{run_id}", *source_urls],
                    "contact_authority_verified": False, "financial_authorization": False,
                    "limitation": "Public observations require independent review before contact or financial authorization."}
                if not records:
                    result["error"] = "no_matching_source_records"
                await store.set(row, status="completed", dataset_id=dataset_id, result=canonical(result),
                                expires_at=row["expires_at"] if old_cache else time.time() + CACHE_SECONDS)
                return result
            if status in {"FAILED", "TIMED-OUT", "ABORTED"}:
                await store.set(row, status="failed")
                return _unavailable("apify_run_failed", run_id=run_id, run_status=status)
            if status not in {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}:
                raise ApifyError("apify_invalid_run_status")
            await asyncio.sleep(0.5)
        return _unavailable("apify_run_pending", run_id=row["run_id"], lookup_id=lookup_key,
                            can_resume_lookup=True)
    except ApifyError as exc:
        return _unavailable(exc.code, lookup_id=lookup_key, run_id=row["run_id"],
                            can_resume_lookup=bool(row["run_id"]))
    finally:
        if owned:
            await http.aclose()


async def lookup_carrier(agent, env, task_id: str, *, usdot=None, mc=None,
                         client: httpx.AsyncClient | None = None) -> dict:
    try:
        dot, docket = _dot(usdot), _docket(mc)
        if not dot and not docket:
            return _unavailable("carrier_registration_identifier_missing")
    except (ValueError, TypeError):
        return _unavailable("carrier_registration_identifier_invalid")
    return await _run_lookup(agent, env, task_id, "carrier", {
        "dotNumbers": [dot] if dot else [], "docketNumbers": [docket] if docket else [],
        "liveCheck": True, "onlyChanges": False, "maxResults": 1,
    }, {"usdot": dot, "mc": docket}, client=client)


async def scrape_company(agent, env, task_id: str, *, url: str,
                         client: httpx.AsyncClient | None = None) -> dict:
    try:
        website = await public_website(url)
    except ValueError:
        return _unavailable("company_website_missing_or_not_public")
    return await _run_lookup(agent, env, task_id, "company", {
        "startUrls": [{"url": website}], "proxyConfig": {"useApifyProxy": True},
        "maxRequests": 1, "maxRequestsPerStartUrl": 1, "maxDepth": 0, "sameDomain": True,
        "mergeContacts": True, "considerChildFrames": False, "useBrowser": False,
        "maximumLeadsEnrichmentRecords": 0, "verifyLeadsEnrichmentEmails": False,
        "scrapeSocialMediaProfiles": {key: False for key in ("facebooks", "instagrams", "youtubes", "tiktoks", "twitters")},
    }, {"url": website}, client=client)
