"""Authenticated A2A 1.0 submission and same-task continuation helpers."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import new_data_message
from a2a.types import AgentCard, GetTaskRequest, Role, SendMessageConfiguration, SendMessageRequest, Task
from google.protobuf.json_format import ParseDict

from common.config import AGENTS
from common.envelope import DelegationMeta, TaskEnvelope


@asynccontextmanager
async def agent_client(agent: str, sender: str, *, skill: str | None = None, timeout: float = 15):
    spec = AGENTS[agent]
    token = os.getenv(f"A2A_TOKEN_{sender.upper()}", "")
    if os.getenv("FREIGHTOPS_MODE", "fixture") == "live" and not token:
        raise ValueError(f"A2A credential missing for {sender}")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False) as http:
        response = await http.get(f"{spec.url.rstrip('/')}/.well-known/agent-card.json")
        response.raise_for_status()
        card = ParseDict(response.json(), AgentCard())
        if card.name != agent or (skill and skill not in {s.id for s in card.skills}):
            raise ValueError("destination card does not advertise the required identity/skill")
        permitted = [interface for interface in card.supported_interfaces
                     if interface.protocol_binding == "JSONRPC" and interface.protocol_version == "1.0"
                     and interface.url.rstrip("/") == spec.url.rstrip("/")]
        if not permitted:
            raise ValueError("destination card endpoint/version does not match configured service")
        del card.supported_interfaces[:]
        card.supported_interfaces.extend(permitted)
        client = await create_client(card, ClientConfig(streaming=False, polling=True, httpx_client=http))
        try:
            yield client
        finally:
            await client.close()


async def send_task(agent: str, envelope: TaskEnvelope, meta: DelegationMeta) -> Task:
    message = new_data_message(envelope.model_dump(), context_id=envelope.case_id, role=Role.ROLE_USER)
    message.metadata.update(meta.model_dump(exclude_none=True))
    async with agent_client(agent, meta.from_agent, skill=envelope.task_type) as client:
        async for event in client.send_message(SendMessageRequest(
            message=message, configuration=SendMessageConfiguration(return_immediately=True),
        )):
            if event.HasField("task"):
                return event.task
    raise RuntimeError("A2A destination returned no durable task acceptance")


async def get_task(agent: str, task_id: str, *, sender: str) -> Task:
    async with agent_client(agent, sender) as client:
        return await client.get_task(GetTaskRequest(id=task_id))


async def resume_task(agent: str, task_id: str, case_id: str, wait_key: str, data: dict,
                      *, sender: str = "gateway", idempotency_key: str) -> Task:
    message = new_data_message({"wait_key": wait_key, "data": data}, context_id=case_id, role=Role.ROLE_USER)
    message.task_id = task_id
    message.metadata.update({"from_agent": sender, "idempotency_key": idempotency_key, "case_id": case_id})
    async with agent_client(agent, sender) as client:
        async for event in client.send_message(SendMessageRequest(
            message=message, configuration=SendMessageConfiguration(return_immediately=True),
        )):
            if event.HasField("task"):
                return event.task
    raise RuntimeError("A2A destination returned no resume acceptance")
