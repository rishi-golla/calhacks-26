# W1 harness (ASUS intake)

Under PRD v1.3 ASUS is intake and first-hop routing only. Mesh agents call their own model providers; nothing
here is on their inference path.

## Pieces
| Path | What |
|---|---|
| `infra/serve_*.sh` | vLLM containers, all on 127.0.0.1: A `nano` :8001, B `qwen4b` :8002, C `gptoss` :8003. `sg docker -c "bash infra/serve_all.sh"` starts them in order, gated on `/health`. |
| `config.toml` | Logical names -> backend fallback chains (`small-fast`->B, `reasoning`->A, `coder-tools`->C) and the deterministic `auto` routing table. |
| `server.py` | :8000 (127.0.0.1 unless `HARNESS_HOST`). `/v1/chat/completions` keeps the v1.0 7.1 shape (harness block, 503 `{"error":"unavailable"}`), `/v1/models` reports live health, `POST /v1/ingest[?dispatch=false]` runs one event through Stage 1. **W1-internal.** |
| `ingest/` | Stage 1: regex redaction -> 7.3 table -> A extracts + flags names/addresses -> B labels only unlabeled items -> C cross-checks PII and amounts (`subject.needs_review`) -> `TaskEnvelope` -> A2A with `idempotency_key = event_id`. |
| `data/` (gitignored) | `vault.sqlite` (token -> real value, mode 600, never leaves the box) and `ingest.sqlite` (event -> task mapping for dedup). |

## Run
```bash
make harness          # :8000
make router           # Redis `events`, group `router` -> A2A (or HARNESS_CONSUME=1 make harness; never both)
.venv/bin/python -m pytest harness/tests            # redaction units + hero e2e (skips if models are down)
.venv/bin/python -m harness.tests.bench_latency     # p50 per model
```

## Behaviour worth knowing
- ACK only after a terminal outcome (accepted / duplicate / not_routed / triage). Failed dispatches stay pending
  and are reclaimed after 30 s (`ROUTER_RECLAIM_IDLE_MS`).
- Unknown or low-confidence (<0.6) classifications and bad events go to Redis stream `triage`.
- Envelope schema is unchanged: review flags ride in `subject` (`needs_review`, `review_notes`, `doc_type`).
- Tokens look like `[[ACCOUNT_1a2b3c4d]]` and are deterministic per vault, so agents can still correlate values.
