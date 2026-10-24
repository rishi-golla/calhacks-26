import asyncio
import json
import random
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader

from common import harness_client
from ingest.documents import classify, extract_invoice_rules, read_email
from ingest import main as ingest_main
from ingest.main import _invoice_email, connect, handle
from integrations import edgar, fmcsa
from sim import docs, generator
from sim import mail as sim_mail
from sim.generator import SCENARIOS
from sim.world import price_lane
from tests.test_w4_data import CARRIER_ROW, SEC_SUB


def test_ingest_group_starts_at_stream_beginning(world, monkeypatch):
    class StopReading(Exception):
        pass

    class FakeRedis:
        def __init__(self):
            self.group_args = None

        async def xgroup_create(self, stream, group, id, mkstream):
            self.group_args = (stream, group, id, mkstream)

        async def xreadgroup(self, *args, **kwargs):
            raise StopReading

    fake_redis = FakeRedis()
    db = connect()
    monkeypatch.setattr(ingest_main.redis, "from_url", lambda _url: fake_redis)
    monkeypatch.setattr(ingest_main, "connect", lambda: db)
    with pytest.raises(StopReading):
        asyncio.run(ingest_main.main())
    assert fake_redis.group_args == ("events", "ingest", "0", True)


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
    other_carrier = fmcsa.normalize({**CARRIER_ROW, "dot_number": "391315", "legal_name": "SAGE VALLEY CARRIERS LLC",
                                     "dba_name": "SAGE VALLEY", "phone": "(323) 555-1315",
                                     "email_address": "billing@sagevalley.example", "docket1": "231686"})
    consignee = {**shipper, "id": "S-99", "cik": 99, "name": "CONSIGNEE DC", "lat": 34.0, "lon": -118.2}
    carrier.update(lat=33.95, lon=-118.17, terms="Net 30")
    shipper.update(lat=33.76, lon=-84.39, terms="Net 30",
                   directory={"phone": shipper["phone"], "source": shipper["source"]})
    result = {"fsc_per_mile": 0.433, "carriers": [carrier, other_carrier],
              "shippers": [shipper, consignee], "lanes": [{
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


def _invoice_material(world, scenario="clean", seed=3):
    sim_generator, load, events = _run(world, scenario, seed)
    trigger = next(event for event in events if event["type"] == "email.received"
                   and any(Path(name).stem.startswith("INV-")
                           for name in event["payload"]["attachments"]))
    message = read_email(sim_generator.data_dir() / "mail" / f"{trigger['payload']['mail_id']}.eml")
    return sim_generator, load, events, trigger, message


def _store_tms(db, events):
    tms = [event for event in events if event["type"] == "load.tendered"
           or (event["type"] == "gps.ping" and event["payload"].get("milestone") == "delivery.departed")]
    _ingest(tms, db)
    db.commit()


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_ingestion_extracts_and_routes_raw_scenario(world, scenario):
    generator, load, events = _run(world, scenario)
    db = connect()
    try:
        derived = _ingest(events, db)
    finally:
        db.close()

    invoices = [event["payload"] for event in derived if event["type"] == "carrier_invoice.received"]
    assert all({"event_id", "type", "ts", "sim_ts", "case_hint", "priority", "payload", "source"} <= event.keys()
               for event in derived)
    assert all(event["source"] == "sim" and event["ts"].endswith("Z") and event["sim_ts"].endswith("Z")
               and "occurred_at" not in event for event in derived)
    if scenario == "double_brokering":
        review = next(event for event in derived if event["type"] == "document.extracted")
        assert not invoices and review["payload"]["status"] == "needs_review"
        assert review["payload"]["fields"]["mismatch"] == "usdot"
        return
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

    if scenario == "misapplied_payment":
        assert not any(event["type"] == "shipper_payment.received" for event in derived)
        assert any(event["type"] == "document.extracted" and event["payload"]["status"] == "needs_review"
                   for event in derived)
        return
    payment = next(event["payload"] for event in derived if event["type"] == "shipper_payment.received")
    assert payment["chase"] == (scenario == "shipper_short_pay")
    assert payment["deduction_reason"] == ("rate dispute" if scenario == "shipper_short_pay" else None)

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


def test_bank_offset_rolls_back_and_reemits_payment_on_redelivery(world):
    _, _, events = _run(world, "shipper_short_pay")
    bank_event = next(event for event in events if event["type"] == "bank.event")
    db = connect()
    try:
        _ingest(events[:events.index(bank_event)], db)
        db.commit()

        first = asyncio.run(handle(bank_event, db))
        first_payment = next(event["payload"] for event in first
                             if event["type"] == "shipper_payment.received")
        offset = db.execute("SELECT offset FROM bank_offsets WHERE file=?",
                            (bank_event["payload"]["file"],)).fetchone()
        assert offset and offset[0] > 0

        db.rollback()
        assert db.execute("SELECT offset FROM bank_offsets WHERE file=?",
                          (bank_event["payload"]["file"],)).fetchone() is None
        second = asyncio.run(handle(bank_event, db))
        second_payment = next(event["payload"] for event in second
                              if event["type"] == "shipper_payment.received")
        assert second_payment == first_payment
    finally:
        db.close()


def test_unclassified_pdf_email_emits_review_event(world):
    _, load, events = _run(world, "clean")
    pod_ref = next(event["payload"]["pod_ref"] for event in events if event["type"] == "pod.received")
    email = sim_mail.write("records@carrier.example", "ap@example.test", "Unclassified document",
                           "Please review the attached document.", datetime.now(UTC), [pod_ref])
    trigger = {"type": "email.received", "case_hint": load["case_id"],
               "payload": {"mail_id": email["mail_id"], "from": email["from"], "to": email["to"],
                           "subject": email["subject"], "sent_at": email["sent_at"],
                           "attachments": email["attachments"]}}
    db = connect()
    try:
        derived = asyncio.run(handle(trigger, db))
    finally:
        db.close()

    assert len(derived) == 1
    assert derived[0]["type"] == "document.extracted"
    assert derived[0]["payload"]["kind"] == "other"
    assert derived[0]["payload"]["status"] == "needs_review"
    assert derived[0]["payload"]["fields"] == {
        "subject": "Unclassified document", "attachments": [Path(pod_ref.removeprefix("doc:") + ".pdf").name]}


def test_invoice_usdot_mismatch_needs_review(world):
    _, _, events, trigger, message = _invoice_material(world)
    db = connect()
    try:
        _store_tms(db, events)
        invoice = next(item for item in message["attachments"]
                       if classify(item["text"]) == "carrier_invoice")
        invoice["text"] = re.sub(r"\bUSDOT\s+\d+", "USDOT 999999", invoice["text"], count=1)
        derived = asyncio.run(_invoice_email(trigger, message, db))
    finally:
        db.close()

    assert len(derived) == 1 and derived[0]["type"] == "document.extracted"
    assert derived[0]["payload"]["status"] == "needs_review"
    assert derived[0]["payload"]["fields"]["mismatch"] == "usdot"


def test_foreign_receipts_and_pod_are_not_matched_to_invoice_load(world):
    _, load, events, trigger, message = _invoice_material(world, "detention_unsupported", seed=3)
    _, other_load, other_events, _, other_message = _invoice_material(
        world, "detention_supported", seed=11)
    db = connect()
    try:
        _store_tms(db, events)
        _store_tms(db, other_events)
        foreign_pod = next(item for item in other_message["attachments"]
                           if classify(item["text"]) == "pod")
        foreign_detention = next(item for item in other_message["attachments"]
                                 if classify(item["text"]) == "detention_log")
        lumper_ref = docs.write(f"LUMPER-{other_load['load_id']}", "lumper_receipt", {
            "lumper_co": "Foreign Lumper LLC", "consignee": other_load["consignee"],
            "receipt_no": "R-100", "date": "2026-10-25", "po": other_load["po"],
            "carrier": other_load["carrier"], "pallets": other_load["pallets"],
            "amount": 180.0, "unloader": "Loader"})
        lumper_text = (docs.docs_dir() / f"{lumper_ref.removeprefix('doc:')}.pdf").read_bytes()
        foreign_lumper = {"filename": f"{lumper_ref.removeprefix('doc:')}.pdf",
                          "text": "\n".join(page.extract_text() or ""
                                            for page in PdfReader(BytesIO(lumper_text)).pages)}
        message["attachments"] = [item for item in message["attachments"] if classify(item["text"]) != "pod"]
        message["attachments"].extend([
            {"filename": "POD-OTHER.pdf", "text": foreign_pod["text"]},
            {"filename": "DET-OTHER.pdf", "text": foreign_detention["text"]},
            foreign_lumper,
        ])
        assert load["po"] != other_load["po"]
        derived = asyncio.run(_invoice_email(trigger, message, db))
    finally:
        db.close()

    invoice = next(event["payload"] for event in derived
                   if event["type"] == "carrier_invoice.received")
    document = derived[0]["payload"]
    assert invoice["receipt_attached"] is False
    assert invoice["pod_signed"] is False
    assert set(document["fields"]["unmatched_attachments"]) == {
        "POD-OTHER.pdf", "DET-OTHER.pdf", foreign_lumper["filename"]}


def test_conflicting_invoice_load_id_and_bol_needs_review(world):
    _, _, events, trigger, message = _invoice_material(world, seed=3)
    _, other_load, other_events, _, _ = _invoice_material(world, seed=17)
    db = connect()
    try:
        _store_tms(db, events)
        _store_tms(db, other_events)
        invoice = next(item for item in message["attachments"]
                       if classify(item["text"]) == "carrier_invoice")
        invoice["text"] = re.sub(r"(\bBOL\s*#\s*)[A-Z0-9-]+",
                                 rf"\g<1>{other_load['bol']}", invoice["text"], count=1, flags=re.IGNORECASE)
        derived = asyncio.run(_invoice_email(trigger, message, db))
    finally:
        db.close()

    assert len(derived) == 1 and derived[0]["type"] == "document.extracted"
    assert derived[0]["payload"]["status"] == "needs_review"
    assert derived[0]["payload"]["fields"]["mismatch"] == "load_id/bol"


def test_invoice_without_line_items_needs_review(world):
    _, _, events, trigger, message = _invoice_material(world)
    db = connect()
    try:
        _store_tms(db, events)
        invoice = next(item for item in message["attachments"]
                       if classify(item["text"]) == "carrier_invoice")
        invoice["text"] = re.sub(r"(?s)(Description\s+Qty\s+Rate\s+Amount).*?(?=TOTAL\s+DUE)",
                                 r"\1\n", invoice["text"], count=1)
        derived = asyncio.run(_invoice_email(trigger, message, db))
    finally:
        db.close()

    assert len(derived) == 1 and derived[0]["type"] == "document.extracted"
    assert derived[0]["payload"]["status"] == "needs_review"
    assert derived[0]["payload"]["fields"]["mismatch"] == "no_line_items"
