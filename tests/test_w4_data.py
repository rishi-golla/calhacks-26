import copy
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
    other = fmcsa.normalize({**CARRIER_ROW, "dot_number": "391315", "legal_name": "SAGE VALLEY CARRIERS LLC",
                             "dba_name": "SAGE VALLEY", "phone": "(323) 555-1315",
                             "email_address": "billing@sagevalley.example", "docket1": "231686"})
    k = {**s, "id": "S-99", "cik": 99, "name": "CONSIGNEE DC", "lat": 34.0, "lon": -118.2}
    c.update(lat=33.95, lon=-118.17, terms="Net 30")
    s.update(lat=33.76, lon=-84.39, terms="Net 30", directory={"phone": s["phone"], "source": s["source"]})
    w = {"fsc_per_mile": 0.433, "carriers": [c, other], "shippers": [s, k], "lanes": [{
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


def test_clean_and_decoy_pick_share(world):
    from sim.generator import SCENARIOS, pick

    rng = random.Random(23)
    picks = [pick(rng) for _ in range(2000)]
    clean_and_decoys = {"clean", "clean_decoy", "decoy_detention_supported_small", "decoy_quickpay"}
    share = sum(scenario in clean_and_decoys for scenario in picks) / len(picks)
    assert all(weight > 0 for weight in SCENARIOS.values())
    assert 0.70 <= share <= 0.80


@pytest.mark.parametrize("scenario", sorted(__import__("sim.generator").generator.SCENARIOS))
def test_lifecycle_emits_raw_arrivals_only(world, scenario):
    g, load, evs = _run(world, scenario)
    sim_times = [e["sim_ts"] for e in evs]
    assert sim_times == sorted(sim_times)
    assert all({"event_id", "type", "ts", "sim_ts", "case_hint", "priority", "payload", "source"} <= e.keys()
               for e in evs)
    assert all("occurred_at" not in e for e in evs)
    assert all(e["source"] == "sim" and e["ts"].endswith("Z") and e["sim_ts"].endswith("Z") for e in evs)
    types = {event["type"] for event in evs}
    assert types >= {"load.tendered", "load.dispatched", "load.delivered", "load.status", "gps.ping", "pod.received",
                     "email.received", "shipper_invoice.issued", "bank.event"}
    assert all(event["payload"].get("status") == "loaded" for event in evs if event["type"] == "load.status")
    bank_event = next(event for event in evs if event["type"] == "bank.event")
    assert bank_event["payload"]["record"].startswith("16,")
    assert not types & {"carrier_invoice.received", "bank_change.requested",
                        "shipper_payment.received", "bank.transaction"}
    tender = next(event["payload"] for event in evs if event["type"] == "load.tendered")
    assert tender["operator_overrides"] == {} and tender["free_time_min"] == load["free_time_min"]
    emails = [event["payload"] for event in evs if event["type"] == "email.received"]
    expected_email_keys = {"mail_id", "from", "to", "subject", "sent_at", "attachments"}
    assert emails and all(set(payload) == expected_email_keys for payload in emails)
    assert all(not {"case_id", "load_id", "invoice_amount", "amount", "paid_amount"} & payload.keys()
               for payload in emails)
    assert not types & {"carrier_invoice.received", "bank_change.requested", "shipper_payment.received"}
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


def test_per_load_generation_is_order_independent(world, tmp_path, monkeypatch):
    from sim import generator as g

    loads = [g.new_load(world, random.Random(seed)) for seed in (41, 42)]
    t0 = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)

    def generate(root, ordered):
        monkeypatch.setenv("SIM_DATA_DIR", str(root))
        monkeypatch.setenv("WORLD_FILE", str(root / "world.json"))
        monkeypatch.setenv("SIM_SEED", "701")
        root.mkdir()
        (root / "world.json").write_text(json.dumps(world))
        for load in ordered:
            for step in g.lifecycle(copy.deepcopy(load), "clean", random.Random(99), t0=t0):
                step.make()
            g.record_answer(load, "clean", ts=t0)
        documents = {}
        for path in (root / "docs").glob("*.json"):
            doc = json.loads(path.read_text())
            documents.setdefault(doc["load_id"], {})[path.name] = path.read_bytes()
        answers = {item["load_id"]: item for item in
                   (json.loads(line) for line in (root / "answer_key.jsonl").read_text().splitlines())}
        return documents, answers

    first = generate(tmp_path / "order-a", loads)
    second = generate(tmp_path / "order-b", list(reversed(loads)))
    assert first == second


def test_500_loads_have_unique_invoice_numbers_and_document_paths(world, monkeypatch):
    from sim import generator as g

    writes = []
    original_write = g.docs.write

    def tracked_write(ref, kind, data):
        writes.append(f"{ref}.pdf")
        return original_write(ref, kind, data)

    monkeypatch.setattr(g.docs, "write", tracked_write)
    rnd = random.Random(500)
    t0 = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
    for _ in range(500):
        load = g.new_load(world, rnd)
        for step in g.lifecycle(load, "clean", rnd, t0=t0):
            step.make()
    invoices = [json.loads(path.read_text())["invoice_number"]
                for path in (g.data_dir() / "docs").glob("*.json")
                if json.loads(path.read_text()).get("type") == "carrier_invoice"]
    assert len(writes) == len(set(writes))
    assert len(invoices) == len(set(invoices)) == 500
    registry_path = g.data_dir() / "invoice_registry.json"
    assert registry_path.exists()
    g._INVOICE_REGISTRIES.pop(registry_path)
    assert g._reserve_invoice_number(world["carriers"][0], random.Random(9), "restarted-load") not in invoices
