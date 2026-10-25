# W1 harness (ASUS intake)

Under PRD v1.3 ASUS is intake and first-hop routing only. Mesh agents call their own model providers; nothing
here is on their inference path.

## Pieces
| Path | What |
|---|---|
| `infra/serve_*.sh` | vLLM containers, all on 127.0.0.1: A `nano` :8001, B `qwen4b` :8002, C `gptoss` :8003 (out of rotation: NaN output on GB10, see the script). `sg docker -c "bash infra/serve_all.sh"` starts them in order, gated on `/health`. |
| `config.toml` | Logical names -> backend fallback chains (`small-fast`->B, `reasoning`->A, `coder-tools`->B while C is out of rotation) and the deterministic `auto` routing table. |
| `server.py` | :8000 (127.0.0.1 unless `HARNESS_HOST`). `/v1/chat/completions` keeps the v1.0 7.1 shape (harness block, 503 `{"error":"unavailable"}`), `/v1/models` reports live health, `POST /v1/ingest[?dispatch=false]` runs one event through Stage 1. **W1-internal.** |
| `ingest/` | Stage 1: regex redaction -> 7.3 table -> A extracts + flags names/addresses -> B labels only unlabeled items -> C cross-checks PII and amounts (`subject.needs_review`) -> `TaskEnvelope` -> first hop (A2A, or the SpacetimeDB outbox) with `idempotency_key = event:<event_id>`. |
| `data/` (gitignored) | `vault.sqlite` (token -> real value, mode 600, never leaves the box) and `ingest.sqlite` (event -> task or queued enqueue payload, for dedup and byte-identical retries). |

## Secrets (never committed)
- `.env` (gitignored, mode 600) on the router host: `SPACETIME_*` for the harness/router identities and
  `A2A_TOKEN_ROUTER`. `make router` does not read it, so start the router with
  `set -a; . ./.env; set +a; make router`.
- A2A tokens are shared secrets, identical on every host. The team set ships in the private team bundle
  (`data/team-release/a2a.env`, see its README); the router needs `A2A_TOKEN_ROUTER` from it.
  `harness/infra/a2a_tokens.sh` only generates a set for a fresh, separate deployment.

- Local fixture-mode agents on the GX10 load `data/team-release/agents-local.env`: the team A2A tokens plus
  `DEMO_ALLOWLIST_*`, and no provider keys. Without an allowlist every outreach is refused by design (P-011), and the
  refusal cascades Voice Desk → Verification → Risk → Carrier AP as `child task failed`.

## Run
```bash
make harness          # :8000
make router           # Redis `events`, group `router` -> first hop (or HARNESS_CONSUME=1 make harness; never both)
.venv/bin/python -m pytest harness/tests            # redaction units + hero e2e (skips if models are down)
.venv/bin/python -m harness.tests.bench_latency     # p50 per model
```

## Behaviour worth knowing
- ACK only after a terminal outcome (queued / accepted / duplicate / not_routed / triage). Failed dispatches stay pending
  and are reclaimed after 120 s (`ROUTER_RECLAIM_IDLE_MS`); entries still running are never reclaimed.
- Unknown or low-confidence (<0.6) classifications, and routable events with no upstream `case_id`, go to Redis
  stream `triage`. Malformed entries go to `events:dead_letter`.
- `FIRST_HOP_TRANSPORT=a2a` (default) dispatches with W3's `common.a2a_transport` (`A2A_TOKEN_ROUTER` in live
  mode), idempotency key `event:<event_id>`. `FIRST_HOP_TRANSPORT=spacetime` instead calls `enqueue_first_hop`
  (router identity, write id `first_hop:<event_id>`), which commits the `routing_outbox` row and a `queued`
  `dispatch` row together; the dispatch worker delivers it over A2A. It needs `SPACETIME_DISABLED=0`; otherwise
  every routed event stays pending. The first attempt's payload is stored in `ingest.sqlite` and resent verbatim on
  every retry, because the reducer rejects a reused write id with changed contents (as HTTP 530).
- Model-bound events share `ROUTER_MODEL_CONCURRENCY` (8) slots; the rest skip the queue.
- `python -m harness.tests.batch_bench` scores a fixed batch against the W4 ground truth (see its docstring).
- Envelope schema is unchanged: review flags ride in `subject` (`needs_review`, `review_notes`, `doc_type`).
- Tokens look like `[[ACCOUNT_1a2b3c4d]]` and are deterministic per vault, so agents can still correlate values.
