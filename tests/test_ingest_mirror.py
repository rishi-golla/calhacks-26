"""Ingest mirrors cases, invoices and the directory into SpacetimeDB before routing (W4 task 2)."""
import json
import re
from pathlib import Path

import httpx
import pytest

from common.spacetime_client import SpacetimeClient
from ingest import main as ingest_main
from ingest import mirror as spacetime_mirror
from ingest import source_export
from ingest.main import connect
from integrations import directory, edgar, fmcsa, tools
from sim import generator
from sim.world import offline_world
from tests import test_ingest
from tests.test_ingest import _run
from tests.test_w4_data import CARRIER_ROW, SEC_SUB

world = test_ingest.world
MODULE = Path(__file__).parents[1] / "spacetime/module/spacetimedb/src/operations.ts"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    import os
    for name in os.environ:
        if name.startswith("SPACETIME_"):
            monkeypatch.delenv(name)


class FakeSpacetime:
    """The module's write_id rules: identical replays are no-ops, reused IDs with new content are rejected,
    an invoice's case is immutable. `fail` makes the next call to a reducer fail like a server error."""

    def __init__(self, log: list | None = None):
        self.log = log if log is not None else []
        self.receipts: dict[str, dict] = {}
        self.rows: dict[tuple[str, str], dict] = {}
        self.rejected: list[tuple[str, dict]] = []
        self.fail: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        reducer = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(json.loads(request.content)[0])
        if reducer in self.fail:
            self.fail.discard(reducer)
            return httpx.Response(500)
        if (seen := self.receipts.get(payload["write_id"])) is not None:
            if seen != payload:
                self.rejected.append((reducer, payload))
                return httpx.Response(400)
            self.log.append(("noop", reducer, payload))
            return httpx.Response(200)
        key = payload.get("case_id") if reducer == "open_case" else payload["id"]
        previous = self.rows.get((reducer, key))
        if reducer == "upsert_invoice" and previous and previous["case_id"] != payload["case_id"]:
            self.rejected.append((reducer, payload))
            return httpx.Response(400)
        self.receipts[payload["write_id"]] = payload
        self.rows[(reducer, key)] = payload
        self.log.append(("write", reducer, payload))
        return httpx.Response(200)

    def mirror(self, enrichment=None) -> spacetime_mirror.Mirror:
        client = SpacetimeClient(actor="ingest", disabled=False, transport=httpx.MockTransport(self.handler))
        return spacetime_mirror.Mirror(client, enrichment)

    def writes(self, reducer: str) -> list[dict]:
        return [payload for kind, name, payload in self.log if kind == "write" and name == reducer]

    def row(self, reducer: str, key: str) -> dict:
        return self.rows[(reducer, key)]


class FakeRedis:
    def __init__(self, log: list):
        self.log = log
        self.acked = []

    async def xadd(self, stream, fields, **_kwargs):
        self.log.append(("xadd", stream, json.loads(fields["json"])))

    async def xack(self, _stream, _group, entry_id):
        self.acked.append(entry_id)


async def _process(events, db, spacetime, redis, exporter=None, mirror=None):
    mirror = mirror or spacetime.mirror()
    results = []
    for index, event in enumerate(events):
        results.append(await ingest_main._process_entry(redis, db, f"{index}-0", {"json": json.dumps(event)},
                                                        exporter, mirror))
    return results


async def _hero_events(world, tmp_path):
    directory.save(offline_world())  # a current-schema world, so seeding never rebuilds from the network
    await generator.seed_hero(world)
    return [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]


async def test_hero_cases_invoice_and_counterparties_are_mirrored_before_routing(world, tmp_path):
    events = await _hero_events(world, tmp_path)
    log: list = []
    spacetime = FakeSpacetime(log)
    db = connect()
    try:
        assert all(await _process(events, db, spacetime, FakeRedis(log),
                                  source_export.Exporter(tmp_path / "source.json")))
    finally:
        db.close()
    assert not spacetime.rejected

    # The POD opens the case before the carrier bills; the invoice then refreshes it.
    opened = spacetime.writes("open_case")
    pod_open, invoice_open = [payload for payload in opened if payload["case_id"] == "CASE-4821"]
    assert pod_open["title"] == "Sierra Haul LLC delivery on LOAD-4821" and pod_open["exposure_usd"] == 0
    assert invoice_open == {**invoice_open, "load_id": "LOAD-4821", "priority": "P1", "exposure_usd": 300.0,
                            "title": "Sierra Haul LLC detention on LOAD-4821"}
    invoice = spacetime.row("upsert_invoice", next(payload["id"] for payload in spacetime.writes("upsert_invoice")
                                                   if payload["case_id"] == "CASE-4821"))
    assert invoice == {**invoice, "carrier_id": "C-311", "load_id": "LOAD-4821", "amount": 2150.0,
                       "contract_amount": 1850.0}
    assert "paid_amount" not in invoice and invoice["issued_at"].startswith("20")
    bank = spacetime.row("open_case", "CASE-C-77-BANK")
    assert bank["priority"] == "P0" and bank["exposure_usd"] == 9400.0 and "load_id" not in bank
    assert bank["title"] == "Rapid Ridge Transport bank change"
    receivable = spacetime.row("open_case", "CASE-4821-AR")
    assert receivable["title"] == "Bay Produce Co payment on LOAD-4821" and receivable["exposure_usd"] == 0
    # Receivable and bank-change cases refer to invoices they do not own; only payable cases mirror one.
    assert {payload["case_id"] for payload in spacetime.writes("upsert_invoice")} == {"CASE-4821", "CASE-4877"}
    for company_id, kind in (("C-311", "carrier"), ("S-07", "shipper"), ("C-77", "carrier")):
        company = spacetime.row("upsert_company", company_id)
        contact = spacetime.row("upsert_contact", f"CT-{company_id}")
        assert company["kind"] == kind and company["phone"] and company["source_url"] == \
            f"fixture://directory/{company_id}"
        assert contact["company_id"] == company_id and contact["source"] == "fixture"
        assert contact["confidence"] == 1.0 and contact["phone"] == company["phone"]

    # Every routed event finds its case, its invoice and its counterparties already written.
    written: set[tuple[str, str]] = set()
    for entry in log:
        if entry[0] in ("write", "noop"):
            payload = entry[2]
            written.add((entry[1], payload.get("case_id") if entry[1] == "open_case" else payload["id"]))
        elif entry[0] == "xadd" and entry[2]["type"] in spacetime_mirror.ROUTES:
            payload = entry[2]["payload"]
            if not payload.get("case_id"):
                continue
            assert ("open_case", payload["case_id"]) in written, entry[2]["type"]
            for key in ("carrier_id", "shipper_id"):
                if payload.get(key):
                    assert ("upsert_company", payload[key]) in written
            if entry[2]["type"] == "carrier_invoice.received":
                assert ("upsert_invoice", payload["invoice_id"]) in written


async def test_replaying_the_raw_stream_creates_no_duplicates_or_rejections(world):
    _gen, load, events = _run(world, "detention_unsupported")
    spacetime = FakeSpacetime()
    db = connect()
    try:
        assert all(await _process(events, db, spacetime, FakeRedis([])))
        first = {key: dict(row) for key, row in spacetime.rows.items()}
        assert all(await _process(events, db, spacetime, FakeRedis([])))
    finally:
        db.close()
    assert not spacetime.rejected and spacetime.rows == first
    case = spacetime.row("open_case", load["case_id"])
    assert case["priority"] == "P1" and case["exposure_usd"] > 0 and case["title"].endswith(load["load_id"])
    assert spacetime.row("upsert_contact", f"CT-{load['carrier']['id']}")["source"] == "fmcsa"
    assert spacetime.row("upsert_contact", f"CT-{load['shipper']['id']}")["source"] == "sec_edgar"


async def test_failed_write_leaves_the_entry_unacked_and_the_retry_resends_identical_writes(world):
    _gen, load, events = _run(world, "clean")
    invoice_mail = next(index for index, event in enumerate(events) if event["type"] == "email.received"
                        and any(str(name).startswith("INV-") for name in event["payload"]["attachments"]))
    log: list = []
    spacetime = FakeSpacetime(log)
    redis = FakeRedis(log)
    db = connect()
    try:
        assert all(await _process(events[:invoice_mail], db, spacetime, redis))
        entry = {"json": json.dumps(events[invoice_mail])}
        mirror = spacetime.mirror()
        spacetime.fail.add("upsert_invoice")
        start = len(log)
        assert await ingest_main._process_entry(redis, db, "retry-0", entry, None, mirror) is False
        failed = log[start:]
        assert "retry-0" not in redis.acked and not any(item[0] == "xadd" for item in failed)
        start = len(log)
        assert await ingest_main._process_entry(redis, db, "retry-0", entry, None, mirror) is True
        retried = log[start:]
    finally:
        db.close()
    assert redis.acked[-1] == "retry-0" and not spacetime.rejected
    # Writes that landed before the failure replay as no-ops with the same write_id and payload.
    assert [payload for _kind, _name, payload in failed] == \
        [payload for kind, _name, payload in retried if kind != "xadd"][:len(failed)]
    assert spacetime.row("upsert_invoice", next(iter(spacetime.writes("upsert_invoice")))["id"])["case_id"] == \
        load["case_id"]


async def test_mirror_is_off_by_default_and_ingest_still_routes(world):
    _gen, _load, events = _run(world, "clean")
    log: list = []
    mirror = spacetime_mirror.Mirror()
    assert mirror.disabled
    db = connect()
    try:
        assert all(await _process(events, db, FakeSpacetime(), FakeRedis(log), mirror=mirror))
    finally:
        db.close()
    assert any(item[0] == "xadd" and item[2]["type"] == "carrier_invoice.received" for item in log)


def test_every_directory_source_maps_to_a_contact_source_the_module_accepts():
    module_sources = set(re.findall(r"'(\w+)'", re.search(r"CONTACT_SOURCES = \[(.*?)\]",
                                                          MODULE.read_text()).group(1)))
    generated = offline_world()
    records = [*generated["carriers"], *generated["shippers"], fmcsa.normalize(CARRIER_ROW), edgar.normalize(SEC_SUB)]
    sources = {record["source"] for record in records} | {"apify_website"}
    assert sources == set(spacetime_mirror.CONTACT_SOURCES)
    assert set(spacetime_mirror.CONTACT_SOURCES.values()) <= module_sources
    assert spacetime_mirror.CONTACT_SOURCES["sec_edgar"] == "sec_edgar"
    assert spacetime_mirror.contact_payload({"id": "CT-X", "company_id": "X", "source": "unknown"}) is None


def test_exposure_is_exact_to_the_cent():
    case = {"load_id": "LOAD-1", "invoice_id": "INV-1", "invoice_amount": 2150.1, "contract_rate": 1850.0,
            "carrier": {"name": "Carrier"}}
    assert spacetime_mirror.exposure("CASE-1", case) == 300.1
    assert spacetime_mirror.exposure("CASE-1", {**case, "invoice_amount": 1800.0}) == 0
    receivable = {"shipper_invoice_amount": 1850.0, "deposits": [{"amount": 1000.0}, {"amount": 274.99}],
                  "shipper": {"name": "Shipper"}, "load_id": "LOAD-1"}
    assert spacetime_mirror.exposure("CASE-1-AR", receivable) == 575.01
    assert spacetime_mirror.title("CASE-1-AR", receivable) == "Shipper short pay on LOAD-1"


def test_write_ids_are_stable_and_change_with_content():
    payload = {"id": "C-1", "name": "Carrier"}
    assert spacetime_mirror.write_id("upsert_company", "C-1", payload) == \
        spacetime_mirror.write_id("upsert_company", "C-1", dict(reversed(payload.items())))
    assert spacetime_mirror.write_id("upsert_company", "C-1", payload) != \
        spacetime_mirror.write_id("upsert_company", "C-1", {**payload, "name": "Renamed"})


async def test_directory_sync_pushes_every_company_and_repeats_as_noops(world):
    spacetime = FakeSpacetime()
    mirror = spacetime.mirror()
    current = directory.load()
    current["shippers"][0]["website"] = "https://www.coca-colacompany.com"
    companies, contacts = source_export.build_directory(current)
    kinds = spacetime_mirror.directory_kinds(current)
    assert await mirror.directory(companies, contacts, kinds) == 0
    assert {payload["id"] for payload in spacetime.writes("upsert_company")} == set(companies)
    assert {payload["company_id"] for payload in spacetime.writes("upsert_contact")} == set(contacts)
    assert spacetime.row("upsert_company", current["shippers"][0]["id"])["website"] == \
        "https://www.coca-colacompany.com"
    before = len(spacetime.log)
    assert await mirror.directory(companies, contacts, kinds) == 0
    assert all(kind == "noop" for kind, _name, _payload in spacetime.log[before:])


async def test_enriched_alternates_join_the_registered_contact(world, tmp_path, monkeypatch):
    _gen, load, events = _run(world, "clean")
    monkeypatch.setenv("APIFY_TOKEN", "test-token")
    carrier_id = load["carrier"]["id"]
    alternates = [{"type": "phone", "value": "+13235550199", "source": "apify_website"}]
    calls = []

    def enrich_contact(company_id, role):
        calls.append((company_id, role))
        return {"phone": "+10000000000", "alternates": alternates if company_id == carrier_id else [],
                "conflict": company_id == carrier_id}

    monkeypatch.setattr(tools, "enrich_contact", enrich_contact)
    spacetime = FakeSpacetime()
    enrichment = spacetime_mirror.Enrichment(tmp_path / "enrichment.json")
    mirror = spacetime.mirror(enrichment)
    db = connect()
    try:
        assert all(await _process(events, db, spacetime, FakeRedis([]), mirror=mirror))
        for task in list(mirror.background):
            await task
    finally:
        db.close()
    contact = spacetime.row("upsert_contact", f"CT-{carrier_id}")
    assert json.loads(contact["alternates_json"]) == alternates
    assert contact["phone"] == directory.get(carrier_id)["phone"]  # the registration stays primary
    assert sorted(calls) == sorted({(carrier_id, "dispatch"), (load["shipper"]["id"], "accounts_payable")})
    assert json.loads((tmp_path / "enrichment.json").read_text())[carrier_id] == alternates
    assert not spacetime.rejected
