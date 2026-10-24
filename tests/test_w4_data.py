import json
import random

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
    c, s = fmcsa.normalize(CARRIER_ROW), edgar.normalize(SEC_SUB)
    s["directory"] = {"phone": s["phone"], "source": s["source"]}
    w = {"carriers": [c], "shippers": [s], "lanes": [{
        "id": "LN-1", "shipper_id": s["id"], "carrier_id": c["id"], "origin": "Atlanta, GA",
        "destination": "South Gate, CA", "contract_rate": 1850.0, "free_time_min": 120, "detention_rate_hr": 150.0}]}
    (tmp_path / "world.json").write_text(json.dumps(w))
    return w


def test_enrichment_uses_public_registration(world):
    fm = tools.fmcsa_lookup("391314")
    assert fm["phone"] == "+13233571999" and fm["authority_status"] == "ACTIVE"
    en = tools.enrich_contact("C-391314", "dispatch")
    assert en["email"] == "enrique@piazzatrucking.com" and en["confidence"] == 0.9 and en["alternates"] == []
    assert tools.website_for(world["carriers"][0]) == "https://piazzatrucking.com"
    assert "error" in tools.enrich_contact("C-0", "dispatch")


@pytest.mark.parametrize("scenario", ["clean", "detention_unsupported", "detention_supported", "shipper_short_pay",
                                      "bank_change", "duplicate_invoice"])
def test_lifecycle_events_route_cleanly(world, scenario):
    from router import main as router
    from sim import generator as g
    rnd = random.Random(3)
    load = g.new_load(world, rnd)
    steps = g.lifecycle(load, scenario, rnd)
    evs = [e for _, e in steps]
    routed = [e for e in evs if router.plan(e)]
    invoices = [e for e in evs if e["type"] == "carrier_invoice.received"]
    inv = invoices[0]["payload"]
    assert inv["carrier"]["usdot"] == "391314" and inv["shipper"]["name"] == "COCA COLA CO"
    assert inv["invoice_amount"] == sum(li["amount"] for li in inv["line_items"])
    assert all(router.invalid(e["payload"]) is None for e in routed)
    assert {e["type"] for e in routed} >= {"carrier_invoice.received", "shipper_payment.received"}
    assert not any(e["type"] in ("gps.ping", "pod.received", "load.tendered") for e in routed)
    if scenario == "detention_unsupported":
        assert inv["detention_hours_claimed"] == 2 and inv["gps_dwell_min"] <= inv["free_time_min"]
    if scenario == "duplicate_invoice":
        assert len(invoices) == 2 and router._keys(invoices[0])[1] == router._keys(invoices[1])[1]
    for ref in invoices[0]["evidence_refs"]:
        assert (g.data_dir() / "docs" / f"{ref.removeprefix('doc:')}.json").exists()
    g.record_answer(load, scenario)
    key = (g.data_dir() / "answer_key.jsonl").read_text().splitlines()
    assert json.loads(key[-1])["scenario"] == scenario
