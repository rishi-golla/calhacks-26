import asyncio
import json
import random
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pypdf import PdfReader

from agents.carrier_ap.tools import three_way_match
from common import harness_client
from ingest.documents import extract_invoice_rules, read_email
from ingest.main import connect, handle
from integrations import edgar, fmcsa
from sim import docs, generator
from sim.generator import SCENARIOS
from sim.world import price_lane
from tests.test_w4_data import CARRIER_ROW, SEC_SUB


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_FILE", str(tmp_path / "world.json"))
    monkeypatch.setenv("SIM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPS_DB", str(tmp_path / "ops.db"))
    monkeypatch.setenv("MOCK_INTEGRATIONS", "0")
    monkeypatch.setenv("HARNESS_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setattr(harness_client, "HARNESS_URL", "http://127.0.0.1:1")
    carrier, shipper = fmcsa.normalize(CARRIER_ROW), edgar.normalize(SEC_SUB)
    consignee = {**shipper, "id": "S-99", "cik": 99, "name": "CONSIGNEE DC", "lat": 34.0, "lon": -118.2}
    carrier.update(lat=33.95, lon=-118.17, terms="Net 30")
    shipper.update(lat=33.76, lon=-84.39, terms="Net 30",
                   directory={"phone": shipper["phone"], "source": shipper["source"]})
    result = {"fsc_per_mile": 0.433, "carriers": [carrier], "shippers": [shipper, consignee], "lanes": [{
        "id": "LN-1", "shipper_id": shipper["id"], "carrier_id": carrier["id"],
        "consignee_id": consignee["id"], "origin": "Atlanta, GA", "destination": "South Gate, CA",
        **price_lane(2210, random.Random(1)), "equipment": "53' Reefer",
        "commodity": "Packaged food, palletized", "nmfc_class": 70, "temp_f": 34,
        "free_time_min": 120, "detention_rate_hr": 75.0, "transit_hours": 64.0,
        "accessorials": {"lumper": "at cost", "layover": 250.0, "tonu": 150.0, "stop_off": 75.0}}]}
    (tmp_path / "world.json").write_text(json.dumps(result))
    return result


def _run(world, scenario, seed=3):
    rnd = random.Random(seed)
    load = generator.new_load(world, rnd)
    t0 = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
    steps = generator.lifecycle(load, scenario, rnd, t0=t0)
    return generator, load, [event for step in steps for event in step.make()]


def _ingest(events, db):
    async def process():
        result = []
        for event in events:
            result.extend(await handle(event, db))
        return result

    return asyncio.run(process())


def _invoice_sidecar(generator, events, invoice):
    email_event = next(event for event in events
                       if event["type"] == "email.received"
                       and event["payload"]["mail_id"] == invoice["source_email"])
    mail = read_email(generator.data_dir() / "mail" / f"{invoice['source_email']}.eml")
    invoice_filename = next(name for name in email_event["payload"]["attachments"]
                            if Path(name).stem.startswith("INV-"))
    sidecar = generator.data_dir() / "docs" / f"{Path(invoice_filename).stem}.json"
    return json.loads(sidecar.read_text()), mail


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_ingestion_extracts_and_routes_raw_scenario(world, scenario):
    generator, load, events = _run(world, scenario)
    db = connect()
    try:
        derived = _ingest(events, db)
    finally:
        db.close()

    invoices = [event["payload"] for event in derived if event["type"] == "carrier_invoice.received"]
    expected_count = 2 if scenario in ("duplicate_invoice", "duplicate_renumbered") else 1
    assert len(invoices) == expected_count
    delivery = next(event["payload"]["dwell_min"] for event in events
                    if event["type"] == "gps.ping" and event["payload"].get("milestone") == "delivery.departed")
    for invoice in invoices:
        sidecar, mail = _invoice_sidecar(generator, events, invoice)
        assert invoice["invoice_amount"] == sidecar["total"]
        assert [item["amount"] for item in invoice["line_items"]] == [
            item["amount"] for item in sidecar["line_items"]]
        assert invoice["contract_rate"] == load["contract_rate"]
        detention = next((re.search(r"(\d+(?:\.\d+)?)h", item["desc"], re.IGNORECASE)
                          for item in sidecar["line_items"]
                          if re.search(r"\bdetention\b", item["desc"], re.IGNORECASE)), None)
        assert invoice["detention_hours_claimed"] == (float(detention.group(1)) if detention else 0)
        assert invoice["pod_signed"] is (scenario != "pod_unsigned")
        accessorial = any(re.search(r"\b(detention|lumper|layover|stop[- ]off|tonu|driver assist)\b",
                                    item["desc"], re.IGNORECASE)
                          for item in sidecar["line_items"])
        has_receipt = any(re.search(r"DRIVER CHECK-IN|LUMPER RECEIPT", attachment["text"], re.IGNORECASE)
                          for attachment in mail["attachments"])
        assert invoice["receipt_attached"] is (True if has_receipt else False if accessorial else None)
        assert invoice["remit_to"]["name"] == sidecar["remit_to"]["name"]
        assert invoice["gps_dwell_min"] == delivery
        assert invoice["free_time_min"] == load["free_time_min"]
        assert invoice["carrier"]["usdot"] == sidecar["carrier"]["usdot"]
        assert invoice["extraction"]["method"] == "rules"

    if scenario == "duplicate_invoice":
        assert invoices[0]["invoice_id"] == invoices[1]["invoice_id"]
    if scenario == "duplicate_renumbered":
        assert invoices[1]["case_id"].endswith("-R")
        assert invoices[0]["bol"] == invoices[1]["bol"]
        duplicate = three_way_match({**invoices[1], "duplicate_of": invoices[0]["invoice_id"]})
        assert "P-007" in duplicate["policy_ids"]

    if scenario in ("bank_change", "factoring_unverified"):
        change = next(event["payload"] for event in derived if event["type"] == "bank_change.requested")
        expected_via = "email" if scenario == "bank_change" else "invoice_remit_to"
        assert change["requested_via"] == expected_via
        mail_event = next(event for event in events
                          if event["type"] == "email.received"
                          and event["payload"]["mail_id"] == change["source_email"])
        bank_mail = read_email(generator.data_dir() / "mail" / f"{mail_event['payload']['mail_id']}.eml")
        account = re.search(r"Account \*{4}(\d{4})", bank_mail["body"])
        assert account and change["new_account_last4"] == account.group(1)

    payment = next(event["payload"] for event in derived if event["type"] == "shipper_payment.received")
    assert payment["chase"] == (scenario == "shipper_short_pay")
    assert payment["deduction_reason"] == ("rate dispute" if scenario == "shipper_short_pay" else None)

    held_scenarios = set(SCENARIOS) - {
        "clean", "duplicate_invoice", "duplicate_renumbered", "shipper_short_pay", "bank_change",
        "detention_supported",
    }
    if scenario in held_scenarios:
        assert not three_way_match(invoices[0])["matched"]


@pytest.mark.parametrize("style", range(3))
def test_invoice_rules_parse_all_carrier_layouts(world, style):
    usdot = next(str(value) for value in range(390000, 391000)
                 if docs.carrier_style(str(value)) == style)
    carrier = {**world["carriers"][0], "usdot": usdot}
    invoice_number = f"LAYOUT-{style}"
    doc_id = f"INV-{carrier['usdot']}-{invoice_number}"
    docs.write(doc_id, "carrier_invoice", {
        "load_id": f"LOAD-{style}", "bol": f"BOL-{style}", "pro": f"PRO-{style}",
        "invoice_number": invoice_number, "date": "2026-01-15", "due": "2026-02-14", "terms": "Net 30",
        "origin": "Atlanta, GA", "destination": "South Gate, CA",
        "carrier": carrier, "shipper": world["shippers"][0],
        "line_items": [{"desc": "Linehaul", "qty": 1, "rate": 200, "amount": 200},
                       {"desc": "Fuel surcharge", "qty": 5, "rate": 1.25, "amount": 6.25}],
        "total": 206.25,
        "remit_to": {"name": carrier["name"], "routing_last4": "1234", "account_last4": "5678"},
    })
    pdf = docs.docs_dir() / f"{doc_id}.pdf"
    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)
    fields = extract_invoice_rules(text)
    assert fields["invoice_number"] == invoice_number
    assert fields["load_id"] == f"LOAD-{style}"
    assert fields["bol"] == f"BOL-{style}"
    assert fields["usdot"] == carrier["usdot"]
    assert fields["invoice_date"] == "2026-01-15"
    assert fields["terms"] == "Net 30"
    assert fields["remit_to_name"] == carrier["name"]
    assert [item["amount"] for item in fields["line_items"]] == [200.0, 6.25]
    assert fields["total"] == 206.25


def test_unsupported_detention_without_receipt_is_held_over_contract(world):
    world["lanes"][0].update(contract_rate=8000, linehaul=8000)
    _, _, events = _run(world, "detention_unsupported")
    db = connect()
    try:
        derived = _ingest(events, db)
    finally:
        db.close()
    invoice = next(event["payload"] for event in derived if event["type"] == "carrier_invoice.received")
    match = three_way_match(invoice)
    assert not match["matched"] and "P-014" in match["policy_ids"]
