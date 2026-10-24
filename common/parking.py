"""Resume work parked on an external key: a call id, an email id or a human approval id."""
import logging

from common import a2a_client, store

log = logging.getLogger(__name__)


async def resume_parked(key: str, payload: dict, kind: str | None = None) -> dict | None:
    """Claims the key once (duplicate webhooks are ignored) and wakes the parked A2A task.
    `kind` restricts which parked entry types a caller may resume. If delivery fails the claim is released."""
    if kind and not any(p["key"] == key and p["kind"] == kind for p in store.list_parked(kind)):
        return None
    p = store.take_parked(key)
    if p is None:
        return None
    try:
        ok = await a2a_client.resume_task(p["agent"], p["task_id"], p["context_id"], {"key": key, **payload})
    except Exception:
        log.exception("resume of %s failed", key)
        ok = False
    store.log_event(p["case_id"], "gateway", p["task_id"], "resumed_parked" if ok else "resume_failed",
                    key=key, parked_kind=p["kind"])
    if not ok:
        store.unpark(key)
        return None
    return p
