import json
import random
from datetime import UTC, datetime

import pytest

from integrations import edgar, fmcsa, tools
from integrations.normalize import e164, email

CARRIER_ROW = {"dot_number": "391314", "legal_name": "SAMUEL J PIAZZA & SON INC", "dba_name": "PIAZZA TRUCKING",
               "phone": "(323) 357-1999", "email_address": "Enrique@PiazzaTrucking.com", "phy_city": "SOUTH GATE",
               "phy_state": "CA", "phy_street": "9001 RAYO AVE", "phy_zip": "90280", "power_units": "38",
               "docket1": "231685"}
SEC_SUB = {"cik": "21344", "name": "COCA COLA CO", "sic": "2080", "sicDescription": "Beverages",
           "phone": "404-676-2121", "tickers": ["KO"],
           "addresses": {"business": {"street1": "ONE COCA COLA PLAZA", "city": "ATLANTA", "stateOrCountry": "GA",
                                      "zipCode": "30313"}}}


def test_normalize_public_records():
    assert e164("(323) 357-1999") == "+13233571999" and e164("1-800-555-0100") == "+18005550100"
    assert email("Bad Email") is None and email(" A@B.COM ") == "a@b.com"
    c = fmcsa.normalize(CARRIER_ROW)
    assert c["id"] == "C-391314" and c["name"] == "PIAZZA TRUCKING" and c["mc"] == "MC-231685"
    assert c["phone"] == "+13233571999" and c["email"] == "enrique@piazzatrucking.com" and c["source"] == "fmcsa_census"
    s = edgar.normalize(SEC_SUB)
    assert s["id"] == "S-21344" and s["state"] == "GA" and s["phone"] == "+14046762121"
    assert edgar.is_shipper("2080") and edgar.is_shipper("5311") and not edgar.is_shipper("2834")
    assert not edgar.is_shipper("7372")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_FILE", str(tmp_path / "world.json"))
    monkeypatch.setenv("SIM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPS_DB", str(tmp_path / "ops.db"))
    monkeypatch.setenv("MOCK_INTEGRATIONS", "0")
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    from sim.world import price_lane
    c, s = fmcsa.normalize(CARRIER_ROW), edgar.normalize(SEC_SUB)
    k = {**s, "id": "S-99", "cik": 99, "name": "CONSIGNEE DC", "lat": 34.0, "lon": -118.2}
    c.update(lat=33.95, lon=-118.17, terms="Net 30")
    s.update(lat=33.76, lon=-84.39, terms="Net 30", directory={"phone": s["phone"], "source": s["source"]})
    w = {"fsc_per_mile": 0.433, "carriers": [c], "shippers": [s, k], "lanes": [{
        "id": "LN-1", "shipper_id": s["id"], "carrier_id": c["id"], "consignee_id": k["id"],
        "origin": "Atlanta, GA", "destination": "South Gate, CA", **price_lane(2210, random.Random(1)),
        "equipment": "53' Reefer", "commodity": "Packaged food, palletized", "nmfc_class": 70, "temp_f": 34,
        "free_time_min": 120, "detention_rate_hr": 75.0, "transit_hours": 64.0,
        "accessorials": {"lumper": "at cost", "layover": 250.0, "tonu": 150.0, "stop_off": 75.0}}]}
    (tmp_path / "world.json").write_text(json.dumps(w))
    return w


def test_enrichment_uses_public_registration(world):
    fm = tools.fmcsa_lookup("391314")
    assert fm["phone"] == "+13233571999" and fm["authority_status"] == "ACTIVE"
    en = tools.enrich_contact("C-391314", "dispatch")
    assert en["email"] == "enrique@piazzatrucking.com" and en["confidence"] == 0.9 and en["alternates"] == []
    assert tools.website_for(world["carriers"][0]) == "https://piazzatrucking.com"
    assert "error" in tools.enrich_contact("C-0", "dispatch")


def _run(world, scenario, seed=3):
    from sim import generator as g
    rnd = random.Random(seed)
    load = g.new_load(world, rnd)
    t0 = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
    steps = g.lifecycle(load, scenario, rnd, t0=t0)
    assert [st.at for st in steps] == sorted(st.at for st in steps) and steps[0].at == t0
    return g, load, [ev for st in steps for ev in st.make()]


@pytest.mark.parametrize("scenario", sorted(__import__("sim.generator").generator.SCENARIOS))
def test_lifecycle_paper_trail_routes_cleanly(world, scenario):
    from router import main as router
    g, load, evs = _run(world, scenario)
    occurred = [e["occurred_at"] for e in evs]
    assert occurred == sorted(occurred)
    routed = [e for e in evs if router.plan(e)]
    invoices = [e for e in evs if e["type"] == "carrier_invoice.received"]
    inv = invoices[0]["payload"]
    assert inv["carrier"]["usdot"] == "391314" and inv["shipper"]["name"] == "COCA COLA CO"
    assert all(router.invalid(e["payload"]) is None for e in routed)
    assert {e["type"] for e in routed} >= {"carrier_invoice.received", "shipper_payment.received"}
    assert {e["type"] for e in evs} >= {"load.tendered", "load.status", "gps.ping", "pod.received",
                                        "email.received", "shipper_invoice.issued", "bank.transaction"}
    lines = round(sum(li["amount"] for li in inv["line_items"]), 2)
    if scenario == "math_error":
        assert inv["invoice_amount"] > lines
    else:
        assert inv["invoice_amount"] == lines
    if scenario in ("clean", "duplicate_invoice", "duplicate_renumbered", "shipper_short_pay", "pod_unsigned"):
        assert inv["invoice_amount"] == inv["contract_rate"]
    if scenario in ("rate_mismatch", "fsc_error", "math_error", "lumper_no_receipt", "detention_unsupported"):
        assert inv["invoice_amount"] > inv["contract_rate"] * 1.02  # must trip the AP contract-tolerance check
    if scenario == "detention_unsupported":
        assert inv["detention_hours_claimed"] == 2 and inv["gps_dwell_min"] <= inv["free_time_min"]
    if scenario == "duplicate_invoice":
        assert len(invoices) == 2 and router._keys(invoices[0])[1] == router._keys(invoices[1])[1]
    if scenario == "duplicate_renumbered":
        assert len(invoices) == 2 and invoices[0]["payload"]["bol"] == invoices[1]["payload"]["bol"]
        assert router._keys(invoices[0])[1] != router._keys(invoices[1])[1]
        from agents.carrier_ap.tools import three_way_match
        duplicate_match = three_way_match({**invoices[1]["payload"],
                                           "duplicate_of": invoices[0]["payload"]["invoice_id"]})
        assert "P-007" in duplicate_match["policy_ids"]
    if scenario == "pod_unsigned":
        assert inv["pod_signed"] is False
    if scenario in ("bank_change", "factoring_unverified"):
        bc = next(e["payload"] for e in evs if e["type"] == "bank_change.requested")
        assert (bc["from"] != bc["registered_email"]) == (scenario == "bank_change")
    pay = next(e["payload"] for e in evs if e["type"] == "shipper_payment.received")
    assert pay["chase"] == (scenario == "shipper_short_pay")
    for ref in invoices[0]["evidence_refs"]:
        kind, _, name = ref.partition(":")
        sub, ext = ("docs", "pdf") if kind == "doc" else ("mail", "eml")
        assert (g.data_dir() / sub / f"{name}.{ext}").stat().st_size > 500
    g.record_answer(load, scenario, load["planted"])
    key = json.loads((g.data_dir() / "answer_key.jsonl").read_text().splitlines()[-1])
    assert key["scenario"] == scenario and key["expected"]


@pytest.mark.parametrize("scenario", sorted(
    set(__import__("sim.generator").generator.SCENARIOS)
    - {"clean", "duplicate_invoice", "duplicate_renumbered", "shipper_short_pay", "bank_change",
       "detention_supported"}))
def test_exception_invoices_are_held_by_ap_policy(world, scenario):
    from agents.carrier_ap.tools import three_way_match
    _, _, evs = _run(world, scenario)
    invoice = next(e["payload"] for e in evs if e["type"] == "carrier_invoice.received")
    assert not three_way_match(invoice)["matched"]


def test_unsupported_detention_without_receipt_is_held_over_contract(world):
    from agents.carrier_ap.tools import three_way_match
    world["lanes"][0].update(contract_rate=8000, linehaul=8000)
    _, _, evs = _run(world, "detention_unsupported")
    invoice = next(e["payload"] for e in evs if e["type"] == "carrier_invoice.received")
    match = three_way_match(invoice)
    assert not match["matched"] and "P-014" in match["policy_ids"]


def test_gps_breadcrumbs_follow_the_lane(world):
    _, load, evs = _run(world, "clean")
    pings = [e["payload"] for e in evs if e["type"] == "gps.ping"]
    assert pings[0]["lat"] == load["shipper"]["lat"] and pings[-1]["lat"] == load["consignee"]["lat"]
    assert pings[-1]["milestone"] == "delivery.departed" and pings[-1]["dwell_min"] > 0
