"""Seeded carriers must be real, Apify-confirmed identities, and swapping them in must not disturb hero fixtures."""
import json

import pytest

from scripts import seed_real_carriers as seed

RUN = {"provider": "apify", "actor": "humble-echidna~fmcsa-carrier-check", "build": "0.1.2", "run_id": "RUN12345",
       "dataset_id": "DATA12345", "checked_at": "2026-10-25T10:00:00Z", "charged_items": 1}


def candidate(**extra):
    return {"id": "C-80806", "kind": "carrier", "usdot": "80806", "mc": "MC-135797", "name": "SOURCE CARRIER",
            "legal_name": "SOURCE CARRIER INC", "phone": "+15555550100", "email": "ap@sourcecarrier.com",
            "city": "Reno", "state": "NV", "source": "fmcsa_census", "website": "https://sourcecarrier.com/", **extra}


def item(**extra):
    return {"dotNumber": "80806", "docketNumbers": ["MC135797"], "legalName": "SOURCE CARRIER INC",
            "operatingAuthorityStatus": "authorized", "usdotStatus": "active", "outOfService": False, **extra}


def test_website_comes_only_from_a_company_email_domain():
    assert seed.website_for("dispatch@SourceCarrier.com") == "https://sourcecarrier.com/"
    assert seed.website_for("owner@gmail.com") is None
    assert seed.website_for(None) is None


def test_isp_mailbox_domain_is_not_the_carriers_website():
    assert seed.owns_site(candidate(), "")
    assert not seed.owns_site(candidate(website="https://frontier.com/"), "<title>Frontier Internet</title>")
    assert seed.owns_site(candidate(website="https://scc-nv.com/"), "<h1>Source Carrier, since 1972</h1>")


@pytest.mark.parametrize("mismatch", [{"docketNumbers": ["MC999999"]}, {"dotNumber": "80807"},
                                      {"operatingAuthorityStatus": "not authorized"}, {"outOfService": True},
                                      {"usdotStatus": "inactive"}])
def test_only_matching_active_registrations_are_confirmed(mismatch):
    assert seed.confirmed(candidate(), [item(**mismatch)], RUN) is None


def test_confirmed_carrier_restates_the_live_record_with_provenance():
    record = seed.confirmed(candidate(), [item(legalName="SOURCE CARRIER CORP", dbaName="SCC")], RUN)
    assert (record["name"], record["legal_name"], record["authority_status"]) == ("SCC", "SOURCE CARRIER CORP",
                                                                                  "ACTIVE")
    assert record["verified_by"]["evidence_ref"] == "apify://runs/RUN12345"
    assert record["source"] == "fmcsa_census"


def test_apply_replaces_only_fixtures_in_state_and_remaps_lanes():
    world = {"sources": {"offline": "fixture"},
             "carriers": [{"id": "C-311", "usdot": "3141592", "state": "CA", "source": "prd_hero_fixture"},
                          {"id": "C-OFF-1", "usdot": "2000000", "state": "NV", "source": "offline_fixture",
                           "terms": "QuickPay 2% / 2 days"},
                          {"id": "C-OFF-2", "usdot": "2000001", "state": "ID", "source": "offline_fixture"}],
             "lanes": [{"id": "L1", "carrier_id": "C-OFF-1"}, {"id": "L2", "carrier_id": "C-311"},
                       {"id": "L3", "carrier_id": "C-OFF-2"}]}
    reno = candidate()
    sparks = candidate(id="C-90000", usdot="90000", city="Sparks")
    stockton = candidate(id="C-70000", usdot="70000", city="Stockton", state="CA")
    seeded, remap = seed.apply(world, {"run": RUN, "carriers": [reno, sparks, stockton]})
    # Same state first, then any unused carrier; the hero keeps its record and hands over its lane.
    assert remap == {"C-311": "C-70000", "C-OFF-1": "C-80806", "C-OFF-2": "C-90000"}
    assert seeded["carriers"][0] == world["carriers"][0]
    assert seeded["carriers"][2]["terms"] == "QuickPay 2% / 2 days"
    assert [lane["carrier_id"] for lane in seeded["lanes"]] == ["C-80806", "C-70000", "C-90000"]
    assert "RUN12345" in seeded["sources"]["carriers"]
    with pytest.raises(SystemExit):
        seed.apply(world, {"run": RUN, "carriers": [reno, sparks]})


def test_committed_snapshot_is_real_confirmed_and_unique():
    snapshot = json.loads(seed.SNAPSHOT.read_text())
    carriers = snapshot["carriers"]
    assert len({c["usdot"] for c in carriers}) == len({c["website"] for c in carriers}) == len(carriers)
    for c in carriers:
        assert c["id"] == f"C-{c['usdot']}" and c["mc"].startswith("MC-") and c["source"] == "fmcsa_census"
        assert c["website"].startswith("https://") and c["authority_status"] == "ACTIVE"
        assert c["verified_by"]["run_id"] == snapshot["run"]["run_id"]
        assert not seed.PASSENGER_WORDS.search(f"{c['legal_name']} {c['name']}")


def test_short_fetch_keeps_the_committed_snapshot(monkeypatch, tmp_path):
    snapshot = tmp_path / "fmcsa_carriers.json"
    snapshot.write_text("committed")
    monkeypatch.setattr(seed, "SNAPSHOT", snapshot)
    monkeypatch.setattr(seed, "census_candidates", lambda state: [candidate(address="1 Main St, Reno, NV")])
    monkeypatch.setattr(seed, "apify_confirm", lambda dots: ([item()], RUN))
    monkeypatch.setattr(seed.geo, "geocode", lambda *_: {"lat": 39.5, "lon": -119.8, "geo_source": "test"})

    async def live(candidates):
        return candidates

    monkeypatch.setattr(seed, "live_websites", live)
    world = {"carriers": [{"id": f"C-OFF-{i}", "state": "NV", "source": "offline_fixture"} for i in range(2)],
             "lanes": []}
    with pytest.raises(SystemExit):
        seed.fetch(world)
    assert snapshot.read_text() == "committed"


def test_seeding_needs_an_existing_world(monkeypatch, tmp_path):
    monkeypatch.setenv("WORLD_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr("sys.argv", ["seed_real_carriers"])
    with pytest.raises(SystemExit, match="build a world first"):
        seed.main()


def test_hero_carriers_keep_their_records_but_hand_their_lanes_to_real_carriers():
    world = {"carriers": [{"id": "C-311", "usdot": "3141592", "state": "NV", "source": "prd_hero_fixture"},
                          {"id": "C-77", "usdot": "7700770", "state": "NV", "source": "prd_hero_fixture"},
                          {"id": "C-REAL", "usdot": "1", "state": "NV", "source": "fmcsa_census"}],
             "lanes": [{"id": "L1", "carrier_id": "C-311"}, {"id": "L2", "carrier_id": "C-REAL"}]}
    seeded, remap = seed.apply(world, {"run": RUN, "carriers": [candidate()]})
    assert remap == {"C-311": "C-80806"}, "a hero carrier with no lanes is left alone"
    assert [c["id"] for c in seeded["carriers"]] == ["C-311", "C-80806", "C-77", "C-REAL"]
    assert [lane["carrier_id"] for lane in seeded["lanes"]] == ["C-80806", "C-REAL"]
    assert seed.apply(seeded, {"run": RUN, "carriers": [candidate()]})[1] == {}, "re-applying changes nothing"
