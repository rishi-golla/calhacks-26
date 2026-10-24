"""Boot an agent as its own A2A JSON-RPC service with a persistent task store."""
import logging
import os
from pathlib import Path

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import DatabaseTaskStore
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette

from common.a2a_util import build_card
from common.config import AGENTS
from common.runtime import FreightAgent


def task_store(agent: str) -> DatabaseTaskStore:
    """TASK_DB_URL=postgresql+asyncpg://... in the demo; per-agent SQLite file otherwise."""
    url = os.getenv("TASK_DB_URL")
    if not url:
        d = Path(os.getenv("TASK_DB_DIR", "data"))
        d.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{d}/tasks_{agent}.db"
    return DatabaseTaskStore(create_async_engine(url), table_name=f"a2a_tasks_{agent}")


def build_app(executor: FreightAgent, descriptions: dict[str, str]) -> Starlette:
    spec = AGENTS[executor.name]
    card = build_card(spec, descriptions)
    handler = DefaultRequestHandler(agent_executor=executor, task_store=task_store(executor.name), agent_card=card)
    return Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")])


def serve(executor: FreightAgent, descriptions: dict[str, str]) -> None:
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s {executor.name} %(levelname)s %(message)s")
    spec = AGENTS[executor.name]
    for noisy in ("httpx", "a2a"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    uvicorn.run(build_app(executor, descriptions), host="0.0.0.0", port=spec.port, log_level="warning")
