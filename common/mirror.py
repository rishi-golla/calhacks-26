"""Fire-and-forget mirroring of ops-store writes to SpacetimeDB reducers (W2 owns reducer names)."""
import asyncio
import logging

from common.spacetime_client import call_reducer

log = logging.getLogger(__name__)
_pending: set[asyncio.Task] = set()


def emit(reducer: str, *args) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(_safe(reducer, *args))
    _pending.add(t)
    t.add_done_callback(_pending.discard)


async def _safe(reducer: str, *args) -> None:
    try:
        await call_reducer(reducer, *args)
    except Exception as e:  # noqa: BLE001 - mirror must never break an agent
        log.debug("mirror %s failed: %s", reducer, e)
