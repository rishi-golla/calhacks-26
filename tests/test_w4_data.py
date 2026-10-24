import json
import random
from datetime import UTC, datetime, timedelta

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
def test_lifecycle_emits_raw_arrivals_only(world, scenario):
    g, load, evs = _run(world, scenario)
    occurred = [e["occurred_at"] for e in evs]
    assert occurred == sorted(occurred)
    types = {event["type"] for event in evs}
    assert types >= {"load.tendered", "load.status", "gps.ping", "pod.received",
                     "email.received", "shipper_invoice.issued", "bank.file_appended"}
    assert not types & {"carrier_invoice.received", "bank_change.requested",
                        "shipper_payment.received", "bank.transaction"}
    tender = next(event["payload"] for event in evs if event["type"] == "load.tendered")
    assert tender["operator_overrides"] == {} and tender["free_time_min"] == load["free_time_min"]
    emails = [event["payload"] for event in evs if event["type"] == "email.received"]
    expected_email_keys = {"mail_id", "from", "to", "subject", "sent_at", "attachments"}
    assert emails and all(set(payload) == expected_email_keys for payload in emails)
    assert all(not {"case_id", "load_id", "invoice_amount", "amount", "paid_amount"} & payload.keys()
               for payload in emails)
    g.record_answer(load, scenario, load["planted"])
    key = json.loads((g.data_dir() / "answer_key.jsonl").read_text().splitlines()[-1])
    assert key["scenario"] == scenario and key["expected"]

def test_gps_breadcrumbs_follow_the_lane(world):
    _, load, evs = _run(world, "clean")
    pings = [e["payload"] for e in evs if e["type"] == "gps.ping"]
    assert pings[0]["lat"] == load["shipper"]["lat"] and pings[-1]["lat"] == load["consignee"]["lat"]
    assert pings[-1]["milestone"] == "delivery.departed" and pings[-1]["dwell_min"] > 0


def test_backdated_lifecycle_has_immediate_events(world):
    from sim import generator as g
    rnd = random.Random(3)
    load = g.new_load(world, rnd)
    now = datetime.now(UTC)
    steps = g.lifecycle(load, "clean", rnd, t0=now - timedelta(hours=96))
    ready = [step for step in steps if step.at <= now]
    assert ready
    assert any(event["type"] == "load.tendered" for step in ready for event in step.make())
