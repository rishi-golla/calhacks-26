"""Run an explicitly simulated brokerage source case over the real A2A mesh."""
import asyncio
import json
import os
from pathlib import Path

from a2a.client import ClientConfig, create_client
from a2a.helpers import new_data_message
from a2a.types import Role, SendMessageRequest

from common.config import AGENTS
from common.envelope import DelegationMeta, TaskEnvelope
from common.runtime import extract_artifact_data

SOURCE = Path(__file__).resolve().parent.parent / "sim/fixtures/brokerage_source.json"


async def run(case_id: str = "CASE-4821", *, idempotency_key: str | None = None) -> list[dict]:
    if os.getenv("FREIGHTOPS_MODE", "fixture") != "fixture":
        raise ValueError("hero_smoke is a fixture-only command; use authenticated intake for live work")
    dataset = json.loads(SOURCE.read_text())
    source = dataset["cases"][case_id]
    env = TaskEnvelope(task_type="process_carrier_invoice", case_id=case_id, subject=source, priority="P0")
    msg = new_data_message(env.model_dump(), context_id=case_id, role=Role.ROLE_USER)
    meta = DelegationMeta(case_id=case_id, from_agent="router", priority="P0",
                          correlation_id=f"smoke:{case_id}", idempotency_key=idempotency_key or f"smoke:{case_id}")
    msg.metadata.update(meta.model_dump(exclude_none=True))
    client = await create_client(AGENTS["carrier_ap"].url, ClientConfig(streaming=True))
    out: list[dict] = []
    try:
        async for event in client.send_message(SendMessageRequest(message=msg)):
            out.extend(extract_artifact_data(event))
    finally:
        await client.close()
    return out


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), indent=2))
