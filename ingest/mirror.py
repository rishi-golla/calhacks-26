"""Mirror cases, carrier invoices and the counterparty directory into SpacetimeDB (role `ingest`).

Payloads come from the same source export agents read (ingest.source_export), so IDs and amounts agree.
A routed case is mirrored before its event reaches `events`: open_case, its carrier invoice, then the carrier and
shipper with their contacts. The whole directory is pushed at startup and again whenever world.json changes.

Write IDs are `ingest:<reducer>:<id>:<sha256(payload)[:16]>`: replaying the raw stream resends identical writes,
which the module treats as no-ops, and a changed record gets a new write ID. A record that changes and then
changes back reuses its old write ID and is not rewritten; nothing in the simulator does that.

SPACETIME_DISABLED=1 (the default) turns the mirror off.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from common.spacetime_client import SpacetimeClient, SpacetimeError
from ingest import source_export
from integrations import directory, tools
from router.main import ROUTES

log = logging.getLogger("ingest.mirror")
# Directory provenance -> upsert_contact.source. Fixtures stay labeled as fixtures, never as a registry.
CONTACT_SOURCES = {"fmcsa_census": "fmcsa", "sec_edgar": "sec_edgar", "apify_website": "website",
                   "prd_hero_fixture": "fixture", "offline_fixture": "fixture"}
# Only public registry records have a real website worth spending Apify credits on.
ENRICHABLE_SOURCES = {"fmcsa_census", "sec_edgar"}
ENRICH_ROLE = {"carrier": "dispatch", "shipper": "accounts_payable"}
E164 = re.compile(r"\+[1-9]\d{7,14}")
DIRECTORY_CONCURRENCY = 8


def _usd(value) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _phone(value) -> str:
    return value if isinstance(value, str) and E164.fullmatch(value) else ""


def write_id(reducer: str, key: str, payload: dict) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"ingest:{reducer}:{key}:{digest[:16]}"


def routed_case_ids(events: Iterable[dict]) -> list[str]:
    """Cases the router will hand to an agent, resolved the way the router resolves them."""
    found = []
    for event in events:
        payload = event.get("payload") or {}
        case_id = event.get("case_id") or payload.get("case_id")
        if event.get("type") in ROUTES and isinstance(case_id, str) and case_id and case_id not in found:
            found.append(case_id)
    return found


def _deposited(case: dict) -> Decimal:
    return sum((Decimal(str(item.get("amount") or 0)) for item in case.get("deposits") or []), Decimal(0))


def _short_paid(case: dict) -> Decimal:
    billed = case.get("shipper_invoice_amount")
    return max(Decimal(str(billed)) - _deposited(case), Decimal(0)) if billed is not None else Decimal(0)


def _kind(case_id: str, case: dict) -> str:
    if case.get("bank_change_requested") or case_id.endswith("-BANK"):
        return "bank_change"
    if case_id.endswith("-AR"):
        return "receivable"
    return "payable" if case.get("invoice_id") else "delivery"


def exposure(case_id: str, case: dict) -> float:
    """Payable: the amount billed over contract. Bank change: the payment that would be redirected.
    Receivable: what the shipper has not paid."""
    kind = _kind(case_id, case)
    if kind == "bank_change":
        return _usd(case.get("invoice_amount") or (case.get("bank_change") or {}).get("next_payment_amount"))
    if kind == "receivable":
        return _usd(_short_paid(case))
    if kind == "payable" and case.get("invoice_amount") is not None and case.get("contract_rate") is not None:
        return _usd(max(Decimal(str(case["invoice_amount"])) - Decimal(str(case["contract_rate"])), Decimal(0)))
    return 0.0


def priority(case_id: str, case: dict) -> str:
    """Matches the priority ingest gives the case's routable event."""
    kind = _kind(case_id, case)
    if kind == "bank_change":
        return "P0"
    if kind == "payable" and (case.get("claim_type") or exposure(case_id, case) > 0
                              or case.get("carrier_identity_mismatch")):
        return "P1"
    return "P2"


def title(case_id: str, case: dict) -> str:
    kind = _kind(case_id, case)
    carrier = (case.get("carrier") or {}).get("name") or case.get("carrier_id")
    shipper = (case.get("shipper") or {}).get("name") or case.get("shipper_id")
    if kind == "bank_change":
        subject, what = carrier, "bank change"
    elif kind == "receivable":
        subject, what = shipper, "short pay" if _short_paid(case) > 0 else "payment"
    elif kind == "payable" and case.get("carrier_identity_mismatch"):
        subject, what = carrier, "carrier identity mismatch"
    elif kind == "payable":
        subject, what = carrier, (case.get("claim_type") or "invoice").replace("_", " ")
    else:
        subject, what = carrier, "delivery"
    text = " ".join(part for part in (subject, what) if part)
    return f"{text} on {case['load_id']}" if case.get("load_id") else text


def case_payload(case_id: str, case: dict, routed_priority: str | None = None) -> dict:
    """`routed_priority` is the priority of the event routing this case; the more urgent of it and the case's own
    wins, so a demo case ingest raised to P0 shows as P0."""
    urgency = sorted(p for p in (priority(case_id, case), routed_priority) if p in ("P0", "P1", "P2", "P3"))
    payload = {"case_id": case_id, "title": title(case_id, case), "priority": urgency[0],
               "exposure_usd": exposure(case_id, case)}
    if case.get("load_id"):
        payload["load_id"] = case["load_id"]
    return payload


def invoice_payload(case_id: str, case: dict) -> dict | None:
    """The carrier invoice this case owns. Bank-change and receivable cases only refer to an invoice that
    belongs to the payable case (CASE-<load> or CASE-<load>-R), and an invoice's case is immutable."""
    if _kind(case_id, case) != "payable":
        return None
    required = ("invoice_id", "carrier_id", "load_id", "invoice_amount", "contract_rate")
    if missing := [key for key in required if case.get(key) in (None, "")]:
        log.warning("not mirroring invoice of %s: missing %s", case_id, ", ".join(missing))
        return None
    if Decimal(str(case["invoice_amount"])) <= 0:
        log.warning("not mirroring invoice of %s: amount is not positive", case_id)
        return None
    payload = {"id": case["invoice_id"], "case_id": case_id, "carrier_id": case["carrier_id"],
               "load_id": case["load_id"], "amount": _usd(case["invoice_amount"]),
               "contract_amount": _usd(case["contract_rate"])}
    if case.get("issued_at"):
        payload["issued_at"] = case["issued_at"]
    return payload


def company_payload(company: dict, kind: str) -> dict:
    return {"id": company["id"], "kind": company.get("kind") or kind,
            "name": company.get("name") or company.get("legal_name") or company["id"],
            "usdot": str(company.get("usdot") or ""), "mc": str(company.get("mc") or ""),
            "phone": _phone(company.get("phone")), "website": company.get("website") or "",
            "source_url": company.get("source_url") or ""}


def contact_payload(contact: dict, alternates: list | None = None) -> dict | None:
    source = CONTACT_SOURCES.get(contact.get("source"))
    if not source:
        log.warning("not mirroring contact %s: no upsert_contact source for %r", contact.get("id"),
                    contact.get("source"))
        return None
    return {"id": contact["id"], "company_id": contact["company_id"], "name": contact.get("name") or "",
            "title": contact.get("title") or "", "phone": _phone(contact.get("phone")),
            "email": contact.get("email") or "", "linkedin_url": contact.get("linkedin_url") or "",
            "source": source, "source_url": contact.get("source_url") or "",
            "confidence": float(contact.get("confidence") or 0),
            "alternates_json": json.dumps(alternates or [], sort_keys=True, separators=(",", ":"))}


class Enrichment:
    """Website alternates for registry counterparties of active cases, scraped once and cached on disk.
    The registered phone and email stay primary (PRD R2); scraped values that differ are alternates."""

    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.getenv("SIM_DATA_DIR", "data")) / "enrichment.json"
        try:
            self.cache: dict[str, list] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.cache = {}
        self.running: set[str] = set()

    @staticmethod
    def available() -> bool:
        return bool(os.getenv("APIFY_TOKEN")) and os.getenv("MOCK_INTEGRATIONS", "0") != "1"

    def alternates(self, company_id: str) -> list:
        return self.cache.get(company_id, [])

    def wanted(self, company: dict) -> bool:
        return (self.available() and company.get("source") in ENRICHABLE_SOURCES
                and company["id"] not in self.cache and company["id"] not in self.running)

    async def scrape(self, company: dict, kind: str) -> list | None:
        self.running.add(company["id"])
        try:
            found = await asyncio.to_thread(tools.enrich_contact, company["id"], ENRICH_ROLE.get(kind, "dispatch"))
        finally:
            self.running.discard(company["id"])
        if "error" in found:
            log.warning("enrichment of %s failed: %s", company["id"], found["error"])
            return None
        self.cache[company["id"]] = found.get("alternates") or []
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps(self.cache, sort_keys=True))
        temporary.replace(self.path)
        return self.cache[company["id"]]


DIRECTORY_REDUCERS = {"upsert_company", "upsert_contact"}
SENT_DIRECTORY_WRITES_MAX = 20_000


class Mirror:
    def __init__(self, client: SpacetimeClient | None = None, enrichment: Enrichment | None = None):
        self.client = client or SpacetimeClient(actor="ingest")
        self.enrichment = enrichment or Enrichment()
        self.background: set[asyncio.Task] = set()
        # Directory write IDs this process has already had acknowledged. Every case re-mirrors its carrier and
        # shipper; the module treats an identical write ID as a no-op, but under load the round trips were most of
        # ingest's time per case. Case and invoice writes are never skipped, so a retry still resends them.
        self._sent_directory: dict[str, None] = {}

    @property
    def disabled(self) -> bool:
        return self.client.disabled

    async def write(self, reducer: str, key: str, payload: dict) -> None:
        identifier = write_id(reducer, key, payload)
        directory_write = reducer in DIRECTORY_REDUCERS
        if directory_write and identifier in self._sent_directory:
            return
        await self.client.mutate(reducer, {"write_id": identifier, **payload})
        if directory_write:
            self._sent_directory[identifier] = None
            if len(self._sent_directory) > SENT_DIRECTORY_WRITES_MAX:
                del self._sent_directory[next(iter(self._sent_directory))]

    async def company(self, dataset: dict, company_id: str, kind: str, party: dict | None = None) -> None:
        """A directory company and its contacts; a company missing from the directory is mirrored from the
        case's own record, without a source, so no outreach can treat it as verified."""
        company = dataset.get("companies", {}).get(company_id) or {**(party or {}), "id": company_id}
        await self.write("upsert_company", company_id, company_payload(company, kind))
        contacts = dataset.get("contacts", {}).get(company_id, [])
        for contact in contacts:
            if payload := contact_payload(contact, self.enrichment.alternates(company_id)):
                await self.write("upsert_contact", contact["id"], payload)
        if contacts and self.enrichment.wanted(company):
            task = asyncio.create_task(self._enrich(company, kind, contacts))
            self.background.add(task)
            task.add_done_callback(self.background.discard)

    async def _enrich(self, company: dict, kind: str, contacts: list[dict]) -> None:
        alternates = await self.enrichment.scrape(company, kind)
        if not alternates:
            return
        try:
            for contact in contacts:
                if payload := contact_payload(contact, alternates):
                    await self.write("upsert_contact", contact["id"], payload)
        except SpacetimeError as error:
            # The next case or directory sync for this company carries the cached alternates again.
            log.warning("enriched contacts for %s not mirrored yet (%s)", company["id"], type(error).__name__)

    async def case(self, dataset: dict, case_id: str, event_payload: dict | None = None,
                   routed_priority: str | None = None) -> None:
        case = dataset.get("cases", {}).get(case_id)
        if case is None:
            # Routed before ingest knew its load (for example a POD for an untendered load).
            case = {"load_id": (event_payload or {}).get("load_id")}
        await self.write("open_case", case_id, case_payload(case_id, case, routed_priority))
        if invoice := invoice_payload(case_id, case):
            await self.write("upsert_invoice", invoice["id"], invoice)
        for kind, key in (("carrier", "carrier_id"), ("shipper", "shipper_id")):
            if case.get(key):
                await self.company(dataset, case[key], kind, case.get(kind))

    async def cases(self, dataset: dict, events: list[dict]) -> None:
        payloads, priorities = {}, {}
        for event in events:
            payload = event.get("payload") or {}
            case_id = event.get("case_id") or payload.get("case_id")
            payloads.setdefault(case_id, payload)
            if event.get("type") in ROUTES:
                priorities.setdefault(case_id, event.get("priority"))
        for case_id in routed_case_ids(events):
            await self.case(dataset, case_id, payloads.get(case_id), priorities.get(case_id))

    async def directory(self, companies: dict, contacts: dict, kinds: dict[str, str]) -> int:
        """Push every directory company and contact; returns how many companies failed."""
        gate = asyncio.Semaphore(DIRECTORY_CONCURRENCY)

        async def one(company_id: str) -> bool:
            async with gate:
                try:
                    company = companies[company_id]
                    await self.write("upsert_company", company_id, company_payload(company, kinds[company_id]))
                    for contact in contacts.get(company_id, []):
                        if payload := contact_payload(contact, self.enrichment.alternates(company_id)):
                            await self.write("upsert_contact", contact["id"], payload)
                    return True
                except SpacetimeError as error:
                    log.warning("directory mirror of %s failed (%s)", company_id, type(error).__name__)
                    return False

        results = await asyncio.gather(*(one(company_id) for company_id in companies))
        return results.count(False)


def directory_kinds(world: dict) -> dict[str, str]:
    kinds = {record["id"]: "carrier" for record in world.get("carriers", []) if record.get("id")}
    kinds.update({record["id"]: "shipper" for record in world.get("shippers", []) if record.get("id")})
    return kinds


async def sync_directory(mirror: Mirror, retry_s: float = 30) -> None:
    """Startup and post-`make world` directory push, retried until every company lands."""
    while True:
        world = directory.load()
        companies, contacts = source_export.build_directory(world)
        failed = await mirror.directory(companies, contacts, directory_kinds(world))
        if not failed:
            log.info("mirrored %d directory companies into SpacetimeDB", len(companies))
            return
        log.warning("%d of %d directory companies not mirrored; retrying in %ss", failed, len(companies), retry_s)
        await asyncio.sleep(retry_s)
