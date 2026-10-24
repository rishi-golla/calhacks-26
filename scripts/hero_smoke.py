"""Send the hero invoice to Carrier AP over real A2A; AP delegates to Risk. Requires `make agents`."""
import asyncio
import json
import pathlib

from a2a.client import ClientConfig, create_client
from a2a.helpers import new_data_message
from a2a.types import Role, SendMessageRequest

from common.config import AGENTS
from common.envelope import TaskEnvelope
from common.runtime import extract_artifact_data

HERO = json.loads((pathlib.Path(__file__).parent.parent / "sim/fixtures/hero_case.json").read_text())


async def run() -> list[dict]:
    env = TaskEnvelope(task_type="process_carrier_invoice", case_id=HERO["case_id"], subject=HERO, priority="P0")
    msg = new_data_message(env.model_dump(), role=Role.ROLE_USER)
    msg.metadata.update({"case_id": HERO["case_id"], "from_agent": "router"})
    client = await create_client(AGENTS["carrier_ap"].url, ClientConfig(streaming=True))
    out: list[dict] = []
    try:
        async for ev in client.send_message(SendMessageRequest(message=msg)):
            out.extend(extract_artifact_data(ev))
    finally:
        await client.close()
    return out


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), indent=2))
