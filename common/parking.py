"""Resume work parked on an external key: a call id, an email id or a human approval id."""
from common import a2a_client, store


async def resume_parked(key: str, payload: dict) -> dict | None:
    """Claims the key once (duplicate webhooks are ignored) and wakes the parked A2A task."""
    p = store.take_parked(key)
    if p is None:
        return None
    ok = await a2a_client.resume_task(p["agent"], p["task_id"], p["context_id"], {"key": key, **payload})
    store.log_event(p["case_id"], "gateway", p["task_id"], "resumed_parked" if ok else "resume_failed",
                    key=key, parked_kind=p["kind"])
    return p
