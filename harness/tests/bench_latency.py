"""p50/p90 latency per model. Usage: python -m harness.tests.bench_latency [-n 20]
Workloads: `classify` (short prompt, ~30 output tokens, json_schema) and `extract` (hero invoice -> schema).
Goes through the running harness on :8000 with the model pinned; `direct` hits vLLM to show harness overhead."""
import argparse
import asyncio
import json
import statistics
import time

import httpx

from harness.ingest.routing import CLASSIFY_SCHEMA, CLASSIFY_SYSTEM
from harness.ingest.schemas import SCHEMAS
from harness.pool import Pool
from harness.tests.test_e2e_hero import HERO_TEXT

HARNESS = "http://127.0.0.1:8000"
EMAIL = "From: carrier. Subject: new bank details. Please update our remittance account before Friday."


def bodies() -> dict[str, dict]:
    return {
        "classify": {"max_tokens": 120, "temperature": 0,
                     "messages": [{"role": "system", "content": CLASSIFY_SYSTEM},
                                  {"role": "user", "content": EMAIL}],
                     "response_format": {"type": "json_schema",
                                         "json_schema": {"name": "r", "schema": CLASSIFY_SCHEMA}}},
        "extract": {"max_tokens": 1200, "temperature": 0,
                    "messages": [{"role": "user", "content": f"Extract as JSON:\n{HERO_TEXT}"}],
                    "response_format": {"type": "json_schema",
                                        "json_schema": {"name": "e", "schema": SCHEMAS["carrier_invoice"]}}},
    }


async def run(c: httpx.AsyncClient, url: str, body: dict, n: int) -> dict:
    lat, toks = [], []
    for _ in range(n):
        t = time.perf_counter()
        r = await c.post(url, json=body, timeout=120)
        r.raise_for_status()
        lat.append((time.perf_counter() - t) * 1000)
        toks.append(r.json()["usage"]["completion_tokens"])
    lat.sort()
    return {"p50_ms": round(statistics.median(lat)), "p90_ms": round(lat[int(0.9 * (n - 1))]),
            "out_tok": round(statistics.median(toks)),
            "tok_s": round(statistics.median(toks) / (statistics.median(lat) / 1000), 1)}


async def main(n: int) -> None:
    pool = Pool()
    rows = []
    async with httpx.AsyncClient() as c:
        for b in pool.backends.values():
            for name, body in bodies().items():
                await c.post(f"{HARNESS}/v1/chat/completions", json={**body, "model": b.served_name}, timeout=120)
                rows.append({"model": b.served_name, "workload": name, "via": "harness",
                             **await run(c, f"{HARNESS}/v1/chat/completions", {**body, "model": b.served_name}, n)})
            direct = {**b.extra_body, **bodies()["classify"], "model": b.served_name}
            rows.append({"model": b.served_name, "workload": "classify", "via": "direct",
                         **await run(c, f"{b.url}/v1/chat/completions", direct, n)})
    await pool.aclose()
    print(f"n={n} per row, sequential, single client\n")
    print("| model | workload | via | p50 ms | p90 ms | out tok | tok/s |\n|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['model']} | {r['workload']} | {r['via']} | {r['p50_ms']} | {r['p90_ms']} | {r['out_tok']} |"
              f" {r['tok_s']} |")
    print("\n" + json.dumps(rows))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=20)
    asyncio.run(main(ap.parse_args().n))
