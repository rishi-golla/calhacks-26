"""Unreferenced shipper cash (sim `misapplied_payment`) and the configurable `events`/`documents` caps."""
import asyncio
import json
import logging
import os

import pytest

from agents.shipper_ar.main import ShipperArAgent
from agents.validator.tools import deterministic_review
from common.envelope import ResultArtifact
from ingest import main as ingest_main
from ingest import mirror as spacetime_mirror
from ingest import source_export
from ingest.main import connect, handle
from tests.test_brokerage_workflows import run_handler
from tests.test_ingest_mirror import FakeRedis, FakeSpacetime
from tests.test_validator_decisions import REFS, bundle, failed, source

ACME = {"id": "S-1", "name": "ACME FOODS"}
BETA = {"id": "S-2", "name": "BETA GOODS"}


@pytest.fixture(autouse=True)
def environment(tmp_path, monkeypatch):
    (tmp_path / "world.json").write_text(json.dumps({"carriers": [], "shippers": [ACME, BETA]}))
    monkeypatch.setenv("WORLD_FILE", str(tmp_path / "world.json"))
    monkeypatch.setenv("SIM_DATA_DIR", str(tmp_path))
    for name in ("EVENTS_STREAM_MAXLEN", "DOCUMENTS_STREAM_MAXLEN"):
        monkeypatch.delenv(name, raising=False)
    for name in os.environ:
        if name.startswith("SPACETIME_"):
            monkeypatch.delenv(name)


def tendered(load_id, shipper):
    return {"event_id": f"EVT-T-{load_id}", "type": "load.tendered", "sim_ts": "2026-10-24T08:00:00Z",
            "payload": {"load_id": load_id, "case_id": f"CASE-{load_id}", "shipper": shipper, "po": f"PO-{load_id}",
                        "carrier": {"id": "C-1", "name": "CARRIER ONE"}}}


def issued(load_id, shipper, amount):
    return {"event_id": f"EVT-I-{load_id}", "type": "shipper_invoice.issued", "sim_ts": "2026-10-24T09:00:00Z",
            "evidence_refs": [f"doc:SINV-{load_id}"],
            "payload": {"invoice_id": f"SINV-{load_id}", "load_id": load_id, "case_id": f"CASE-{load_id}",
                        "po": f"PO-{load_id}", "shipper": shipper, "amount": amount}}


def deposit(txn_id, amount, reference, counterparty):
    record = f"16,165,{round(amount * 100)},0,{txn_id},{reference},{counterparty}/"
    return {"event_id": f"EVT-B-{txn_id}", "type": "bank.event", "sim_ts": "2026-10-25T09:00:00Z",
            "payload": {"file": "BAI2-20261025.txt", "account": "1234", "record": record}}


def run(db, events):
    async def process():
        out = []
        for event in events:
            out.extend(await handle(event, db))
        return out
    return asyncio.run(process())


def setup(db, invoices):
    """invoices: (load_id, shipper, amount)."""
    run(db, [tendered(load_id, shipper) for load_id, shipper, _ in invoices])
    run(db, [issued(*item) for item in invoices])


def routable(derived):
    return [event for event in derived if event["type"] != "document.extracted"]


def test_unique_open_invoice_of_the_shipper_is_routed_for_review_not_applied():
    db = connect()
    try:
        # BETA owes the same amount, but the remitter is ACME: shipper first, then amount.
        setup(db, [("L1", ACME, 1000.0), ("L2", BETA, 1000.0), ("L3", ACME, 750.0)])
        derived = run(db, [deposit("BNK-1", 1000.0, "SINV-MISSING-L1", "ACME FOODS")])
        [event] = routable(derived)
        assert event["type"] == "shipper_payment.received"
        payload = event["payload"]
        assert payload["case_id"] == "CASE-L1-AR" and payload["invoice_id"] == "SINV-L1"
        assert payload["reference_mismatch"] is True and payload["remittance_ref"] == "SINV-MISSING-L1"
        assert payload["invoices_applied"] == [] and payload["candidate_invoice_id"] == "SINV-L1"
        assert payload["match_basis"] == "shipper_and_amount" and payload["ingest_flags"] == ["reference_mismatch"]
        assert payload["shipper_id"] == "S-1" and payload["chase"] is False
        assert spacetime_mirror.routed_case_ids(derived) == ["CASE-L1-AR"]

        cases = source_export.build_cases(db)
        receivable = cases["CASE-L1-AR"]
        assert receivable["deposits"] == []  # never cash applied to the invoice
        assert receivable["unapplied_deposits"] == [{
            "id": "BNK-1", "shipper_id": "S-1", "amount": 1000.0, "posted_at": "2026-10-25T09:00:00Z",
            "remittance_ref": "SINV-MISSING-L1", "candidate_invoice_id": "SINV-L1",
            "match_basis": "shipper_and_amount", "reference_mismatch": True}]
        assert "unapplied_deposits" not in cases["CASE-L2-AR"]
        assert spacetime_mirror.title("CASE-L1-AR", receivable) == "ACME FOODS unapplied payment on L1"
        assert spacetime_mirror.exposure("CASE-L1-AR", receivable) == 1000.0

        # The invoice now has a candidate: the same cash again under a bad reference is not matched twice.
        again = run(db, [deposit("BNK-2", 1000.0, "SINV-MISSING-L1", "ACME FOODS")])
        assert [event["type"] for event in routable(again)] == ["bank.event"]
    finally:
        db.close()


def test_unknown_remitter_falls_back_to_a_unique_amount_match():
    db = connect()
    try:
        setup(db, [("L1", ACME, 1000.0), ("L2", BETA, 800.0)])
        [event] = routable(run(db, [deposit("BNK-1", 800.0, "", "ACCOUNTS PAYABLE DEPT")]))
        assert event["type"] == "shipper_payment.received"
        assert event["payload"]["case_id"] == "CASE-L2-AR" and event["payload"]["match_basis"] == "amount_only"
    finally:
        db.close()


def test_no_match_when_the_known_shipper_has_no_such_open_invoice(caplog):
    db = connect()
    try:
        # BETA has an open $800 invoice, but ACME remitted: a known shipper never borrows another's invoice.
        setup(db, [("L1", ACME, 1000.0), ("L2", BETA, 800.0)])
        with caplog.at_level(logging.WARNING, logger="ingest"):
            derived = run(db, [deposit("BNK-1", 800.0, "SINV-MISSING-X", "ACME FOODS")])
        [event] = routable(derived)
        assert event["type"] == "bank.event" and "case_id" not in event["payload"]
        assert spacetime_mirror.routed_case_ids(derived) == []
        review = next(event["payload"] for event in derived if event["type"] == "document.extracted")
        assert review["status"] == "needs_review" and review["kind"] == "bank_deposit" and review["case_id"] is None
        assert review["fields"] == {**review["fields"], "mismatch": "unapplied_cash", "reason": "no_open_invoice",
                                    "txn_id": "BNK-1", "candidate_invoice_ids": []}
        assert "unapplied cash: deposit BNK-1" in caplog.text
    finally:
        db.close()


def test_paid_invoice_is_not_open():
    db = connect()
    try:
        setup(db, [("L1", ACME, 1000.0)])
        paid = run(db, [deposit("BNK-1", 1000.0, "SINV-L1", "ACME FOODS")])
        assert "reference_mismatch" not in routable(paid)[0]["payload"]  # the normal, referenced path
        derived = run(db, [deposit("BNK-2", 1000.0, "SINV-MISSING-L1", "ACME FOODS")])
        assert [event["type"] for event in routable(derived)] == ["bank.event"]
    finally:
        db.close()


def test_ambiguous_match_stays_unrouted_and_goes_to_review(caplog):
    db = connect()
    try:
        setup(db, [("L1", ACME, 1000.0), ("L2", ACME, 1000.0)])
        with caplog.at_level(logging.WARNING, logger="ingest"):
            derived = run(db, [deposit("BNK-1", 1000.0, "SINV-MISSING-L1", "ACME FOODS")])
        [event] = routable(derived)
        assert event["type"] == "bank.event" and "case_id" not in event["payload"]
        assert spacetime_mirror.routed_case_ids(derived) == []
        review = next(event["payload"] for event in derived if event["type"] == "document.extracted")
        assert review["fields"]["reason"] == "ambiguous"
        assert review["fields"]["candidate_invoice_ids"] == ["SINV-L1", "SINV-L2"]
        assert "ambiguous" in caplog.text
        stored = json.loads(db.execute("SELECT json FROM deposits WHERE txn_id='BNK-1'").fetchone()[0])
        assert "matched_invoice_id" not in stored
    finally:
        db.close()


def test_remittance_advice_po_breaks_a_tie():
    db = connect()
    try:
        setup(db, [("L1", ACME, 1000.0), ("L2", ACME, 1000.0)])
        advice = {"mail_id": "MAIL-9", "subject": "Remittance advice - ACME FOODS - SINV-MISSING-L2",
                  "body": "Payment of $1,000.00 sent via ACH for invoice SINV-MISSING-L2 (PO PO-L2)."}
        ingest_main._remittance_advice({"event_id": "EVT-M"}, advice, db)
        [event] = routable(run(db, [deposit("BNK-1", 1000.0, "SINV-MISSING-L2", "ACME FOODS")]))
        assert event["payload"]["case_id"] == "CASE-L2-AR"
        assert event["payload"]["match_basis"] == "shipper_and_amount_and_remittance_po"
        assert "mail:MAIL-9" in event["payload"]["evidence_refs"]
    finally:
        db.close()


class CappedRedis(FakeRedis):
    def __init__(self, log):
        super().__init__(log)
        self.caps = {}

    async def xadd(self, stream, fields, **kwargs):
        self.caps[stream] = (kwargs.get("maxlen"), kwargs.get("approximate"))
        await super().xadd(stream, fields, **kwargs)


async def test_matched_receivable_is_mirrored_before_its_event_is_published(monkeypatch):
    monkeypatch.setenv("EVENTS_STREAM_MAXLEN", "1234")
    log: list = []
    spacetime, redis = FakeSpacetime(log), CappedRedis(log)
    mirror = spacetime.mirror()
    events = [tendered("L1", ACME), issued("L1", ACME, 1000.0),
              deposit("BNK-1", 1000.0, "SINV-MISSING-L1", "ACME FOODS")]
    db = connect()
    try:
        for index, event in enumerate(events):
            assert await ingest_main._process_entry(redis, db, f"{index}-0", {"json": json.dumps(event)},
                                                    None, mirror)
    finally:
        db.close()
    published = next(index for index, entry in enumerate(log)
                     if entry[0] == "xadd" and entry[2]["type"] == "shipper_payment.received")
    before = [(entry[1], entry[2]) for entry in log[:published] if entry[0] == "write"]
    record = next(payload for reducer, payload in before
                  if reducer == spacetime_mirror.SOURCE_REDUCER and payload["id"] == "case:CASE-L1-AR")
    assert json.loads(record["record_json"])["unapplied_deposits"][0]["id"] == "BNK-1"
    opened = next(payload for reducer, payload in before if reducer == "open_case")
    assert opened["case_id"] == "CASE-L1-AR" and opened["title"] == "ACME FOODS unapplied payment on L1"
    assert redis.caps[ingest_main.EVENTS_STREAM] == (1234, True)


async def test_stream_caps_default_to_200000_and_bad_caps_fail_at_startup(monkeypatch):
    assert ingest_main.events_maxlen() == ingest_main.documents_maxlen() == 200_000
    monkeypatch.setenv("DOCUMENTS_STREAM_MAXLEN", "77")
    redis = CappedRedis([])
    db = connect()
    try:
        assert await ingest_main._process_entry(redis, db, "0-0", {"json": json.dumps(
            deposit("BNK-1", 5.0, "SINV-NONE", "NOBODY"))})
    finally:
        db.close()
    assert redis.caps == {ingest_main.DOCUMENTS_STREAM: (77, True), ingest_main.EVENTS_STREAM: (200_000, True)}

    def unreachable(_url):
        pytest.fail("a bad cap must stop ingest before it connects")

    monkeypatch.setattr(ingest_main.redis, "from_url", unreachable)
    for name in ("EVENTS_STREAM_MAXLEN", "DOCUMENTS_STREAM_MAXLEN"):
        for bad in ("0", "-5", "lots", "1.5"):
            monkeypatch.setenv(name, bad)
            with pytest.raises(ValueError, match=name):
                await ingest_main.main()
        monkeypatch.delenv(name)


def receivable_source(**changes):
    return {"case_id": "CASE-L1-AR", "shipper_id": "S-1", "shipper_invoice_id": "SINV-L1",
            "shipper_invoice_amount": 1000, "period": "2026-10", "evidence_refs": ["doc:SINV-L1"],
            "deposits": [], "unapplied_deposits": [{
                "id": "BNK-1", "shipper_id": "S-1", "amount": 1000, "remittance_ref": "SINV-MISSING-L1",
                "candidate_invoice_id": "SINV-L1", "reference_mismatch": True}], **changes}


@pytest.fixture
def fixture_mode(monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")


@pytest.mark.parametrize("skill", ["reconcile_customer_payment", "chase_overdue"])
async def test_shipper_ar_reports_unapplied_cash_as_partial_and_never_chases(fixture_mode, skill):
    calls = []

    async def delegate(child, *args, **kwargs):
        calls.append(child)
        return [{"outcome": "verified", "findings": {}}]

    result = await run_handler(ShipperArAgent, skill, receivable_source(overdue=True), delegate=delegate)
    assert result.outcome == "partial"
    assert result.findings["reference_mismatch"] is True and result.findings["cash_applied_automatically"] is False
    assert result.findings["unapplied_deposit_ids"] == ["BNK-1"] and result.findings["matched_amount"] == 0
    assert "place_call" not in calls and "approve_entry" not in calls


async def test_shipper_ar_holds_a_settled_invoice_with_extra_unreferenced_cash(fixture_mode):
    async def delegate(*args, **kwargs):
        return [{"outcome": "verified", "findings": {}}]

    paid = receivable_source(deposits=[{"id": "BNK-0", "invoice_id": "SINV-L1", "shipper_id": "S-1", "amount": 1000}],
                             journal_requests=[])
    result = await run_handler(ShipperArAgent, "reconcile_customer_payment", paid, delegate=delegate)
    assert result.outcome == "hold" and result.findings["balance"] == 0


def test_validator_expects_hold_for_unreferenced_cash_on_a_settled_invoice():
    src = source(shipper_invoice_id="SINV-1", shipper_invoice_amount=1000, deposits=[
        {"id": "D-1", "invoice_id": "SINV-1", "shipper_id": "S-1", "amount": 1000}],
        unapplied_deposits=[{"id": "D-2", "amount": 1000, "reference_mismatch": True}])
    verified = ResultArtifact(outcome="verified", evidence_refs=REFS, findings={
        "matched_amount": 1000, "balance": 0, "matched_deposit_ids": ["D-1"]}).model_dump()
    review = deterministic_review(bundle(src, verified, "shipper_ar", "reconcile_customer_payment"))
    assert "outcome_matches_balance" in failed(review)
    held = dict(verified, outcome="hold")
    review = deterministic_review(bundle(src, held, "shipper_ar", "reconcile_customer_payment"))
    assert "outcome_matches_balance" not in failed(review)
