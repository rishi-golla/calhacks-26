"""Hosted persona/tool updates use the documented API without dialing a phone."""
import copy
import json

import httpx
import pytest

from scripts import setup_cartesia as setup
from scripts.local_env import read_env


class Account:
    def __init__(self):
        self.agents = {f"agent_{purpose}": {
            "id": f"agent_{purpose}", "name": setup.persona(purpose)["name"],
            "config": {**setup.persona(purpose)["config"], "instructions": "Old generic prompt",
                       "model": {"id": "preserved-model"}, "audio": {"output": {"voice_id": "preserved-voice"}}},
        } for purpose in setup.PURPOSES}
        self.tools = {}
        self.requests = []
        self.ignore_tool_patch = False

    def respond(self, request):
        self.requests.append(request)
        assert request.url.host == "api.cartesia.ai"
        assert request.headers["Cartesia-Version"] == "2026-08-14"
        path = request.url.path
        assert not path.startswith("/agents/calls")
        assert path != "/agents/phone-numbers/provision"
        if request.method == "GET":
            if path == "/v1/agents":
                return httpx.Response(200, json={"data": list(self.agents.values()), "has_more": False})
            if path == "/agents/phone-numbers":
                return httpx.Response(200, json={"data": [{"id": "pn_existing", "label": "renamed number"}], "has_more": False})
            if path == "/v1/agents/tools":
                return httpx.Response(200, json={"data": list(self.tools.values()), "has_more": False})
            if path.startswith("/v1/agents/tools/"):
                item = copy.deepcopy(self.tools[path.rsplit("/", 1)[1]])
                # GET normalizes optional empty fields; this must not create a new
                # tool update or agent version every time the setup is rerun.
                item["api_schema"].setdefault("authentication", None)
                item["api_schema"].setdefault("query_params_schema", None)
                return httpx.Response(200, json=item)
            return httpx.Response(200, json=self.agents[path.rsplit("/", 1)[1]])
        body = json.loads(request.content)
        if path == "/v1/agents/tools" and request.method == "POST":
            identifier = f"tool_{len(self.tools)}"
            self.tools[identifier] = {"id": identifier, **body}
            return httpx.Response(200, json=self.tools[identifier])
        if path.startswith("/v1/agents/tools/") and request.method == "PATCH":
            assert "type" not in body  # Cartesia's sparse tool update rejects type.
            identifier = path.rsplit("/", 1)[1]
            item = self.tools[identifier]
            if not self.ignore_tool_patch:
                for key, value in body.items():
                    if key == "api_schema":
                        for field, updated in value.items():
                            if field == "request_headers":
                                for header, content in updated.items():
                                    if content is None:
                                        item[key][field].pop(header, None)
                                    else:
                                        item[key].setdefault(field, {})[header] = content
                            elif updated is None:
                                item[key].pop(field, None)
                            else:
                                item[key][field] = updated
                    else:
                        item[key] = value
            return httpx.Response(200, json=item)
        assert request.method == "PATCH" and path.startswith("/v1/agents/")
        item = self.agents[path.rsplit("/", 1)[1]]
        item["config"].update(body["config"])
        return httpx.Response(200, json=item)

    @property
    def mutations(self):
        return [request for request in self.requests if request.method != "GET"]


@pytest.fixture
def account(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("EXISTING_SECRET=keep-me\n")
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    monkeypatch.setattr(setup, "load_live_env", lambda: None)
    monkeypatch.setenv("CARTESIA_API_KEY", "test-only-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://voice.demo-brokerage.net")
    monkeypatch.setenv("CARTESIA_FROM_NUMBER_ID", "pn_existing")
    for purpose in setup.PURPOSES:
        monkeypatch.setenv(f"CARTESIA_AGENT_ID_{purpose.upper()}", f"agent_{purpose}")
    for name in setup.LOOKUP_TOOLS:
        monkeypatch.delenv(f"CARTESIA_TOOL_ID_{name.upper()}", raising=False)
    result = Account()
    original = httpx.Client
    monkeypatch.setattr(setup.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(result.respond), **kwargs))
    return result


def configure(*, apply):
    return setup.setup(apply=apply, provision_number=False, update_existing=True, enable_tools=True)


def test_tools_bind_only_per_call_capabilities_and_case_without_model_arguments():
    for name in setup.LOOKUP_TOOLS:
        tool = setup.tool_definition(name, "https://voice.demo-brokerage.net")
        assert tool["name"] == f"freightops_{name}"
        assert tool["api_schema"]["url"].endswith(f"/voice/tools/{name}")
        assert tool["api_schema"]["method"] == "POST"
        assert tool["api_schema"]["request_headers"] == {
            "x-call-token": {"type": "dynamic_variable", "name": "call_token"}}
        assert tool["api_schema"]["request_body_schema"] == {
            "type": "object", "properties": {
                "attempt_id": {"type": "string", "dynamic_variable": "attempt_id"},
                "case_id": {"type": "string", "dynamic_variable": "case_id"}}, "required": []}
        assert tool["assignments"] == []


def test_preview_with_update_flags_never_changes_account_or_private_file(account):
    before = (setup.ROOT / ".env").read_bytes()
    result = configure(apply=False)
    assert not account.mutations
    assert (setup.ROOT / ".env").read_bytes() == before
    assert all(item["needs_update"] for item in result["tools"].values())
    assert all(item["needs_update"] for item in result["agents"].values())


def test_apply_updates_existing_personas_and_is_idempotent_without_replacing_ids(account):
    first = configure(apply=True)
    assert len(account.tools) == 5 and len(account.agents) == 3
    assert len(account.mutations) == 8  # Five tool creates and three persona patches.
    assert first["personas_have_gateway_tools"]
    assert all(item["updated"] and not item["created"] for item in first["agents"].values())
    for agent in account.agents.values():
        config = agent["config"]
        assert len(config["tools"]) == 5
        assert config["model"]["id"] == "preserved-model"
        assert config["audio"]["output"]["voice_id"] == "preserved-voice"
        assert "{{load_reference}}" in config["initial_message"]
        assert "freightops_get_case_summary once" in config["instructions"]
        assert "Never guess an email address" in config["instructions"]
        assert "POD already on file" in config["instructions"]
    saved = read_env(setup.ROOT / ".env")
    assert saved["CARTESIA_FROM_NUMBER_ID"] == "pn_existing"
    assert saved["EXISTING_SECRET"] == "keep-me"
    assert saved["CARTESIA_WEBHOOK_TOOLS_ENABLED"] == "1"
    for purpose in setup.PURPOSES:
        assert saved[f"CARTESIA_AGENT_ID_{purpose.upper()}"] == f"agent_{purpose}"
    account.requests.clear()
    second = configure(apply=True)
    assert not account.mutations
    assert all(not item["updated"] and not item["created"] for item in second["agents"].values())


def test_tool_update_clears_old_auth_model_parameters_and_response_assignments(account):
    tool = setup.tool_definition("get_load_facts", "https://old.demo-brokerage.net")
    tool["id"] = "tool_existing"
    tool["api_schema"].update(authentication={"type": "bearer", "token": {"secret_id": "old"}},
        query_params_schema={"properties": {"phone": {"type": "string"}}, "required": ["phone"]},
        path_params_schema={"case": {"type": "string"}})
    tool["api_schema"]["request_headers"]["x-old-credential"] = {"secret_id": "old"}
    tool["assignments"] = [{"dynamic_variable": "case_id", "value_path": "caller_chosen_id"}]
    account.tools[tool["id"]] = tool
    configure(apply=True)
    updated = account.tools["tool_existing"]
    assert setup.tool_matches(updated, setup.tool_definition("get_load_facts", "https://voice.demo-brokerage.net"))
    assert "authentication" not in updated["api_schema"]
    assert "x-old-credential" not in updated["api_schema"]["request_headers"]
    assert updated["assignments"] == []


@pytest.mark.parametrize("drift", [None, "tool_url", "persona_attachment"])
def test_read_only_preview_reports_observed_tool_readiness(account, drift):
    configure(apply=True)
    if drift == "tool_url":
        next(iter(account.tools.values()))["api_schema"]["url"] = "https://old.demo-brokerage.net/voice/tools/get_load_facts"
    elif drift == "persona_attachment":
        account.agents["agent_verification"]["config"]["tools"].pop()
    before = (setup.ROOT / ".env").read_bytes()
    report_before = (setup.ROOT / "data/live/cartesia-setup.json").read_bytes()
    account.requests.clear()
    result = configure(apply=False)
    assert result["personas_have_gateway_tools"] is (drift is None)
    assert not account.mutations
    assert (setup.ROOT / ".env").read_bytes() == before
    assert (setup.ROOT / "data/live/cartesia-setup.json").read_bytes() == report_before


def test_apply_verifies_published_tool_before_attaching_it(account):
    tool = setup.tool_definition("get_load_facts", "https://old.demo-brokerage.net")
    account.tools["tool_existing"] = {"id": "tool_existing", **tool}
    account.ignore_tool_patch = True
    with pytest.raises(RuntimeError, match="Published Cartesia tool"):
        configure(apply=True)
    assert not any(request.method == "PATCH" and "/tools/" not in request.url.path for request in account.mutations)
    assert read_env(setup.ROOT / ".env")["EXISTING_SECRET"] == "keep-me"


def test_prompt_only_update_preserves_preexisting_tool_references(account):
    for agent in account.agents.values():
        agent["config"]["tools"] = [{"id": "existing-custom-tool"}]
    setup.setup(apply=True, provision_number=False, update_existing=True)
    assert all(agent["config"]["tools"] == [{"id": "existing-custom-tool"}] for agent in account.agents.values())
    assert not account.tools


@pytest.mark.parametrize("url", ["", "http://voice.demo-brokerage.net", "https://localhost",
    "https://127.0.0.1", "https://10.0.0.1", "https://[::1]", "https://your-tunnel.example.com",
    "https://user:secret@voice.demo-brokerage.net", "https://voice.demo-brokerage.net?token=secret",
    "https://voice.demo-brokerage.net/{case_id}"])
def test_tool_setup_rejects_local_or_credential_bearing_url_before_provider_requests(account, monkeypatch, url):
    monkeypatch.setenv("PUBLIC_BASE_URL", url)
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        configure(apply=True)
    assert not account.requests


def test_enabling_tools_requires_explicit_update_flag(account):
    with pytest.raises(ValueError, match="update-existing"):
        setup.setup(apply=True, provision_number=False, enable_tools=True)
    assert not account.requests
