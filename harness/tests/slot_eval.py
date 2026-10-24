"""Head-to-head check of a model in the classifier slot, with free ground truth: typed events from the benchmark
batch (bank-change emails, carrier invoices, shipper payments) have a known owner via the 7.3 table, so their
redacted text tests whether a model labels documents correctly.

    python -m harness.tests.slot_eval qwen4b olmoe        # served model names (pinned through the harness)
"""
import asyncio
import json
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from harness.chat import complete
from harness.ingest.pipeline import document_text
from harness.ingest.redact import known_spans, redact_obj, replace_spans
from harness.ingest.routing import CLASSIFY_SCHEMA, CLASSIFY_SYSTEM, ROUTES
from harness.ingest.vault import Vault
from harness.pool import Pool
from harness.tests.batch_bench import BATCH

PER_TYPE = 20


def samples() -> list[tuple[str, str]]:
    vault = Vault(Path(tempfile.mkdtemp()) / "v.sqlite")
    picked: Counter = Counter()
    out = []
    for line in BATCH.read_text().splitlines():
        ev = json.loads(line)
        t, payload = ev["type"], ev.get("payload") or {}
        if t not in ROUTES or not isinstance(payload.get("text"), str) or picked[t] >= PER_TYPE:
            continue
        red, toks = redact_obj(payload, vault)
        red = replace_spans(red, known_spans(toks, vault))
        out.append((document_text({k: red.get(k) for k in ("from", "to", "subject", "date", "text")}), ROUTES[t]))
        picked[t] += 1
    return out


async def run(model: str, items: list[tuple[str, str]], pool: Pool) -> dict:
    sem = asyncio.Semaphore(8)

    async def one(text: str, want: str) -> tuple[bool, int, str]:
        body = {"model": model, "max_tokens": 120, "temperature": 0,
                "messages": [{"role": "system", "content": CLASSIFY_SYSTEM},
                             {"role": "user", "content": f"<item>\n{text[:6000]}\n</item>"}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "route", "schema": CLASSIFY_SCHEMA}},
                "metadata": {"agent": "router", "task_type": "classify_event"}}
        async with sem:
            t = time.perf_counter()
            try:
                resp = await complete(pool, body)
                got = json.loads(resp["choices"][0]["message"]["content"]).get("skill", "?")
            except Exception as e:  # noqa: BLE001 - count failures as wrong
                got = f"error:{type(e).__name__}"
            return got == want, int((time.perf_counter() - t) * 1000), got

    res = await asyncio.gather(*(one(t, w) for t, w in items))
    wrong = Counter(f"{w}->{g}" for (_t, w), (ok, _ms, g) in zip(items, res, strict=True) if not ok)
    return {"model": model, "accuracy": f"{sum(r[0] for r in res)}/{len(res)}",
            "p50_ms": int(statistics.median(r[1] for r in res)), "confusions": dict(wrong.most_common(5))}


async def main(models: list[str]) -> None:
    items = samples()
    print(f"{len(items)} labelled items: {dict(Counter(w for _t, w in items))}")
    pool = Pool()
    await pool.check_health()
    for m in models:
        print(json.dumps(await run(m, items, pool)))
    await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["qwen4b", "olmoe"]))
