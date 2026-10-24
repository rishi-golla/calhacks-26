"""Thin helpers over a2a-sdk 1.2.3."""
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from common.config import AgentSpec


def build_card(spec: AgentSpec, descriptions: dict[str, str]) -> AgentCard:
    return AgentCard(
        name=spec.name, description=spec.title, version="0.1.0",
        default_input_modes=["application/json"], default_output_modes=["application/json"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=spec.url, protocol_version="1.0")],
        skills=[AgentSkill(id=s, name=s, description=descriptions.get(s, s), tags=["freight", spec.name])
                for s in spec.skills],
    )
