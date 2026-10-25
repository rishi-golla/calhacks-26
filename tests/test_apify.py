"""Paid lookup starts, scope and evidence must remain safe across retries/restarts."""
import asyncio
import json
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import update

from common.envelope import TaskEnvelope
from common.policy import Decision
from common.runtime_store import RuntimeStore
from integrations import apify


@pytest.fixture
async def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("APIFY_TOKEN", "test-api-secret")
    for key in ("APIFY_FMCSA_ACTOR_ID", "APIFY_FMCSA_BUILD", "APIFY_CONTACT_ACTOR_ID", "APIFY_CONTACT_BUILD"):
        monkeypatch.delenv(key, raising=False)
    store = RuntimeStore(f"sqlite+aiosqlite:///{tmp_path / 'apify.sqlite'}")
    agent = SimpleNamespace(name="verification", store=store, _assert_execution=AsyncMock())
    env = TaskEnvelope(task_type="verify_dispute", case_id="test-case")
    yield agent, env
    await store.close()


def carrier(**extra):
    return {"dotNumber": "80806", "docketNumbers": ["MC135797"], "legalName": "SOURCE CARRIER INC",
            "operatingAuthorityStatus": "authorized", "usdotStatus": "active", "outOfService": False,
            "phone": "(555) 555-0100", "email": "dispatch@example.test", "checkedAt": "2026-10-24T11:00:00Z",
            "sources": {"authority": {"source": "safer", "asOf": "2026-10-24"}}, **extra}


def responses(requests, items=None, status="SUCCEEDED"):
    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-api-secret"
        assert "secret" not in str(request.url)
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "RUN12345", "status": "READY"}})
        if "/actor-runs/" in request.url.path:
            return httpx.Response(200, json={"data": {"id": "RUN12345", "status": status,
                                                      "defaultDatasetId": "DATA12345"}})
        return httpx.Response(200, json=items if items is not None else [carrier()])
    return handler


async def test_lookup_real_adapter_schema_bounds_and_shared_cache(setup):
    agent, env = setup
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(responses(requests))) as client:
        result = await apify.lookup_carrier(agent, env, "task-one", usdot="USDOT 080806", client=client)
        replay = await apify.lookup_carrier(agent, env, "task-two", usdot="80806", client=client)
    assert result["available"] and not result["cached"] and replay["cached"]
    assert len(requests) == 3
    submitted = json.loads(requests[0].content)
    assert submitted == {"dotNumbers": ["80806"], "docketNumbers": [], "liveCheck": True,
                         "onlyChanges": False, "maxResults": 1}
    assert requests[0].url.params["maxTotalChargeUsd"] == "0.05"
    assert requests[0].url.params["timeout"] == "60"
    assert requests[0].url.params["memory"] == "512"
    assert result["source_simulated"] is False
    assert result["identity_match"] and result["records"][0]["authority_confirmed_active"] is True
    assert not result["contact_authority_verified"] and not result["financial_authorization"]
    assert result["records"][0]["freshness"]["authority"]["as_of"] == "2026-10-24"
    assert result["evidence_refs"][0] == "apify://runs/RUN12345"


async def test_mc_lookup_never_includes_actor_default_example_carrier(setup):
    agent, env = setup
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(responses(requests))) as client:
        result = await apify.lookup_carrier(agent, env, "task", mc="MC-135797", client=client)
    assert result["available"]
    assert json.loads(requests[0].content)["dotNumbers"] == []
    assert json.loads(requests[0].content)["docketNumbers"] == ["MC135797"]


async def test_ambiguous_start_is_never_retried_after_restart(setup):
    agent, env = setup
    requests = []
    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("secret provider body", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        result = await apify.lookup_carrier(agent, env, "task", usdot="80806", client=client)
        replay = await apify.lookup_carrier(agent, env, "new-task", usdot="80806", client=client)
    assert len(requests) == 1 and result["requires_reconciliation"] and replay["requires_reconciliation"]
    assert "secret" not in json.dumps(result)
    assert not result["available"] and not replay["available"]


async def test_persisted_start_intent_prevents_duplicate_after_process_crash(setup, monkeypatch):
    agent, env = setup
    async def crashed(_row, **values):
        if values.get("run_id"):
            raise RuntimeError("process crashed before received run ID persisted")
    original = apify.LookupStore.set
    monkeypatch.setattr(apify.LookupStore, "set", lambda _self, row, **values: crashed(row, **values))
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(responses(requests))) as client:
        with pytest.raises(RuntimeError, match="process crashed"):
            await apify.lookup_carrier(agent, env, "task", usdot="80806", client=client)
        monkeypatch.setattr(apify.LookupStore, "set", original)
        async with agent.store.engine.begin() as conn:
            await conn.execute(update(apify.runs).values(updated_at=time.time() - 21))
        result = await apify.lookup_carrier(agent, env, "task", usdot="80806", client=client)
    assert len(requests) == 1 and result["requires_reconciliation"]


async def test_known_run_is_polled_again_without_new_start_after_get_failure(setup):
    agent, env = setup
    requests = []
    successful = responses(requests)
    failed_once = False
    def handler(request):
        nonlocal failed_once
        if request.method == "GET" and not failed_once:
            failed_once = True
            requests.append(request)
            return httpx.Response(503, text="private provider response")
        return successful(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await apify.lookup_carrier(agent, env, "task", usdot="80806", client=client)
        replay = await apify.lookup_carrier(agent, env, "task-two", usdot="80806", client=client)
    assert result["can_resume_lookup"] and replay["available"]
    assert sum(request.method == "POST" for request in requests) == 1


async def test_concurrent_callers_share_one_paid_actor_start(setup):
    agent, env = setup
    requests = []
    handler = responses(requests)
    async def slow(request):
        if request.method == "POST":
            await asyncio.sleep(0.02)
        return handler(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        results = await asyncio.gather(*(apify.lookup_carrier(agent, env, f"task-{i}", usdot="80806", client=client)
                                        for i in range(2)))
    assert all(result["available"] for result in results)
    assert sum(request.method == "POST" for request in requests) == 1


async def test_expired_completed_record_can_refresh_once(setup):
    agent, env = setup
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(responses(requests))) as client:
        await apify.lookup_carrier(agent, env, "task", usdot="80806", client=client)
        async with agent.store.engine.begin() as conn:
            await conn.execute(update(apify.runs).values(expires_at=time.time() - 1))
        result = await apify.lookup_carrier(agent, env, "task-new", usdot="80806", client=client)
    assert result["available"] and not result["cached"]
    assert sum(request.method == "POST" for request in requests) == 2


async def test_legacy_cached_result_renormalizes_existing_dataset_without_new_paid_run(setup):
    agent, env = setup
    requests = []
    insurance = {"bipdRequired": 1_000_000, "bipdOnFile": 5_000_000,
                 "cargoRequired": False, "cargoOnFile": False,
                 "bondRequired": True, "bondOnFile": True}
    async with httpx.AsyncClient(transport=httpx.MockTransport(responses(requests, [carrier(insurance=insurance)]))) as client:
        original = await apify.lookup_carrier(agent, env, "task", usdot="80806", client=client)
        original.pop("normalization_version")
        original["records"][0]["insurance"] = {"bipdRequired": 1_000_000, "bipdOnFile": 5_000_000}
        expiry = time.time() + 100
        async with agent.store.engine.begin() as conn:
            await conn.execute(update(apify.runs).values(result=json.dumps(original), expires_at=expiry))
        refreshed = await apify.lookup_carrier(agent, env, "new-task", usdot="80806", client=client)
    assert refreshed["records"][0]["insurance"] == insurance
    assert refreshed["cached"] and refreshed["normalization_version"] == apify.NORMALIZATION_VERSION
    assert refreshed["fetched_at"] == original["fetched_at"]
    assert sum(request.method == "POST" for request in requests) == 1
    assert len(requests) == 5  # Existing run + dataset were re-read using GET only.
    async with agent.store.engine.connect() as conn:
        from sqlalchemy import select
        assert (await conn.execute(select(apify.runs.c.expires_at))).scalar_one() == expiry


@pytest.mark.parametrize("value", [None, "", True, "bad", "0", "123456789", "https://private.invalid/"])
async def test_invalid_or_missing_identifiers_never_start_actor(setup, value):
    agent, env = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("must not request"))) as client:
        result = await apify.lookup_carrier(agent, env, "task", usdot=value, client=client)
    assert not result["available"]


async def test_registration_mismatch_does_not_become_verified_evidence(setup):
    agent, env = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(responses([], [carrier(dotNumber="99999")]))) as client:
        result = await apify.lookup_carrier(agent, env, "task", usdot="80806", client=client)
    assert not result["available"] and not result["identity_match"] and not result["records"]
    assert result["error"] == "no_matching_source_records"


async def test_census_active_does_not_prove_operating_authority():
    record = apify.normalize_carriers([carrier(operatingAuthorityStatus=None)], usdot="80806", mc=None)[0]
    assert record["usdot_status"] == "active" and record["authority_confirmed_active"] is None


def test_carrier_insurance_preserves_dollar_amounts_and_boolean_coverage_signals():
    insurance = {"bipdRequired": 1_000_000, "bipdOnFile": 5_000_000,
                 "cargoRequired": False, "cargoOnFile": False,
                 "bondRequired": True, "bondOnFile": True}
    record = apify.normalize_carriers([carrier(insurance=insurance)], usdot="80806", mc=None)[0]
    assert record["insurance"] == insurance
    assert not record["financial_authorization"] and not record["contact_authority_verified"]
    malformed = {"bipdRequired": True, "bipdOnFile": float("nan"), "cargoRequired": 1,
                 "cargoOnFile": "false", "bondRequired": None, "bondOnFile": "true"}
    record = apify.normalize_carriers([carrier(insurance=malformed)], usdot="80806", mc=None)[0]
    assert record["insurance"] == {}


def test_carrier_dockets_are_canonical_and_deduplicated():
    record = apify.normalize_carriers([carrier(docketNumbers=["MC135797", "MC-135797", "MC0135797", None,
                                                              "FF12345", "FF12345"])],
                                      usdot="80806", mc="MC135797")[0]
    assert record["docket_numbers"] == ["MC135797", "FF12345"]


@pytest.mark.parametrize("value", ["5608078629001", "6402702258112", "555-555-0100", "(123) 555-2671",
    "(415) 155-2671", "(415) 000-2671", "020 8366 1177", "+999 123456789", "+1 123 555 2671",
    "555-2671", "Call (415) 555-2671", "https://carrier.com/4155552671", "415-555-2671 extra 99"])
def test_scraped_phone_candidates_reject_ids_invalid_numbers_and_ambiguous_text(value):
    records = apify.normalize_companies([{"phones": [value]}], "https://carrier.com/")
    assert records[0]["phones"] == []


def test_scraped_phone_candidates_keep_valid_us_and_explicit_international_without_authorization():
    record = apify.normalize_companies([{
        "phones": ["(415) 555-2671", "4155552671", "+1 415 555 2671", "+44 20 8366 1177"],
        "phoneNumbers": ["+442083661177", "(415) 555-2671 ext. 42"],
        "phonesUncertain": ["6402702258112", "(415) 555-2671 x42"],
    }], "https://carrier.com/")[0]
    assert record["phones"] == ["(415) 555-2671", "+44 20 8366 1177", "(415) 555-2671 ext. 42"]
    assert not record["contact_authority_verified"] and not record["financial_authorization"]


async def test_phone_normalization_upgrade_reads_existing_dataset_without_paid_start(setup, monkeypatch):
    agent, env = setup
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])
    requests = []
    items = [{"url": "https://carrier.com/", "phones": ["5608078629001", "6402702258112", "(415) 555-2671"]}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(responses(requests, items))) as client:
        original = await apify.scrape_company(agent, env, "task", url="https://carrier.com/", client=client)
        original["normalization_version"] = 1
        original["records"][0]["phones"] = items[0]["phones"]
        expiry = time.time() + 100
        async with agent.store.engine.begin() as conn:
            await conn.execute(update(apify.runs).values(result=json.dumps(original), expires_at=expiry))
        refreshed = await apify.scrape_company(agent, env, "next-task", url="https://carrier.com/", client=client)
        replay = await apify.scrape_company(agent, env, "another-task", url="https://carrier.com/", client=client)
    assert refreshed["records"][0]["phones"] == ["(415) 555-2671"]
    assert replay["records"] == refreshed["records"]
    assert refreshed["cached"] and refreshed["normalization_version"] == apify.NORMALIZATION_VERSION
    assert refreshed["fetched_at"] == original["fetched_at"]
    assert sum(request.method == "POST" for request in requests) == 1
    assert len(requests) == 5
    async with agent.store.engine.connect() as conn:
        from sqlalchemy import select
        assert (await conn.execute(select(apify.runs.c.expires_at))).scalar_one() == expiry


async def test_policy_denial_and_fixture_mode_never_spend(setup, monkeypatch):
    agent, env = setup
    monkeypatch.setattr(apify.policy, "check", lambda *_: Decision("deny", ["test-policy"]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("must not request"))) as client:
        assert (await apify.lookup_carrier(agent, env, "task", usdot="80806", client=client))["error"] == "apify_lookup_denied"
        monkeypatch.setenv("FREIGHTOPS_MODE", "fixture")
        assert (await apify.lookup_carrier(agent, env, "task", usdot="80806", client=client))["error"] == "live_apify_requires_live_mode"


@pytest.mark.parametrize("url", ["http://carrier.com", "https://127.0.0.1/", "https://localhost/", "https://foo.internal/",
    "https://x:y@carrier.com/", "https://carrier.com/?token=secret", "https://carrier.com/#secret",
    "https://carrier.com:9443/", "https://example.com/", "https://linktr.ee/a"])
async def test_private_or_credentialed_websites_rejected_without_request(setup, url):
    agent, env = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("must not request"))) as client:
        assert not (await apify.scrape_company(agent, env, "task", url=url, client=client))["available"]


async def test_mixed_public_private_dns_is_rejected(setup, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("10.0.0.1", 443))])
    with pytest.raises(ValueError, match="public addresses"):
        await apify.public_website("https://carrier.com/")


async def test_company_scrape_bounded_public_page_and_no_lead_enrichment(setup, monkeypatch):
    agent, env = setup
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])
    requests = []
    items = [{"url": "https://carrier.com/", "emails": ["dispatch@carrier.com"], "phones": ["555-555-0100"]},
             {"url": "https://other.com/", "emails": ["wrong@other.com"]}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(responses(requests, items))) as client:
        result = await apify.scrape_company(agent, env, "task", url="https://carrier.com", client=client)
    body = json.loads(requests[0].content)
    assert body["proxyConfig"] == {"useApifyProxy": True}
    assert body["maxRequests"] == body["maxRequestsPerStartUrl"] == 1
    assert body["maxDepth"] == body["maximumLeadsEnrichmentRecords"] == 0
    assert not body["considerChildFrames"] and not body["useBrowser"]
    assert not any(body["scrapeSocialMediaProfiles"].values())
    assert len(result["records"]) == 1 and result["records"][0]["emails"] == ["dispatch@carrier.com"]
    assert not result["contact_authority_verified"]


@pytest.mark.parametrize("status", [400, 401, 429, 500])
async def test_provider_errors_are_safe_and_starts_not_replayed(setup, status):
    agent, env = setup
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="secret credentials echoed from provider")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await apify.lookup_carrier(agent, env, "task", usdot="80806", client=client)
        await apify.lookup_carrier(agent, env, "retry", usdot="80806", client=client)
    assert len(calls) == 1 and not result["available"] and "secret" not in json.dumps(result)


async def test_configuration_rejects_unreviewed_actor_and_nonversion_build(setup, monkeypatch):
    monkeypatch.setenv("APIFY_FMCSA_ACTOR_ID", "arbitrary/actor")
    monkeypatch.setenv("APIFY_FMCSA_BUILD", "latest")
    status = apify.apify_configuration_status("carrier")
    assert not status["available"]
    assert status["invalid_configuration"] == ["APIFY_FMCSA_ACTOR_ID", "APIFY_FMCSA_BUILD"]
    monkeypatch.delenv("APIFY_TOKEN")
    assert apify.apify_configuration_status("company")["missing_configuration"] == ["APIFY_TOKEN"]


@pytest.mark.parametrize("names,expected", [
    (("SOURCE CARRIER INC",), True), (("Source Carrier, Inc.",), True), (("Source Carrier",), True),
    (("SC LOGISTICS",), True),  # the DBA counts
    (("Sierra Haul LLC",), False), (("Carrier Inc",), False),  # one shared word is not an identity
    ((), None), ((None, ""), None),
])
def test_registered_name_must_match_the_case_carrier(names, expected):
    record = apify.normalize_carriers([carrier(dbaName="SC LOGISTICS")], usdot="80806", mc=None, names=names)[0]
    assert record["name_match"] is expected
    assert record["identity_match"] is (expected is not False)


@pytest.mark.parametrize("ours,registered", [("J.B. Hunt Transport, Inc.", "J B HUNT TRANSPORT INC"),
                                             ("O'Brien & Sons Trucking", "OBRIEN AND SONS TRUCKING LLC"),
                                             ("Cherokee Freight Lines", "CHEROKEE FREIGHT LINES STOCKTON LLC")])
def test_name_match_ignores_punctuation_suffixes_and_locations(ours, registered):
    assert apify.carrier_name_matches({"legal_name": registered}, [ours]) is True


async def test_cached_lookup_is_judged_against_each_cases_carrier_name(setup):
    agent, env = setup
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(responses(requests))) as client:
        ours = await apify.lookup_carrier(agent, env, "task", usdot="80806", names=["Source Carrier Inc"],
                                          client=client)
        other = await apify.lookup_carrier(agent, env, "next", usdot="80806", names=["Sierra Haul LLC"],
                                           client=client)
    assert sum(r.method == "POST" for r in requests) == 1, "the name verdict never costs another paid run"
    assert ours["identity_match"] is True and "identity_mismatch" not in ours
    assert other["cached"] and other["available"] is True and other["identity_match"] is False
    assert other["records"][0]["name_match"] is False
    assert other["identity_mismatch"] == {"reason": "registered_name_differs", "expected_names": ["Sierra Haul LLC"],
                                          "registered_names": ["SOURCE CARRIER INC"]}
