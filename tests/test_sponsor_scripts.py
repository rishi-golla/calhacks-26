"""Sponsor setup stays read-only unless applied, and never initiates a call."""
import json
import stat
import sys
from types import SimpleNamespace

import httpx
import pytest

from common import voice
from scripts import live_sponsors, setup_cartesia
from scripts.local_env import read_env


@pytest.fixture
def private_root(monkeypatch, tmp_path):
    fixture = (live_sponsors.ROOT / "sim/fixtures/live_concurrency_source.json").read_text()
    source = tmp_path / "sim/fixtures/live_concurrency_source.json"
    source.parent.mkdir(parents=True)
    source.write_text(fixture)
    (source.parent / "team_call_source.json").write_text(
        (live_sponsors.ROOT / "sim/fixtures/team_call_source.json").read_text())
    (tmp_path / "data/live").mkdir(parents=True)
    (tmp_path / ".env").write_text("# Keep existing credentials\nOPENAI_API_KEY='secret key # stays'\n")
    (tmp_path / "data/live/runtime.env").write_text("A2A_PORT=8001\n")
    monkeypatch.setattr(setup_cartesia, "ROOT", tmp_path)
    monkeypatch.setattr(live_sponsors, "ROOT", tmp_path)
    monkeypatch.setattr(setup_cartesia, "load_live_env", lambda: None)
    monkeypatch.setattr(live_sponsors, "load_live_env", lambda: None)
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-test-secret")
    return tmp_path


def provider(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(setup_cartesia.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))


def test_setup_dry_run_neither_creates_resources_nor_updates_files(private_root, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"data": [], "has_more": False})

    provider(monkeypatch, handler)
    before = (private_root / ".env").read_bytes()
    result = setup_cartesia.setup(apply=False, provision_number=True)
    assert not any(item["configured"] for item in result["agents"].values())
    assert not result["number_configured"] and result["calls_placed"] == 0
    assert (private_root / ".env").read_bytes() == before
    assert not (private_root / "data/live/cartesia-setup.json").exists()
    assert len(requests) == 2


def test_setup_reuses_resources_on_second_apply_and_never_dials(private_root, monkeypatch):
    agents, numbers, requests = [], [], []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.cartesia.ai"
        assert request.headers["Cartesia-Version"] == "2026-08-14"
        path = request.url.path
        assert path != "/agents/calls"
        if request.method == "GET":
            return httpx.Response(200, json={"data": agents if path == "/v1/agents" else numbers,
                                            "has_more": False})
        body = json.loads(request.content)
        if path == "/v1/agents":
            item = {"id": f"agent_{len(agents)}", "name": body["name"]}
            agents.append(item)
        else:
            assert path == "/agents/phone-numbers/provision"
            item = {"id": "number_one", "label": body["label"]}
            numbers.append(item)
        return httpx.Response(200, json=item)

    provider(monkeypatch, handler)
    first = setup_cartesia.setup(apply=True, provision_number=True)
    second = setup_cartesia.setup(apply=True, provision_number=True)
    assert len(agents) == 3 and len(numbers) == 1
    assert len([r for r in requests if r.method == "POST"]) == 4
    assert all(item["created"] for item in first["agents"].values())
    assert all(item["configured"] and not item["created"] for item in second["agents"].values())
    assert first["calls_placed"] == second["calls_placed"] == 0
    values = read_env(private_root / ".env")
    assert values["OPENAI_API_KEY"] == "secret key # stays"
    assert values["CARTESIA_FROM_NUMBER_ID"] == "number_one"
    assert stat.S_IMODE((private_root / ".env").stat().st_mode) == 0o600
    assert "cartesia-test-secret" not in json.dumps(second)


def test_setup_detects_duplicate_persona_on_later_page_before_any_create(private_root, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path == "/agents/phone-numbers":
            return httpx.Response(200, json={"data": [], "has_more": False})
        next_page = "starting_after" in request.url.params
        return httpx.Response(200, json={
            "data": [{"id": "agent_second" if next_page else "agent_first",
                      "name": "FreightOps · Verification"}],
            "has_more": not next_page, "next_page": None if next_page else "agent_first"})

    provider(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="Multiple FreightOps"):
        setup_cartesia.setup(apply=True, provision_number=True)
    assert requests[1].url.params["starting_after"] == "agent_first"


def test_setup_reuses_number_from_later_page(private_root, monkeypatch):
    def handler(request):
        assert request.method == "GET"
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [
                {"id": "agent_" + p, "name": setup_cartesia.persona(p)["name"]}
                for p in setup_cartesia.PURPOSES], "has_more": False})
        if "starting_after" not in request.url.params:
            return httpx.Response(200, json={"data": [{"id": "pn_unrelated", "label": "other"}], "has_more": True})
        assert request.url.params["starting_after"] == "pn_unrelated"
        return httpx.Response(200, json={"data": [{"id": "pn_existing", "label": setup_cartesia.NUMBER_LABEL}],
                                        "has_more": False})

    provider(monkeypatch, handler)
    result = setup_cartesia.setup(apply=True, provision_number=True)
    assert result["number_configured"]
    assert read_env(private_root / ".env")["CARTESIA_FROM_NUMBER_ID"] == "pn_existing"


@pytest.mark.parametrize("page", [
    {"data": [], "has_more": True},
    {"data": [{"id": "agent_one"}], "has_more": True, "next_page": "https://evil.example/token"},
    {"data": [{"id": "agent_one"}], "has_more": True, "next_page": ""},
    {"data": None, "has_more": False},
    {"data": [], "has_more": "false"},
])
def test_setup_refuses_malformed_listing_before_mutations(private_root, monkeypatch, page):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=page)

    provider(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="Cartesia"):
        setup_cartesia.setup(apply=True, provision_number=True)


@pytest.mark.parametrize("repeating", [True, False])
def test_setup_bounds_pagination_and_rejects_repeated_cursors(private_root, monkeypatch, repeating):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        identifier = "agent_same" if repeating else f"agent_{len(requests)}"
        return httpx.Response(200, json={"data": [{"id": identifier}], "has_more": True})

    provider(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="pagination"):
        setup_cartesia.setup(apply=True, provision_number=True)
    assert len(requests) == (2 if repeating else 10)


def test_setup_does_not_provision_number_without_explicit_flag(private_root, monkeypatch):
    def handler(request):
        assert request.method == "GET"
        rows = [{"id": "agent_" + p, "name": setup_cartesia.persona(p)["name"]}
                for p in setup_cartesia.PURPOSES] if request.url.path == "/v1/agents" else []
        return httpx.Response(200, json={"data": rows, "has_more": False})

    provider(monkeypatch, handler)
    result = setup_cartesia.setup(apply=True, provision_number=False)
    assert not result["number_configured"]
    assert "CARTESIA_FROM_NUMBER_ID" not in read_env(private_root / ".env")


def test_env_update_preserves_secrets_and_failed_replacement_preserves_file(private_root, monkeypatch):
    path = private_root / ".env"
    setup_cartesia.update_env(path, {"SOURCE_DATA_PATH": "/a path/$(not-a-command)/source.json"})
    assert read_env(path)["SOURCE_DATA_PATH"] == "/a path/$(not-a-command)/source.json"
    assert read_env(path)["OPENAI_API_KEY"] == "secret key # stays"
    before = path.read_bytes()

    def fail_replace(*_):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(setup_cartesia.os, "replace", fail_replace)
    with pytest.raises(OSError):
        setup_cartesia.update_env(path, {"SOURCE_DATA_PATH": "/new.json"})
    assert path.read_bytes() == before
    assert list(private_root.glob("..env.*")) == []
    with pytest.raises(ValueError, match="Invalid local"):
        setup_cartesia.update_env(path, {"SOURCE_DATA_PATH": "/source\nOTHER=bad"})
    assert path.read_bytes() == before


def test_prepare_keeps_team_phone_private_and_synthetic_sources_labeled(private_root, monkeypatch):
    from scripts import spacetime_admin
    calls = []
    closed = []
    monkeypatch.setattr(spacetime_admin, "LocalAdmin", lambda *_: SimpleNamespace(
        call=lambda actor, reducer, payload: calls.append((actor, reducer, payload)),
        client=SimpleNamespace(close=lambda: closed.append(True))))
    monkeypatch.setenv("SPACETIME_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("SPACETIME_DB", "test")
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", "+15005550006")
    fixture = private_root / "sim/fixtures/live_concurrency_source.json"
    before = fixture.read_bytes()
    destination = live_sponsors.prepare()
    assert fixture.read_bytes() == before and b"+15005550006" not in before
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    data = json.loads(destination.read_text())
    assert data["companies"]["DEMO-TEAM"]["phone"] == "+15005550006"
    for case_id in (live_sponsors.LOOKUP_CASE, live_sponsors.VOICE_CASE):
        case = data["cases"][case_id]
        assert case["synthetic"] is True and "inbound_call" not in case
        assert all(doc["load_id"] == case["load_id"] for doc in case["documents"])
    assert data["companies"]["PUBLIC-JBHUNT"]["verified"] is False
    assert closed == [True]
    assert {reducer for _, reducer, _ in calls} == {"open_case", "upsert_company", "upsert_invoice", "set_demo_target"}
    voice_source = data["cases"][live_sponsors.VOICE_CASE]
    assert voice_source["load_reference"] == "GG-1042"
    assert voice_source["invoice_amount"] == 1225 and voice_source["paid_amount"] == 0
    invoice = next(payload for _, reducer, payload in calls if reducer == "upsert_invoice")
    assert invoice["amount"] == 1225 and invoice["contract_amount"] == 975
    assert invoice["id"] == "SYN-CARTESIA-TEAM-CALL-INVOICE" and "paid_amount" not in invoice
    assert invoice["write_id"] == "sponsor-invoice:gg1042:v1:SYN-CARTESIA-TEAM-CALL-INVOICE"
    for path in (private_root / ".env", private_root / "data/live/runtime.env"):
        assert read_env(path)["SOURCE_DATA_PATH"] == str(destination)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_prepare_retains_prior_source_artifacts_and_reuses_stable_invoice_write(private_root, monkeypatch):
    from scripts import spacetime_admin
    calls = []
    monkeypatch.setattr(spacetime_admin, "LocalAdmin", lambda *_: SimpleNamespace(
        call=lambda actor, reducer, payload: calls.append((actor, reducer, payload)),
        client=SimpleNamespace(close=lambda: None)))
    monkeypatch.setenv("SPACETIME_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("SPACETIME_DB", "test")
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", "+15005550006")
    public_source = private_root / "sim/fixtures/team_call_source.json"
    public_before = public_source.read_bytes()
    original = live_sponsors.prepare()
    original_bytes, modified = original.read_bytes(), original.stat().st_mtime_ns
    assert live_sponsors.prepare() == original and original.stat().st_mtime_ns == modified
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", "+15005550007")
    replacement = live_sponsors.prepare()
    assert replacement != original and original.read_bytes() == original_bytes
    assert public_source.read_bytes() == public_before
    assert b"+15005550006" not in public_before and b"+15005550007" not in public_before
    invoices = [payload for _, reducer, payload in calls if reducer == "upsert_invoice"]
    assert len(invoices) == 3 and invoices[0] == invoices[1] == invoices[2]
    assert "paid_amount" not in invoices[0]
    cases = [payload for _, reducer, payload in calls if reducer == "open_case"
             and payload["case_id"] == live_sponsors.VOICE_CASE]
    assert cases[0]["write_id"] == "sponsor-case:v1:SYN-CARTESIA-TEAM-CALL"
    assert cases[0]["load_id"] == "SYN-CARTESIA-TEAM-CALL-LOAD"
    assert cases[0]["title"] == "[Live synthetic] Cartesia team phone test"
    assert cases[0] == cases[1] == cases[2]


@pytest.fixture
def preflight_source(private_root, monkeypatch):
    path = private_root / "data/live/preflight.json"
    monkeypatch.setenv("FREIGHTOPS_MODE", "live")
    monkeypatch.setenv("SOURCE_DATA_PATH", str(path))
    monkeypatch.setenv("DEMO_ALLOWLIST_PHONES", "+15005550006")
    monkeypatch.setattr(voice, "voice_configuration_status", lambda _: {"available": True})
    monkeypatch.setattr(voice, "check_call_window", lambda _: None)
    data = {"cases": {live_sponsors.VOICE_CASE: {"carrier_id": "DEMO-TEAM"}}, "companies": {
        "DEMO-TEAM": {"id": "DEMO-TEAM", "phone": "+15005550006", "verified": True,
                      "source": "operator", "source_url": "operator://team-demo", "timezone": "America/Los_Angeles"}}}
    path.write_text(json.dumps(data))
    return path, data


def test_voice_preflight_checks_directory_and_redacts_destination(preflight_source):
    status = live_sponsors.voice_preflight()
    assert status["available"] and status["directory_ready"] and status["calling_window_open"]
    assert status["destination_suffix"] == "0006"
    assert "+15005550006" not in json.dumps(status)


@pytest.mark.parametrize("problem", ["missing_case", "wrong_case", "unverified", "missing_phone", "wrong_destination"])
def test_voice_preflight_rejects_unbound_or_unverified_destination(preflight_source, problem):
    path, data = preflight_source
    company = data["companies"]["DEMO-TEAM"]
    if problem == "missing_case":
        data["cases"].clear()
    elif problem == "wrong_case":
        data["cases"][live_sponsors.VOICE_CASE]["carrier_id"] = "UNRELATED"
    elif problem == "unverified":
        company["verified"] = False
    elif problem == "missing_phone":
        company.pop("phone")
    else:
        company["phone"] = "+15005550007"
    path.write_text(json.dumps(data))
    status = live_sponsors.voice_preflight()
    assert not status["available"] and not status["directory_ready"]
    assert not status["calling_window_open"]
    assert "+15005550007" not in json.dumps(status)


def test_call_command_does_not_submit_task_outside_calling_window(preflight_source, monkeypatch):
    def outside(_):
        raise voice.VoiceError("outside calling hours")

    def never_submit(*_):
        raise AssertionError("No task may be admitted outside the calling window")

    monkeypatch.setattr(voice, "check_call_window", outside)
    monkeypatch.setattr(live_sponsors, "run_task", never_submit)
    monkeypatch.setattr(sys, "argv", ["live_sponsors", "call"])
    with pytest.raises(SystemExit, match="calling hours"):
        live_sponsors.main()
