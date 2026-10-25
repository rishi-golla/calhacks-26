"""Mirror cases, carrier invoices, the counterparty directory and the source dataset into SpacetimeDB (role `ingest`).

Payloads come from the same source export agents read (ingest.source_export), so IDs and amounts agree.
A routed case is mirrored before its event reaches `events`: its source records (`case:`, the carrier's and
shipper's `company:` and `contacts:`, `dataset:meta`) and the carrier and shipper rows with their contacts,
concurrently, then open_case, then its carrier invoice. The whole directory is pushed at startup and again
whenever world.json changes, and every flush of the source export mirrors the source records that changed.

Write IDs are `ingest:<reducer>:<id>:<sha256(payload)[:16]>`: replaying the raw stream resends identical writes,
which the module treats as no-ops, and a changed record gets a new write ID. A record that changes and then
changes back reuses its old write ID and is not rewritten; nothing in the simulator does that. Source records use
`ingest:upsert_source_record:<id>:<digest[:16]>`, where the digest is SHA-256 of `record_json` exactly as sent, and
are skipped while SpacetimeDB holds that digest (`SourceDigests`).

SPACETIME_DISABLED=1 (the default) turns the mirror off.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Iterable
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from time import monotonic

from common.spacetime_client import (
    SpacetimeClient,
    SpacetimeError,
    SpacetimeHTTPError,
    SpacetimeTransportError,
)
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
SOURCE_REDUCER = "upsert_source_record"
DATASET_META = "dataset:meta"
SOURCE_MAPS = (("cases", "case"), ("companies", "company"), ("contacts", "contacts"))
# The module refuses a reducer payload longer than 256 KiB (spacetime/module/spacetimedb/src/policy.ts).
MAX_PAYLOAD_CHARS = 262144
# SpacetimeDB 2.x answers a reducer that refused the write with HTTP 530; the client files it under `server`.
REDUCER_REFUSED = 530
# Filed under `rejected` by the client, but the same write can land later: 402 is Maincloud's exhausted energy
# budget, 408 a request timeout.
TRANSIENT_STATUSES = {402, 408}
RETRY_CAP_S = 30.0


class RecordTooLarge(SpacetimeError):
    """A source record whose upsert_source_record payload exceeds the module's cap; refused without a request."""


def refused(error: BaseException) -> bool:
    """The module refused this exact write (or would, for an oversized record): resending it cannot succeed."""
    return isinstance(error, RecordTooLarge) or (isinstance(error, SpacetimeHTTPError) and (
        (error.kind == "rejected" and error.status_code not in TRANSIENT_STATUSES)
        or error.status_code == REDUCER_REFUSED))


def transient(error: BaseException) -> bool:
    """An outage or overload: the same write can land later."""
    return isinstance(error, SpacetimeTransportError) or (isinstance(error, SpacetimeHTTPError) and (
        (error.kind in ("server", "rate_limited") and error.status_code != REDUCER_REFUSED)
        or error.status_code in TRANSIENT_STATUSES))


def describe(error: BaseException) -> str:
    """Error class and status, never a response body or payload."""
    text = f"{type(error).__name__}: {error}"
    return f"{text} writing {error.write_id}" if getattr(error, "write_id", None) else text


class Throttle:
    """`due()` is true at most once per `seconds`, for warnings that would otherwise repeat every retry."""

    def __init__(self, seconds: float = 60):
        self.seconds = seconds
        self.last: float | None = None

    def due(self) -> bool:
        now = monotonic()
        if self.last is not None and now - self.last < self.seconds:
            return False
        self.last = now
        return True

    def reset(self) -> None:
        self.last = None


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
        subject, what = shipper, ("unapplied payment" if case.get("unapplied_deposits")
                                  else "short pay" if _short_paid(case) > 0 else "payment")
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


def source_record(record_id: str, record) -> dict:
    """The upsert_source_record payload for one record of the source export. `record_json` is the exact string
    hashed and sent; the module recomputes the digest over it and never re-serializes it."""
    record_json = json.dumps(record, default=str, sort_keys=True, separators=(",", ":"))
    return {"id": record_id, "kind": record_id.split(":", 1)[0], "record_json": record_json,
            "digest": hashlib.sha256(record_json.encode()).hexdigest()}


def dataset_meta(dataset: dict) -> dict:
    """The export's dataset-level keys (schema, generated_at, source_kind, provenance flags)."""
    return {key: value for key, value in dataset.items() if key not in dict(SOURCE_MAPS)}


def case_records(dataset: dict, case_ids: Iterable[str]) -> list[dict]:
    """What agents read for these cases: `case:<id>`, `company:` and `contacts:` for its carrier and shipper, and
    `dataset:meta`. Only records the export holds: a counterparty missing from it stays missing, as in the file."""
    records: dict[str, dict] = {}
    for case_id in case_ids:
        case = dataset.get("cases", {}).get(case_id)
        if case is None:
            continue
        records[f"case:{case_id}"] = source_record(f"case:{case_id}", case)
        for key in ("carrier_id", "shipper_id"):
            company_id = case.get(key)
            for name, kind in SOURCE_MAPS[1:]:
                if company_id and (record := dataset.get(name, {}).get(company_id)) is not None:
                    records[f"{kind}:{company_id}"] = source_record(f"{kind}:{company_id}", record)
    records[DATASET_META] = source_record(DATASET_META, dataset_meta(dataset))
    return list(records.values())


def dataset_records(dataset: dict) -> list[dict]:
    records = [source_record(f"{kind}:{key}", record)
               for name, kind in SOURCE_MAPS for key, record in dataset.get(name, {}).items()]
    return [*records, source_record(DATASET_META, dataset_meta(dataset))]


async def _settle(writes: Iterable[Awaitable]) -> None:
    """Run the writes concurrently; once every one has settled, raise the first failure."""
    results = await asyncio.gather(*writes, return_exceptions=True)
    if error := next((result for result in results if isinstance(result, BaseException)), None):
        raise error


class SourceDigests:
    """The digest SpacetimeDB holds for each source record this ingest wrote, persisted at
    `$SIM_DATA_DIR/source_mirror.json` as `{"target": "<url>/<database>", "digests": {id: digest}}` and rewritten
    atomically after every write that lands. A cache kept for another database is ignored, so moving ingest to a
    new database resends every record once instead of skipping them."""

    def __init__(self, target: str, path: Path | None = None):
        self.target = target
        self.path = path or Path(os.getenv("SIM_DATA_DIR", "data")) / "source_mirror.json"
        try:
            stored = json.loads(self.path.read_text())
            digests = stored["digests"] if stored.get("target") == target else {}
            self.digests: dict[str, str] = {str(key): str(value) for key, value in digests.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.digests = {}

    def current(self, record: dict) -> bool:
        return self.digests.get(record["id"]) == record["digest"]

    def stored(self, record: dict) -> None:
        self.digests[record["id"]] = record["digest"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps({"target": self.target, "digests": self.digests}, sort_keys=True))
        temporary.replace(self.path)


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
    def __init__(self, client: SpacetimeClient | None = None, enrichment: Enrichment | None = None,
                 digests: SourceDigests | None = None):
        self.client = client or SpacetimeClient(actor="ingest")
        self.enrichment = enrichment or Enrichment()
        self.digests = digests or SourceDigests(f"{self.client.url}/{self.client.database}")
        # Bounds the concurrent requests of one case's writes and of a source-dataset pass.
        self.gate = asyncio.Semaphore(DIRECTORY_CONCURRENCY)
        self.background: set[asyncio.Task] = set()
        # Directory write IDs this process has already had acknowledged. Every case re-mirrors its carrier and
        # shipper; the module treats an identical write ID as a no-op, but under load the round trips were most of
        # ingest's time per case. Case and invoice writes are never skipped, so a retry still resends them.
        self._sent_directory: dict[str, None] = {}
        self.backlog: dict[str, dict] = {}  # source records of the last export not mirrored yet
        self.rejected: dict[str, str] = {}  # id -> digest the module refused; resent once the record changes
        self.retry_s, self.retry_at = 1.0, 0.0
        self.warn = Throttle()

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

    async def put_source(self, record: dict, stop: list | None = None) -> bool:
        """Write one source record unless SpacetimeDB already holds its digest; False when skipped. A write that
        lands updates the digest cache. `stop` (a list that is nonempty once the pass should halt) is checked once
        the gate admits the write, so a halted pass sends nothing more."""
        if self.digests.current(record):
            return False
        document = {"write_id": f"ingest:{SOURCE_REDUCER}:{record['id']}:{record['digest'][:16]}", **record}
        if len(json.dumps(document, separators=(",", ":"))) > MAX_PAYLOAD_CHARS:
            error = RecordTooLarge(f"source record exceeds the {MAX_PAYLOAD_CHARS // 1024} KiB payload cap")
            error.write_id = document["write_id"]
            raise error
        async with self.gate:
            if stop:
                return False
            await self.client.mutate(SOURCE_REDUCER, document)
        self.digests.stored(record)
        return True

    async def source_records(self, dataset: dict, case_ids: Iterable[str]) -> None:
        """The routed cases' source records (see `case_records`), concurrently under the gate. Raises the first
        failure once every write has settled: the event must not be published ahead of its source truth."""
        records = case_records(dataset, case_ids)
        for record in records:
            # This export is newer than the one the backlog came from; never let the backlog overwrite it.
            self.backlog.pop(record["id"], None)
        await _settle(self.put_source(record) for record in records)

    async def sync_sources(self, dataset: dict | None = None) -> None:
        """Mirror every record of a fresh export whose digest SpacetimeDB does not hold, under the gate. Called
        beside each export flush with the dataset it wrote, or with None to retry what is left. Never raises:
        routed cases already carry their records (`source_records`); this keeps the rest current. A record the
        module refuses is skipped until it changes; any other failure halts the pass, keeps the rest, and the
        next call retries with backoff (1 s to 30 s)."""
        if dataset is not None:
            self.backlog = {record["id"]: record for record in dataset_records(dataset)
                            if not self.digests.current(record) and self.rejected.get(record["id"]) != record["digest"]}
        if not self.backlog or monotonic() < self.retry_at:
            return
        pending = len(self.backlog)
        if pending >= 100:
            log.info("mirroring %d source records into SpacetimeDB", pending)
        stop: list[BaseException] = []
        rejected: list[tuple[str, BaseException]] = []
        written = 0

        async def one(record: dict) -> None:
            nonlocal written
            try:
                written += await self.put_source(record, stop)
            except Exception as error:  # noqa: BLE001 - a pass is best effort; failures are reported below
                if not refused(error):
                    stop.append(error)
                    return
                self.rejected[record["id"]] = record["digest"]
                rejected.append((record["id"], error))
            else:
                if not self.digests.current(record):
                    return  # the pass halted before this one was sent
            if self.backlog.get(record["id"]) is record:
                del self.backlog[record["id"]]

        await asyncio.gather(*(one(record) for record in list(self.backlog.values())))
        if rejected:
            log.warning("SpacetimeDB refused %d source records; not resent until they change (first %s: %s)",
                        len(rejected), rejected[0][0], describe(rejected[0][1]))
        if stop:
            self.retry_at = monotonic() + self.retry_s
            if self.warn.due():
                log.warning("%d of %d source records not mirrored yet (%s); retrying in %.0fs",
                            len(self.backlog), pending, describe(stop[0]), self.retry_s)
            self.retry_s = min(self.retry_s * 2, RETRY_CAP_S)
            return
        self.retry_s, self.retry_at = 1.0, 0.0
        self.warn.reset()
        if written:
            log.log(logging.INFO if written >= 100 else logging.DEBUG, "mirrored %d source records", written)

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

    async def _gated_company(self, dataset: dict, company_id: str, kind: str, party: dict | None) -> None:
        async with self.gate:
            await self.company(dataset, company_id, kind, party)

    async def case(self, dataset: dict, case_id: str, event_payload: dict | None = None,
                   routed_priority: str | None = None) -> None:
        """Source records and the carrier and shipper rows concurrently, then open_case, then the invoice."""
        case = dataset.get("cases", {}).get(case_id)
        if case is None:
            # Routed before ingest knew its load (for example a POD for an untendered load).
            case = {"load_id": (event_payload or {}).get("load_id")}
        parties = [(kind, case[key]) for kind, key in (("carrier", "carrier_id"), ("shipper", "shipper_id"))
                   if case.get(key)]
        await _settle([self.source_records(dataset, [case_id]),
                       *(self._gated_company(dataset, company_id, kind, case.get(kind))
                         for kind, company_id in parties)])
        await self.write("open_case", case_id, case_payload(case_id, case, routed_priority))
        if invoice := invoice_payload(case_id, case):
            await self.write("upsert_invoice", invoice["id"], invoice)

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
