import ast
import re
from pathlib import Path

import pytest

from common.a2a_util import build_card
from common.config import AGENTS, AgentSpec
from scripts.spacetime_admin import SERVICES


def test_all_agents_have_cards_and_unique_ports():
    ports = [a.port for a in AGENTS.values()]
    assert len(ports) == len(set(ports)) == 9
    for spec in AGENTS.values():
        card = build_card(spec, {})
        assert card.name == spec.name and len(card.skills) == len(spec.skills)


def test_independent_agent_endpoint_configuration(monkeypatch):
    monkeypatch.setenv("AGENT_PORT_DOCUMENTS", "19109")
    spec = AgentSpec("documents", "Documents", 9109, ("collect_documents",))
    assert spec.port == 19109
    monkeypatch.setenv("AGENT_URL_DOCUMENTS", "https://documents.example.test/a2a/")
    assert spec.url == "https://documents.example.test/a2a"


@pytest.mark.parametrize("url", ["ftp://example.test", "http://user:secret@example.test", "https://a.test?q=1"])
def test_invalid_advertised_agent_urls_fail_before_discovery(monkeypatch, url):
    monkeypatch.setenv("AGENT_URL_DOCUMENTS", url)
    with pytest.raises(ValueError, match="advertised URL"):
        _ = AGENTS["documents"].url


def test_python_and_database_registries_have_identical_skills_and_permissions():
    source = (Path(__file__).parents[1] / "spacetime/module/spacetimedb/src/policy.ts").read_text()
    agents = ast.literal_eval(re.search(r"export const AGENTS = (\[[^\]]+\])", source)[1])
    assert set(agents) == set(AGENTS)
    assert set(AGENTS).issubset(SERVICES)
    for registry, expected in (
        ("SKILLS", {name: list(spec.skills) for name, spec in AGENTS.items()}),
        ("DELEGATIONS", {name: list(spec.may_delegate_to) for name, spec in AGENTS.items()}),
    ):
        body = re.search(rf"export const {registry}:.*?= \{{(.*?)\}};", source, re.DOTALL)[1]
        actual = {name: ast.literal_eval(values) for name, values in
                  re.findall(r"(\w+):\s*(\[[^\]]*\])", body)}
        assert actual == expected
