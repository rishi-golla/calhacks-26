"""Model pool: config loading, backend health, per-backend slots (P0 reserve) and routing (PRD v1.0 H1-H5)."""
import asyncio
import logging
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("harness.pool")
CONFIG_PATH = Path(__file__).with_name("config.toml")


class Unavailable(Exception):
    pass


@dataclass
class Backend:
    key: str
    served_name: str
    url: str
    timeout_s: float
    max_concurrency: int
    extra_body: dict[str, Any] = field(default_factory=dict)
    healthy: bool = False
    last_check: float = 0.0
    inflight: int = 0
    queued: int = 0

    def __post_init__(self) -> None:
        # One slot is held back for P0 (demo/voice path) so it never queues behind bulk work.
        self._normal = asyncio.Semaphore(max(1, self.max_concurrency - 1))
        self._reserved = asyncio.Semaphore(1)

    async def acquire(self, priority: str) -> asyncio.Semaphore:
        if priority == "P0" and not self._reserved.locked():
            sem = self._reserved
        else:
            sem = self._normal
        self.queued += 1
        try:
            await sem.acquire()
        finally:
            self.queued -= 1
        self.inflight += 1
        return sem

    def release(self, sem: asyncio.Semaphore) -> None:
        self.inflight -= 1
        sem.release()


@dataclass
class Plan:
    chains: list[list[Backend]]   # one chain per model to run; >1 chain means ensemble
    reason: str
    ensemble: bool = False


class Pool:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        cfg = tomllib.loads(path.read_text())
        self.policy_version: str = cfg["policy_version"]
        self.default_deadline_s: float = cfg.get("default_deadline_s", 60)
        self.health_interval_s: float = cfg.get("health_interval_s", 5)
        self.backends = {k: Backend(key=k, **v) for k, v in cfg["backends"].items()}
        self.logical: dict[str, list[str]] = cfg["logical"]
        self.routes: list[dict] = cfg["routes"]
        self._client = httpx.AsyncClient()

    # ---- health ----
    async def check_health(self) -> None:
        async def one(b: Backend) -> None:
            try:
                r = await self._client.get(f"{b.url}/health", timeout=2)
                b.healthy = r.status_code == 200
            except httpx.HTTPError:
                b.healthy = False
            b.last_check = time.time()
        await asyncio.gather(*(one(b) for b in self.backends.values()))

    async def health_loop(self) -> None:
        while True:
            await self.check_health()
            await asyncio.sleep(self.health_interval_s)

    def mark_down(self, b: Backend) -> None:
        b.healthy = False  # the health loop restores it once /health answers again

    # ---- routing ----
    def chain(self, logical: str) -> list[Backend]:
        return [self.backends[k] for k in self.logical[logical]]

    def resolve_pinned(self, model: str) -> list[Backend] | None:
        if model in self.logical:
            return self.chain(model)
        for b in self.backends.values():
            if model in (b.key, b.served_name):
                return [b]
        return None

    @staticmethod
    def _matches(match: dict, md: dict, body: dict) -> bool:
        facts = {**md, "has_tools": bool(body.get("tools")),
                 "has_schema": bool(body.get("response_format"))}
        for k, want in match.items():
            have = facts.get(k)
            if isinstance(want, list):
                if have not in want:
                    return False
            elif have != want:
                return False
        return True

    def plan(self, body: dict, md: dict) -> Plan:
        model = body.get("model") or "auto"
        if model != "auto":
            chain = self.resolve_pinned(model)
            if chain is None:
                raise KeyError(model)
            return Plan([chain], f"pinned model={model}")
        for rule in self.routes:
            if self._matches(rule.get("match", {}), md, body):
                if "ensemble" in rule:
                    return Plan([self.chain(m) for m in rule["ensemble"]], rule["reason"], ensemble=True)
                return Plan([self.chain(rule["model"])], rule["reason"])
        return Plan([self.chain("reasoning")], "no rule matched -> reasoning")

    # ---- calls ----
    async def call(self, b: Backend, payload: dict, priority: str, timeout_s: float) -> tuple[dict, int, int]:
        """POST one chat completion. Returns (response, queue_ms, latency_ms)."""
        t0 = time.perf_counter()
        sem = await asyncio.wait_for(b.acquire(priority), timeout=timeout_s)
        t1 = time.perf_counter()
        try:
            body = {**b.extra_body, **payload, "model": b.served_name}
            r = await self._client.post(f"{b.url}/v1/chat/completions", json=body,
                                        timeout=max(0.5, timeout_s - (t1 - t0)))
            r.raise_for_status()
            return r.json(), int((t1 - t0) * 1000), int((time.perf_counter() - t1) * 1000)
        finally:
            b.release(sem)

    async def aclose(self) -> None:
        await self._client.aclose()
