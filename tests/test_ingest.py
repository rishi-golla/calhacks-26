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
from ingest import main as ingest_main
from ingest.documents import classify, extract_invoice_rules, read_email
from ingest.main import _invoice_email, connect, handle
from integrations import edgar, fmcsa
from sim import bank, docs, generator
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

        async def aclose(self):
            pass

    fake_redis = FakeRedis()
    db = connect()
    monkeypatch.setattr(ingest_main.redis, "from_url", lambda _url: fake_redis)
    monkeypatch.setattr(ingest_main, "connect", lambda: db)
    with pytest.raises(StopReading):
        asyncio.run(ingest_main.main())
    assert fake_redis.group_args == ("raw", "ingest", "0", True)


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
        assert len(invoices) == 1
        assert "usdot_mismatch" in invoices[0]["ingest_flags"]
        assert invoices[0]["invoice_carrier_usdot"] != load["carrier"]["usdot"]
        review = next(event for event in derived if event["type"] == "document.extracted")
        assert review["payload"]["status"] == "needs_review"
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
        assert invoice["carrier"]["usdot"] == load["carrier"]["usdot"]
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
        account = re.search(r"Account number: (\d{10,12})", bank_mail["body"])
        assert account and change["new_account_last4"] == account.group(1)[-4:]
        assert change["new_account"] == account.group(1)
        assert bank.aba_valid(change["new_routing"])

    if scenario == "misapplied_payment":
        assert not any(event["type"] == "shipper_payment.received" for event in derived)
        assert any(event["type"] == "bank.event" and
                   event["payload"]["reference"].startswith("SINV-MISSING-") for event in derived)
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
    bank_event["payload"].pop("record")
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


def test_raw_bank_events_process_each_record_independently(world):
    _, load_a, events_a = _run(world, "clean", seed=3)
    _, load_b, events_b = _run(world, "clean", seed=11)

    def event_of_type(events, event_type):
        return next(event for event in events if event["type"] == event_type)

    shipper_invoice_a = event_of_type(events_a, "shipper_invoice.issued")
    bank_event_a = event_of_type(events_a, "bank.event")
    shipper_invoice_b = event_of_type(events_b, "shipper_invoice.issued")
    bank_event_b = event_of_type(events_b, "bank.event")
    for bank_event in (bank_event_a, bank_event_b):
        bank_event["payload"]["file"] = "BAI2-same-file.txt"

    db = connect()
    try:
        first = _ingest([shipper_invoice_a, bank_event_a], db)
        second = _ingest([shipper_invoice_b, bank_event_b], db)
    finally:
        db.close()

    payments = [event for event in first + second if event["type"] == "shipper_payment.received"]
    assert {event["payload"]["load_id"] for event in payments} == {load_a["load_id"], load_b["load_id"]}
    assert not any(event["type"] == "document.extracted" and event["payload"]["status"] == "needs_review"
                   for event in first + second)


def test_raw_bank_record_then_file_fallback_deduplicates_transactions(world):
    _, load_a, events_a = _run(world, "clean", seed=3)
    _, load_b, events_b = _run(world, "clean", seed=11)

    def event_of_type(events, event_type):
        return next(event for event in events if event["type"] == event_type)

    shipper_invoice_a = event_of_type(events_a, "shipper_invoice.issued")
    bank_event_a = event_of_type(events_a, "bank.event")
    shipper_invoice_b = event_of_type(events_b, "shipper_invoice.issued")
    bank_event_b = event_of_type(events_b, "bank.event")
    record_a = bank_event_a["payload"]["record"]
    record_b = bank_event_b["payload"]["record"]
    filename = "BAI2-idempotency.txt"
    bank_path = generator.data_dir() / "bank" / filename
    bank_path.parent.mkdir(parents=True, exist_ok=True)
    bank_path.write_text(f"{record_a}\n{record_b}\n")
    inline_event = {**bank_event_a, "payload": {**bank_event_a["payload"], "file": filename}}
    file_event = {**bank_event_b, "payload": {"file": filename, "account": bank_event_b["payload"]["account"]}}

    db = connect()
    try:
        _ingest([shipper_invoice_a, shipper_invoice_b], db)
        inline_output = _ingest([inline_event], db)
        file_output = _ingest([file_event], db)
    finally:
        db.close()

    payment_loads = [event["payload"]["load_id"] for event in inline_output + file_output
                     if event["type"] == "shipper_payment.received"]
    assert payment_loads.count(load_a["load_id"]) == 1
    assert payment_loads.count(load_b["load_id"]) == 1
    assert [event["payload"]["load_id"] for event in file_output
            if event["type"] == "shipper_payment.received"] == [load_b["load_id"]]


def test_handle_assigns_stable_event_ids_for_same_trigger(world):
    _, _, events = _run(world, "clean")
    trigger = next(event for event in events if event["type"] == "email.received"
                   and any(Path(name).stem.startswith("INV-") for name in event["payload"]["attachments"]))
    db = connect()
    try:
        _store_tms(db, events)
        first = asyncio.run(handle(trigger, db))
        second = asyncio.run(handle(trigger, db))
    finally:
        db.close()

    assert [event["event_id"] for event in first] == [event["event_id"] for event in second]
    assert [event["event_id"] for event in first] == [f"{trigger['event_id']}:{i}" for i in range(len(first))]


def test_bank_change_lookalike_sender_is_unverified_but_emitted(world):
    _, _, events = _run(world, "bank_change")
    db = connect()
    try:
        _store_tms(db, events)
        derived = _ingest(events, db)
    finally:
        db.close()

    request = next(event for event in derived if event["type"] == "bank_change.requested")
    document = next(event for event in derived if event["type"] == "document.extracted"
                    and event["payload"].get("kind") == "bank_change_request")
    assert request["payload"]["sender_matches_registered"] is False
    assert document["payload"]["status"] == "extracted_unverified_sender"
    assert document["payload"]["fields"]["sender_matches_registered"] is False


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

    document = next(event for event in derived if event["type"] == "document.extracted")
    email_event = next(event for event in derived if event["type"] == "email.received")
    assert document["payload"]["kind"] == "other"
    assert document["payload"]["status"] == "needs_review"
    assert document["payload"]["fields"] == {
        "subject": "Unclassified document", "attachments": [Path(pod_ref.removeprefix("doc:") + ".pdf").name]}
    assert email_event["payload"]["text"]


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

    document = next(event for event in derived if event["type"] == "document.extracted")
    invoice_event = next(event for event in derived if event["type"] == "carrier_invoice.received")
    assert document["payload"]["status"] == "needs_review"
    assert document["payload"]["fields"]["mismatch"] == "usdot"
    assert "usdot_mismatch" in invoice_event["payload"]["ingest_flags"]


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

    document = next(event for event in derived if event["type"] == "document.extracted")
    email_event = next(event for event in derived if event["type"] == "email.received")
    assert document["payload"]["status"] == "needs_review"
    assert document["payload"]["fields"]["mismatch"] == "load_id/bol"
    assert email_event["payload"]["text"]


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

    document = next(event for event in derived if event["type"] == "document.extracted")
    email_event = next(event for event in derived if event["type"] == "email.received")
    assert document["payload"]["status"] == "needs_review"
    assert document["payload"]["fields"]["mismatch"] == "no_line_items"
    assert email_event["payload"]["text"]


def test_offline_40_loads_follow_w1_routing_contract(world, tmp_path, monkeypatch):
    root = tmp_path / "offline-flow"
    asyncio.run(generator.offline(40, root, seed=7))
    monkeypatch.setenv("SIM_DATA_DIR", str(root))
    monkeypatch.setenv("WORLD_FILE", str(root / "world.json"))
    raw_events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    db = connect()
    try:
        async def consume():
            output = []
            by_trigger = {}
            for raw in raw_events:
                derived = await handle(raw, db)
                routable = [event for event in derived if event["type"] != "document.extracted"]
                by_trigger[raw["event_id"]] = routable
                output.extend(routable)
            return output, by_trigger

        routed, by_trigger = asyncio.run(consume())
    finally:
        db.close()

    route_types = {
        "carrier_invoice.received", "pod.received", "voice.inbound.dispute",
        "shipper_payment.received", "bank.event", "bank_change.requested",
    }
    not_routed = {"gps.ping", "load.tendered", "load.dispatched", "load.delivered"}
    allowed_types = route_types | not_routed | {"email.received", "scenario.injected"}
    assert all(event["type"] in allowed_types for event in routed)
    assert not any(event["type"] in {"document.extracted", "shipper_invoice.issued"} for event in routed)
    passthrough = {"load.tendered", "load.dispatched", "gps.ping", "load.delivered", "pod.received"}
    for raw in raw_events:
        if raw["type"] in passthrough:
            emitted = by_trigger[raw["event_id"]]
            assert len(emitted) == 1 and emitted[0]["event_id"] == raw["event_id"]
            if raw["type"] == "pod.received":
                assert emitted[0]["payload"]["carrier_id"]
                assert emitted[0]["payload"]["contract_rate"] > 0
                assert emitted[0]["payload"]["delivered_at"]
                assert "receiver" in emitted[0]["payload"] and "signed_by" in emitted[0]["payload"]
                assert "exceptions" in emitted[0]["payload"]
        elif raw["type"] == "shipper_invoice.issued":
            assert by_trigger[raw["event_id"]] == []

    invoice_emails = [
        raw for raw in raw_events
        if raw["type"] == "email.received"
        and any(Path(str(name).removeprefix("doc:")).name.startswith("INV-")
                for name in raw.get("payload", {}).get("attachments", []))
    ]
    for raw in invoice_emails:
        emitted = by_trigger[raw["event_id"]]
        assert len(emitted) == 1
        assert emitted[0]["type"] in {"carrier_invoice.received", "email.received"}

    bank_arrivals = [raw for raw in raw_events if raw["type"] == "bank.event"]
    for raw in bank_arrivals:
        emitted = by_trigger[raw["event_id"]]
        assert len(emitted) == 1
        assert emitted[0]["type"] in {"shipper_payment.received", "bank.event"}

    answer_entries = [json.loads(line) for line in (root / "answer_key.jsonl").read_text().splitlines()]
    double_load_id = next(item["load_id"] for item in answer_entries
                          if item["scenario"] == "double_brokering")
    double_invoice = next(
        event for raw in invoice_emails
        if raw.get("case_hint") == f"CASE-{double_load_id}"
        or any(double_load_id in str(name) for name in raw.get("payload", {}).get("attachments", []))
        for event in by_trigger[raw["event_id"]]
        if event["type"] == "carrier_invoice.received"
    )
    assert "usdot_mismatch" in double_invoice["payload"]["ingest_flags"]

    email_routed_types = {"carrier_invoice.received", "bank_change.requested", "email.received"}
    for event in routed:
        payload = event["payload"]
        if event["type"] in email_routed_types:
            assert payload["text"].strip()
            assert all(key in payload for key in ("from", "to", "subject", "date", "evidence_refs"))
            assert event["evidence_refs"] == payload["evidence_refs"]
        if event["type"] == "shipper_payment.received":
            assert sum(item["amount"] for item in payload["invoices_applied"]) == payload["amount"]

    reserved = {"event_type", "doc_type", "needs_review", "review_notes", "redacted_tokens"}

    def assert_no_reserved_keys(value):
        if isinstance(value, dict):
            assert not (reserved & value.keys())
            for nested in value.values():
                assert_no_reserved_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_no_reserved_keys(nested)

    for event in routed:
        assert_no_reserved_keys(event["payload"])

    injection = {"event_id": "INJECT-CONTRACT-1", "type": "scenario.injected",
                 "ts": "2026-01-01T00:00:00Z", "sim_ts": "2026-01-01T00:00:00Z",
                 "case_hint": "CASE-INJECT-CONTRACT-1", "payload": {"load_id": "LOAD-INJECT-CONTRACT-1"}}
    db = connect()
    try:
        injected = asyncio.run(handle(injection, db))
    finally:
        db.close()
    assert len(injected) == 1
    assert injected[0]["event_id"] == injection["event_id"]
    assert injected[0]["payload"]["generated_type"] == "load.tendered"

    found_routing_numbers = []
    found_account_numbers = []
    for email_path in (root / "mail").glob("*.eml"):
        body = read_email(email_path)["body"]
        found_routing_numbers.extend(re.findall(r"Routing number:\s*(\d{9})", body))
        found_account_numbers.extend(re.findall(r"Account number:\s*(\d{10,12})", body))
    assert found_routing_numbers
    assert all(bank.aba_valid(number) for number in found_routing_numbers)
    assert all(10 <= len(number) <= 12 for number in found_account_numbers)
