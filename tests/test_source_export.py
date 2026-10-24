"""Ingest state -> the source dataset agents trust (SOURCE_DATA_PATH)."""
import asyncio
import json

import pytest

from agents.carrier_ap.tools import duplicate_invoices, three_way_match
from agents.risk.tools import audit_charges
from common.envelope import TaskEnvelope
from common.source_repository import SourceRepository
from ingest import main as ingest_main
from ingest import source_export
from ingest.main import connect
from integrations import directory
from sim import generator
from sim.world import offline_world
from tests import test_ingest
from tests.test_ingest import _ingest, _run

world = test_ingest.world  # same real-registry world fixture as the ingest tests


def _export(events):
    db = connect()
    try:
        _ingest(events, db)
        db.commit()
        return source_export.build(db)
    finally:
        db.close()


@pytest.fixture
def live_source(tmp_path, monkeypatch):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    target = tmp_path / "source.json"
    monkeypatch.setenv("SOURCE_DATA_PATH", str(target))

    def load(dataset, case_id):
        source_export.write(dataset, target)
        repository = SourceRepository()
        return asyncio.run(repository.load(TaskEnvelope(task_type="process_carrier_invoice", case_id=case_id)))
    return load


def test_unsupported_detention_case_carries_what_agents_check(world, live_source):
    _gen, load, events = _run(world, "detention_unsupported")
    dataset = _export(events)
    case = live_source(dataset, load["case_id"])
    assert case["source_simulated"] is True and case["synthetic"] is True
    assert case["invoice_amount"] > case["contract_rate"] == load["contract_rate"]
    assert case["carrier_id"] == load["carrier"]["id"] and case["shipper_id"] == load["shipper"]["id"]
    assert case["gps_dwell_min"] <= case["free_time_min"] and case["detention_hours_claimed"] == 2
    detention = next(item for item in case["line_items"] if item["kind"] == "detention")
    assert detention["receipt_required"] is True and detention["receipt_ref"] is None
    assert {doc["kind"] for doc in case["documents"]} >= {"rate_confirmation", "bill_of_lading", "pod"}
    assert all(ref in case["evidence_refs"] for ref in (f"doc:RC-{load['load_id']}", f"doc:POD-{load['load_id']}"))
    assert case["period"] == case["issued_at"][:7]
    assert three_way_match(case)["missing_documents"] == []
    audit = audit_charges(case)
    assert audit["needs_verification"] is True and "unsupported_detention" in audit["unsupported_charges"]


def test_supported_detention_links_the_detention_log_as_receipt(world):
    _gen, load, events = _run(world, "detention_supported")
    case = _export(events)["cases"][load["case_id"]]
    detention = next(item for item in case["line_items"] if item["kind"] == "detention")
    assert detention["receipt_ref"] == f"doc:DET-{load['load_id']}"
    assert audit_charges(case)["needs_verification"] is False


def test_unsigned_pod_is_exported_unsigned(world):
    _gen, load, events = _run(world, "pod_unsigned")
    case = _export(events)["cases"][load["case_id"]]
    assert three_way_match(case)["missing_documents"] == ["pod"]


def test_renumbered_invoice_case_sees_the_original_as_a_duplicate(world):
    _gen, load, events = _run(world, "duplicate_renumbered")
    cases = _export(events)["cases"]
    original, renumbered = cases[load["case_id"]], cases[f"{load['case_id']}-R"]
    assert duplicate_invoices(renumbered) == [original["invoice_id"]]
    assert duplicate_invoices(original) == []


def test_receivable_case_lists_its_deposits(world, live_source):
    _gen, load, events = _run(world, "shipper_short_pay")
    dataset = _export(events)
    case = live_source(dataset, f"{load['case_id']}-AR")
    assert case["shipper_invoice_id"] == f"SINV-{load['load_id']}"
    [deposit] = case["deposits"]
    assert deposit["invoice_id"] == case["shipper_invoice_id"] and deposit["shipper_id"] == case["shipper_id"]
    assert 0 < deposit["amount"] < case["shipper_invoice_amount"]
    assert case["invoice_amount"] and case["period"]


def test_bank_change_case_and_invoice_hold_without_bank_credentials(world, live_source):
    _gen, load, events = _run(world, "bank_change")
    dataset = _export(events)
    change = live_source(dataset, f"{load['case_id']}-BANK")
    assert change["bank_change_requested"] is True and change["carrier_id"] == load["carrier"]["id"]
    assert change["bank_change"]["sender_matches_registered"] is False
    assert len(change["bank_change"]["new_account_last4"]) == 4
    assert dataset["cases"][load["case_id"]]["bank_change_requested"] is True
    body = json.dumps(dataset)
    assert '"new_account"' not in body and '"new_routing"' not in body


def test_directory_keeps_registry_provenance(world, live_source):
    _gen, load, events = _run(world, "clean")
    dataset = _export(events)
    carrier = dataset["companies"][load["carrier"]["id"]]
    assert carrier["source"] == "fmcsa_census" and carrier["verified"] is True and carrier["synthetic"] is False
    assert carrier["authority_status"] == "ACTIVE" and "insurance_verified" not in carrier
    live_source(dataset, load["case_id"])
    contact = asyncio.run(SourceRepository().get_contact(carrier["id"]))
    assert contact["phone"] == carrier["phone"] and contact["source_url"] == carrier["source_url"]
    assert contact["source_simulated"] is False


def test_hero_invoice_opens_the_prd_case(world, tmp_path):
    directory.save(offline_world())  # a current-schema world, so seeding never rebuilds from the network
    asyncio.run(generator.seed_hero(world))
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    cases = _export(events)["cases"]
    assert {"CASE-4821", "CASE-4821-AR", "CASE-C-77-BANK"} <= cases.keys()
    assert "CASE-LOAD-4821" not in cases
    assert cases["CASE-C-77-BANK"]["carrier_id"] == "C-77"


def test_write_is_atomic_and_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "source.json"
    source_export.write({"cases": {}, "companies": {}, "contacts": {}}, target)
    assert json.loads(target.read_text())["cases"] == {}
    assert [path.name for path in tmp_path.iterdir()] == ["source.json"]


def test_case_is_exported_before_its_event_reaches_the_router(world, tmp_path):
    _gen, _load, events = _run(world, "clean")
    invoice_mail = next(event for event in events if event["type"] == "email.received"
                        and any(str(name).startswith("INV-") for name in event["payload"]["attachments"]))
    target = tmp_path / "source.json"
    seen = []

    class FakeRedis:
        async def xadd(self, stream, fields, **_kwargs):
            event = json.loads(fields["json"])
            if event["type"] == "carrier_invoice.received":
                seen.append(event["payload"]["case_id"] in json.loads(target.read_text())["cases"])

        async def xack(self, *_args):
            pass

    db = connect()
    try:
        _ingest([event for event in events if event["sim_ts"] < invoice_mail["sim_ts"]], db)
        db.commit()
        exporter = source_export.Exporter(target)
        ok = asyncio.run(ingest_main._process_entry(FakeRedis(), db, "1-0", {"json": json.dumps(invoice_mail)},
                                                    exporter))
    finally:
        db.close()
    assert ok and seen == [True]


def test_double_brokered_invoice_is_exported_as_an_identity_conflict(world):
    _gen, load, events = _run(world, "double_brokering")
    case = _export(events)["cases"][load["case_id"]]
    assert case["carrier_identity_mismatch"] is True
    identity = case["carrier_identity"]
    assert identity["tendered_usdot"] == load["carrier"]["usdot"] != identity["invoice_usdot"]
    assert "usdot_mismatch" in identity["flags"]


@pytest.mark.parametrize("scenario", ["clean", "factoring_unverified", "decoy_quickpay"])
def test_matching_carriers_are_not_identity_conflicts(world, scenario):
    _gen, load, events = _run(world, scenario)
    case = _export(events)["cases"][load["case_id"]]
    assert case["carrier_identity_mismatch"] is False and "carrier_identity" not in case


def test_resent_invoice_stays_one_case_with_its_resubmission_recorded(world):
    _gen, load, events = _run(world, "duplicate_invoice")
    cases = _export(events)["cases"]
    assert [case_id for case_id in cases if case_id.startswith(load["case_id"])] == [
        load["case_id"], f"{load['case_id']}-AR"]
    assert cases[load["case_id"]]["resubmission_refs"]
