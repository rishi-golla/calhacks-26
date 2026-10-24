"""Input validation (harness/ingest/validate.py) and intake memory (harness/ingest/memory.py)."""
import json
import pathlib
import tempfile

import pytest

from harness.ingest.memory import Directory, Memory, domain_of, fingerprints
from harness.ingest.validate import parse_date, validate

INVOICE = {"case_id": "CASE-1", "load_id": "LD-1", "carrier_id": "C-311", "invoice_number": "INV-9",
           "bol": "BOL1", "invoice_amount": 2150.0, "contract_rate": 1850.0, "from": "Billing <ap@sierrahaul.com>",
           "remit_to": {"name": "Sierra Haul LLC"}, "invoice_carrier_usdot": "311"}


@pytest.fixture
def tmp():
    return pathlib.Path(tempfile.mkdtemp())


@pytest.fixture
def directory(tmp):
    p = tmp / "source.json"
    p.write_text(json.dumps({"companies": {"C-311": {"legal_name": "SIERRA HAUL LLC", "name": "Sierra Haul LLC",
                                                     "usdot": "311", "email": "dispatch@sierrahaul.com"}}}))
    return Directory(str(p))


# ---- validation ----

def test_fixes_what_it_safely_can():
    out, issues, fatal = validate("carrier_invoice.received", {**INVOICE, "invoice_amount": "$2,150.00",
                                                                "receipt_attached": "no", "invoice_date": "10/24/2026"})
    assert fatal is None and issues == []
    assert out["invoice_amount"] == 2150.0 and out["receipt_attached"] is False
    assert out["invoice_date"] == "2026-10-24T00:00:00Z"


@pytest.mark.parametrize(("change", "flag"), [
    ({"invoice_amount": "two thousand"}, "invoice_amount 'two thousand' is not a number"),
    ({"invoice_amount": -50.0}, "invoice_amount is negative"),
    ({"invoice_amount": None}, "missing invoice_amount"),
    ({"invoice_amount": 3000.0}, "more than 1.5x the contract rate"),
    ({"detention_hours_claimed": 200}, "detention_hours_claimed implausible"),
    ({"carrier_id": "311"}, "carrier_id '311' has an unexpected format"),
    ({"invoice_date": "yesterday"}, "invoice_date 'yesterday' is not a date"),
])
def test_flags_suspicious_input(change, flag):
    _, issues, fatal = validate("carrier_invoice.received", {**INVOICE, **change})
    assert fatal is None and any(flag in i for i in issues), issues


def test_only_unusable_input_is_rejected():
    assert validate("carrier_invoice.received", ["not", "an", "object"])[2] == "payload must be an object"
    assert validate("gps.ping", {"anything": "goes"})[1:] == ([], None)


def test_unsigned_pod_is_flagged():
    _, issues, _ = validate("pod.received", {"load_id": "LD-1", "signed": False})
    assert "input: POD is not signed" in issues


def test_parse_date_formats():
    assert parse_date("2026-09-26T05:58:55.175339Z") == "2026-09-26T05:58:55.175339Z"
    assert parse_date("2026-09-26 05:58") == "2026-09-26T05:58:00Z" and parse_date("nope") is None


# ---- duplicates (P-007) ----

def test_resent_invoice_is_a_duplicate_within_14_days(tmp):
    m = Memory(tmp / "m.sqlite")
    m.remember_invoice("EVT-1", "CASE-1", INVOICE, at=0)
    dup = m.find_duplicate("EVT-2", INVOICE, at=86400)
    assert dup["event_id"] == "EVT-1" and dup["rule"] == "same invoice number" and dup["days_apart"] == 1.0
    assert m.find_duplicate("EVT-3", INVOICE, at=15 * 86400) is None          # outside the window


def test_renumbered_resend_is_caught_by_bol_and_amount(tmp):
    m = Memory(tmp / "m.sqlite")
    m.remember_invoice("EVT-1", "CASE-1", INVOICE, at=0)
    dup = m.find_duplicate("EVT-2", {**INVOICE, "invoice_number": "INV-9-R"}, at=3600)
    assert dup["rule"] == "same BOL and amount"


def test_retry_of_the_same_event_is_not_its_own_duplicate(tmp):
    m = Memory(tmp / "m.sqlite")
    m.remember_invoice("EVT-1", "CASE-1", INVOICE, at=0)
    assert m.find_duplicate("EVT-1", INVOICE, at=10) is None


def test_different_invoice_is_not_a_duplicate(tmp):
    m = Memory(tmp / "m.sqlite")
    m.remember_invoice("EVT-1", "CASE-1", INVOICE, at=0)
    other = {**INVOICE, "invoice_number": "INV-10", "bol": "BOL2", "load_id": "LD-2", "invoice_amount": 990.0}
    assert m.find_duplicate("EVT-2", other, at=10) is None and fingerprints({"invoice_amount": 1.0}) == []


# ---- identity, against the verified directory ----

def test_matching_identity_raises_nothing(tmp, directory):
    assert Memory(tmp / "m.sqlite", directory).identity_signals("carrier_invoice.received", INVOICE) == []


@pytest.mark.parametrize(("change", "flag"), [
    ({"from": "ap@sierrahau1.com"}, "lookalike sender domain"),
    ({"from": "ap@sierra-billing.com"}, "different sender domain"),
    ({"remit_to": {"name": "Apex Freight Capital LLC"}}, "factoring notice or payment diversion"),
    ({"invoice_carrier_usdot": "999"}, "USDOT on invoice for C-311 is 999"),
])
def test_identity_changes_are_flagged(tmp, directory, change, flag):
    notes = Memory(tmp / "m.sqlite", directory).identity_signals("carrier_invoice.received", {**INVOICE, **change})
    assert any(flag in n for n in notes), notes


def test_bank_change_request_cannot_become_the_baseline(tmp):
    m = Memory(tmp / "m.sqlite", Directory(""))   # carrier not in a directory: first-seen fallback
    bank = {"carrier_id": "C-77", "from": "acct@rapidridge-transp0rt.com", "sender_matches_registered": False}
    m.remember_identity("bank_change.requested", "EVT-B", bank, at=0)
    m.remember_identity("carrier_invoice.received", "EVT-I", {"carrier_id": "C-77", "from": "ap@rapidridge.com"}, at=1)
    notes = m.identity_signals("bank_change.requested", bank)
    assert any("different sender domain" in n and "first seen: rapidridge.com" in n for n in notes)
    assert any("not from the registered contact" in n for n in notes)


def test_domain_of():
    assert domain_of("Billing <AP@SierraHaul.com>") == "sierrahaul.com" and domain_of("no email") == ""


# ---- end to end through the pipeline (models unreachable: deterministic) ----

async def test_pipeline_blocks_a_resent_invoice(tmp):
    from unittest.mock import AsyncMock

    from a2a.helpers import new_task
    from a2a.types import TaskState

    from harness.ingest import dispatch as dispatch_mod
    from harness.ingest.dispatch import Dispatcher
    from harness.ingest.pipeline import process
    from harness.ingest.vault import Vault
    from harness.pool import CONFIG_PATH, Pool

    cfg = tmp / "c.toml"
    cfg.write_text(CONFIG_PATH.read_text().replace("127.0.0.1:800", "127.0.0.1:1800"))
    pool = Pool(cfg)
    await pool.check_health()
    sender = AsyncMock(return_value=new_task("task-1", "CASE-1", TaskState.TASK_STATE_SUBMITTED))
    original = dispatch_mod.send_task
    dispatch_mod.send_task = sender
    try:
        vault, disp, mem = Vault(tmp / "v.sqlite"), Dispatcher(tmp / "d.sqlite"), Memory(tmp / "m.sqlite")
        ev = {"event_id": "EVT-1", "type": "carrier_invoice.received", "sim_ts": "2026-10-01T00:00:00Z",
              "payload": INVOICE}
        first = await process(pool, vault, disp, ev, memory=mem)
        resent = await process(pool, vault, disp, {**ev, "event_id": "EVT-2", "sim_ts": "2026-10-02T00:00:00Z"},
                               memory=mem)
    finally:
        dispatch_mod.send_task = original
        await pool.aclose()
    assert first["status"] == "accepted" and sender.await_count == 1           # the copy is never sent
    assert resent["status"] == "duplicate" and resent["prior"]["task_id"] == "task-1"
    assert "P-007" in resent["reason"] and disp.get("EVT-2")["status"] == "duplicate"
