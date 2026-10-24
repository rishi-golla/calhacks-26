"""Fire the hero invoice at the running mesh (`make agents`) and print the case trail."""
import asyncio
import json
import pathlib
import uuid

HERO = json.loads((pathlib.Path(__file__).parent.parent / "sim/fixtures/hero_case.json").read_text())


async def run() -> dict:
    from common import a2a_client, store
    from router.main import dispatch
    run_id = uuid.uuid4().hex[:4]
    case_id = f"{HERO['case_id']}-{run_id}"
    task_id = await dispatch({"event_id": f"EVT-{run_id}", "type": "carrier_invoice.received", "priority": "P0",
                              "payload": {**HERO, "case_id": case_id, "invoice_id": f"INV-{run_id}"}})
    for _ in range(240):
        t = await a2a_client.get_task("carrier_ap", task_id)
        if a2a_client.state_name(t) in ("completed", "failed"):
            break
        await asyncio.sleep(0.25)
    return {"result": a2a_client.artifacts(t)[-1:], "events": [(e["agent"], e["kind"]) for e in store.events(case_id)]}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), indent=2, default=str))
