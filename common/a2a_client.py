"""Outbound A2A calls shared by agents, router and gateway (a2a-sdk 1.2.3, JSON-RPC, non-streaming)."""
import asyncio
import os

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import new_data_message
from a2a.types import (
    GetTaskRequest,
    Message,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
)
from google.protobuf.json_format import MessageToDict

from common.config import AGENTS

TIMEOUT_S = float(os.getenv("A2A_TIMEOUT_S", "180"))
TERMINAL = {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_CANCELED,
            TaskState.TASK_STATE_REJECTED}
STATE_NAMES = {TaskState.TASK_STATE_COMPLETED: "completed", TaskState.TASK_STATE_FAILED: "failed",
               TaskState.TASK_STATE_CANCELED: "failed", TaskState.TASK_STATE_REJECTED: "failed",
               TaskState.TASK_STATE_INPUT_REQUIRED: "parked", TaskState.TASK_STATE_AUTH_REQUIRED: "parked"}


async def send(agent: str, msg: Message, return_immediately: bool = False) -> Task | None:
    """SendMessage. Blocking mode returns once the task is terminal or parked (INPUT_REQUIRED)."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
        client = await create_client(AGENTS[agent].url, ClientConfig(streaming=False, httpx_client=http))
        task = None
        try:
            req = SendMessageRequest(message=msg,
                                     configuration=SendMessageConfiguration(return_immediately=return_immediately))
            async for ev in client.send_message(req):
                if ev.HasField("task"):
                    task = ev.task
        finally:
            await client.close()
        return task


async def get_task(agent: str, task_id: str) -> Task:
    async with httpx.AsyncClient(timeout=30) as http:
        client = await create_client(AGENTS[agent].url, ClientConfig(streaming=False, httpx_client=http))
        try:
            return await client.get_task(GetTaskRequest(id=task_id))
        finally:
            await client.close()


def state_name(task: Task | None) -> str:
    if task is None:
        return "submitted"
    return STATE_NAMES.get(task.status.state, "working")


def artifacts(task: Task | None) -> list[dict]:
    if task is None:
        return []
    out = []
    for a in MessageToDict(task).get("artifacts", []):
        out.extend(p["data"] for p in a.get("parts", []) if "data" in p)
    return out


async def start_task(agent: str, envelope: dict, metadata: dict, context_id: str | None = None,
                     wait: bool = False) -> Task | None:
    msg = new_data_message(envelope, context_id=context_id, role=Role.ROLE_USER)
    msg.metadata.update(metadata)
    return await send(agent, msg, return_immediately=not wait)


async def wait_until_parked(agent: str, task_id: str, timeout_s: float = 60) -> bool:
    """A resume message must not race the task's own park; poll until INPUT_REQUIRED."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        try:
            t = await get_task(agent, task_id)
            if t.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
                return True
            if t.status.state in TERMINAL:
                return False
        except Exception:  # noqa: BLE001, S110 - agent restarting; keep polling
            pass
        await asyncio.sleep(0.25)
    return False


async def resume_task(agent: str, task_id: str, context_id: str, payload: dict, wait_parked: bool = True) -> bool:
    """Wake a parked task with a follow-up message on the same task id (PRD 7.2 park/resume)."""
    if wait_parked and not await wait_until_parked(agent, task_id):
        return False
    msg = new_data_message(payload, role=Role.ROLE_USER, task_id=task_id, context_id=context_id)
    msg.metadata.update({"kind": "resume"})
    await send(agent, msg, return_immediately=True)
    return True
