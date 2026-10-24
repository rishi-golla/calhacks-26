from common.a2a_util import build_card
from common.config import AGENTS


def test_all_agents_have_cards_and_unique_ports():
    ports = [a.port for a in AGENTS.values()]
    assert len(ports) == len(set(ports)) == 8
    for spec in AGENTS.values():
        card = build_card(spec, {})
        assert card.name == spec.name and len(card.skills) == len(spec.skills)
