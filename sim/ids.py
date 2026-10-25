"""Identifiers for simulated records (event, mail, bank transaction IDs).

Outside a lifecycle step they are random. While sim.generator replays a step of a resumable load it runs the step
under ``replayable(f"{load_id}:{step}")``, so the step mints the same IDs every time it runs: a step interrupted
mid-publish and run again after a restart re-sends the same events and re-posts the same bank transaction, which
ingest and the bank feed then treat as duplicates instead of new money or new documents.
"""
import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_SEED: ContextVar[list | None] = ContextVar("sim_id_seed", default=None)


@contextmanager
def replayable(seed: str) -> Iterator[None]:
    token = _SEED.set([seed, 0])
    try:
        yield
    finally:
        _SEED.reset(token)


def new_hex(length: int) -> str:
    """`length` hex digits: random, or the next deterministic draw of the current replayable step."""
    state = _SEED.get()
    if state is None:
        return uuid.uuid4().hex[:length]
    state[1] += 1
    return hashlib.sha256(f"{state[0]}#{state[1]}".encode()).hexdigest()[:length]
