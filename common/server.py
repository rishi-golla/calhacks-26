"""Boot an agent as its own A2A JSON-RPC service."""
import logging

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette

from common.a2a_util import build_card
from common.config import AGENTS
from common.runtime import FreightAgent


def build_app(executor: FreightAgent, descriptions: dict[str, str]) -> Starlette:
    spec = AGENTS[executor.name]
    card = build_card(spec, descriptions)
    # TODO(W3): swap InMemoryTaskStore for DatabaseTaskStore (Postgres) for crash recovery.
    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card)
    return Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")])


def serve(executor: FreightAgent, descriptions: dict[str, str]) -> None:
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s {executor.name} %(levelname)s %(message)s")
    spec = AGENTS[executor.name]
    uvicorn.run(build_app(executor, descriptions), host="0.0.0.0", port=spec.port, log_level="warning")
