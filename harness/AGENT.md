# W1 harness: agent handoff (ASUS intake and first-hop router)

Status as of **2026-10-24** (branch `w1-harness`, latest `ff32b82`; `main` is a few commits behind until the next
fast-forward). Read this, then `harness/README.md`, `harness/DATA_CONTRACT.md` and `docs/PRD.md` (sections 6 W1,
7.1–7.4, 9). The repo-wide rules in the root `AGENTS.md` also apply.

## 1. What W1 is
The ASUS GX10 is the brokerage's **intake layer**. It reads business events from Redis, validates and cleans them,
scrubs personal data, works out which agent owns each one, and sends that agent the first A2A task. It writes one
intake record per event to SpacetimeDB. After the hand-off, the agents own the case and delegate among themselves
over A2A; W1 never proxies their inference or relays child tasks (PRD v1.3).

```text
sim / inbox → Redis `raw` → W4 ingest (mirrors cases/invoices/directory into SpacetimeDB) → Redis `events`
  → W1 router on the GX10:
      validate input → duplicate + identity memory (SQLite) → scrub personal data (vault)
      → build context (Elasticsearch) → route (7.3 table / classifier) → extract names+addresses (model)
      → exact amounts (code) → personal-data audit (model) → TaskEnvelope
  → A2A SendMessage to the owning agent (Carrier AP / Shipper AR / Risk / Documents)
  → `dispatch` row in SpacetimeDB · `triage` stream for people · `events:dead_letter` for malformed input
```

## 2. Requirement status

### PRD W1 requirements (section 6)
| ID | Requirement | Status | Where / evidence |
|---|---|---|---|
| H1 | Consume `events`, group `router`; validate events; existing contracts only | ✅ | `ingest/consumer.py`, `ingest/validate.py` |
| H2 | Typed events routed by the 7.3 table; ambiguous items classified into an allowlist | ✅ | `ingest/routing.py`; classifier 60/60 on `tests/slot_eval.py` |
| H3 | Agent card and skill checked before dispatch | ✅ | W3's `common/a2a_transport.send_task` |
| H4 | Idempotent dispatch, ack after durable acceptance, reclaim, retry | ✅ | key `event:<id>`; ack only after acceptance **and** its telemetry row; reclaim after 120 s; entries still running are never reclaimed |
| H5 | P0 reserve, bounded in-flight, queue depth/age exposed, no head-of-line blocking | ⚠️ partial | P0-first ordering, 8 model slots, 64 in-flight cap; unrouted events skip the queue. **Missing:** a per-destination bound, and exposed queue depth/age |
| H6 | Classifier output validated; unknown or low-confidence → visible triage | ✅ / ⚠️ | allowlist and confidence ≥ 0.6 → `triage` stream and a `dispatch` row with status `triage`. **Missing:** a triage reader/UI (PRD audit gap) |
| H7 | Intake isolation: bounded time, no network for preprocessing | ✅ | per-call deadlines; models on 127.0.0.1. Legacy `/v1/sandbox/exec` is a stub (sandboxes are W3's under v1.3) |
| H8 | Only intake credentials on ASUS | ✅ | `.env` holds harness/router SpacetimeDB tokens and the router A2A token |
| H9 | Untrusted text can't change routing or controls | ✅ | allowlisted classifier; injection text travels as inert data |
| H10 | Telemetry: event, case, skill/agent, task, policy version, reason, queue time | ✅ | `ingest/telemetry.py` → W2 `record_dispatch` (router role); verified on Maincloud. `log_llm_call` rejects `router` (W2 decision pending) |
| H11 | Known types still route if classification is down; work stays queued if ASUS stops | ✅ | model outage → flagged or triaged, never guessed; 503 `{"error":"unavailable"}` on the chat API |

### PRD W1 done-when
| Criterion | Status |
|---|---|
| A carrier invoice from the live stream reaches Carrier Settlement over real A2A | ✅ (hero e2e test; live demo) |
| Re-delivering its event ID creates no second task | ✅ (tested; agents also dedupe on the key) |
| A queued event survives a router restart | ✅ (shown live; the 14,809-item sim backlog drained with 0 lost) |
| AP → Risk → Verification continues after the router stops | ⚠️ true by design (agents delegate directly); **no explicit test** |
| Intake-to-acceptance latency and throughput reported | ✅ ~1.6 s p50 per document, 163–170 docs/min (300-doc benchmark) |

### Original W1 brief (harness owner's four parts)
| Part | Status |
|---|---|
| 1. Three local models on 127.0.0.1 ports, launch scripts in `harness/infra/` | ✅ (lineup changed for speed; see §4) |
| 2. Harness on :8000 keeping the v1.0 7.1 shape (logical names, routing table, JSON validation + retry, harness block, 503, real `/v1/models`) | ✅ W1-internal under PRD v1.3 |
| 3. Stage 1 ingest: Redis consumer + `POST /v1/ingest`, regex redaction + local vault, model extraction, 7.3 routing, cross-check, TaskEnvelope, A2A | ✅ (cross-check is now a personal-data audit; amounts moved to exact code) |
| 4. Tests: redaction units, hero e2e, p50 latency per model | ✅ plus benchmarks and evaluations (§6) |

### Added during the build (team asks and findings)
| Item | Status |
|---|---|
| Personal-data scrubbing scored against ground truth | ✅ 100% recall, 0 of 1,215 planted values leaked |
| Exact money reading (models misread "$1,769.44") | ✅ `ingest/amounts.py`, 132/132 totals, line items and payments |
| Input validation of synthetic or real payloads | ✅ `ingest/validate.py` |
| Duplicate invoices (P-007) | ✅ `ingest/memory.py`, 45/45 on the recorded run, 0 false |
| Carrier identity vs the verified directory (payee, USDOT, sender domain, lookalikes) | ✅ double brokering 3/3, factoring 15/15, bank-change 13 flagged, 0 false on clean |
| Elasticsearch context memory (carrier history, similar documents) | ✅ `ingest/context.py`; originals surfaced 45/45, history warnings 233/233, 0/238 false |
| History-based validation (amount outliers, velocity) | ❌ evaluated, **not shipped**: `tests/history_eval.py` (P-001 already catches 100%) |
| W4 demo allowlist labels (`team-N`) never tokenized | ✅ guaranteed in code with a regression test |
| PODs → Documents `receive_document` (W4 #9) | ✅ |
| Live demo script | ✅ `python -m harness.demo` (6 events end to end) |
| Fully open models | ✅ tested, alternative lineup (`OPEN_MODELS.md`); **not live**, the team chose speed |

### Open items
| # | Item | Owner | Notes |
|---|---|---|---|
| 1 | Triage reader/UI (PRD audit gap) | W2 + W1 | `dispatch` rows with status `triage` already carry the reason; needs a view and a "resolve → attach case → re-send" action |
| 2 | H5 per-destination bound, queue depth/age metrics | W1 | publish via the `metrics:w1` Redis key W4's evaluator reads (agreed, not built) |
| 3 | Intake metrics publishing (`metrics:w1`, `metrics:w1:benchmark`) | W1 → W4 evaluator → W2 tiles | design agreed with W4; not built |
| 4 | `route_event` (rules-only helper) leaks free-text names | W1 | the live router is unaffected; fix by dropping free-text fields from that helper |
| 5 | Elastic backfill used rules-only scrubbing | W1 | historical records may hold free-text names (local only); re-run through the model step or drop text |
| 6 | Encrypted vault + audited un-scrub ("data under lock") | W1, needs decisions | key owner, un-scrub roles, retention: defaults proposed (key file, controller + second approver for bank details, 90 days) |
| 7 | Redis durability (`appendonly yes`) and auth | infra owner | shared `docker-compose.yml`; every `REDIS_URL` changes |
| 8 | Rotate keys pasted into chat (OpenAI, Cartesia, Apify) | team | not done |
| 9 | Live-mode agent rehearsal and timings | W3 | agents here run in fixture mode on purpose |
| 10 | W4 ingest on the GX10 is behind `main` | W4 | it needs `SPACETIME_TOKEN_INGEST` to mirror cases before routing |
| 11 | Explicit test: agent chain continues with the router stopped | W1 + W3 | done-when criterion |
| 12 | Optional: CRS identity check for owner-operators on payee/bank flags | W1 | CRS "Identity/Fraud" track |

## 3. Code map (`harness/`)
| Path | Role |
|---|---|
| `server.py` | FastAPI on 127.0.0.1:8000: `/v1/chat/completions` (W1-internal), `/v1/models`, `POST /v1/ingest[?dispatch=false]` |
| `config.toml` | model backends (ports, timeouts), logical chains, `auto` routing table, `policy_version` |
| `pool.py`, `chat.py` | backend health and slots, routing, strict JSON (NaN/Infinity/surrogates rejected), retry, fallback, harness block |
| `ingest/consumer.py` | Redis loop: one task per entry, model slots, reclaim, dead letter, telemetry before ack |
| `ingest/pipeline.py` | per-event flow (validate → memory → scrub → context → route → extract → audit → envelope → dispatch) |
| `ingest/validate.py` | per-type input checks; fix / flag / reject |
| `ingest/memory.py` | SQLite memory: invoice fingerprints (14 days), carrier identity (directory first) |
| `ingest/context.py` | Elasticsearch context memory; `--backfill` |
| `ingest/redact.py`, `vault.py` | regex + key rules, structured spans, deterministic tokens, local vault (owner-only SQLite) |
| `ingest/amounts.py` | exact Decimal reading of totals, line items, payments |
| `ingest/routing.py` | 7.3 table, classifier prompt and schema (reason first), allowlist |
| `ingest/dispatch.py`, `firsthop.py`, `telemetry.py` | A2A send (W3 transport), metadata conventions, `record_dispatch` |
| `demo.py` | live 6-event demo |
| `infra/` | vLLM launch scripts, open-model serving, verified downloader, A2A token generator |
| `tests/` | unit + e2e tests; `batch_bench.py`, `slot_eval.py`, `validation_eval.py`, `history_eval.py`, `bench_latency.py` |

`router/main.py` is only the entrypoint (`make router`); it re-exports `route_event` and `ROUTES`.

## 4. Running it on the GX10
**Models (Docker, all on 127.0.0.1, `--restart unless-stopped`):**
| Container | Port | Model | Used for |
|---|---|---|---|
| `qwen4bfp8` | 8009 | Qwen3-4B-Instruct-2507-FP8 | **live**: classify, find names/addresses, personal-data audit |
| `nano` | 8001 | Nemotron-3-Nano-Omni-30B (FP8) | fallback for extraction |
| `qwen4b` | 8002 | Qwen3-4B (BF16) | fallback for classify/audit |
| `elastic` | 9200 | Elasticsearch 8.15.3 | context memory |
| stopped | 8003–8008 | gpt-oss (NaN output on GB10), OLMoE, Apertus, OLMo-3, SmolLM3 | see `OPEN_MODELS.md` |

Launch scripts: `infra/serve_*.sh` (run via `sg docker -c "bash …"`); `infra/wait_health.sh <port>` gates on `/health`.

**Services (tmux session `freightops`; `tmux attach -t freightops`):**
| Window | Command | Notes |
|---|---|---|
| `harness` | `make harness` | loads `.env` automatically (W4's `make-env-file` change) |
| `router` | `make router` | loads `.env`: SpacetimeDB router identity, `A2A_TOKEN_ROUTER`, `SOURCE_DATA_PATH` |
| `agents` | `ENV_FILE=data/team-release/agents-local.env SPACETIME_DISABLED=1 … make agents` | fixture mode; team A2A tokens + demo allowlist only; source dataset from W4's export |
| `ingest` | W4 ingest from `~/w4data` | W4-owned |

**Secrets (never commit; values are not in this file):**
- `~/calhacks-26/.env` (gitignored, owner-only): `SPACETIME_*` for harness/router, `A2A_TOKEN_ROUTER`, `REDIS_URL`, `SOURCE_DATA_PATH`
- `data/team-release/` (gitignored): the team bundle (`a2a.env`, `spacetime.env`, `team.env`, `source.json`) and `agents-local.env`
- `~/.config/freightops/maincloud-services.env`: the full Maincloud service-token set
- `harness/data/` (gitignored): `vault.sqlite` (real values; owner-only, **not encrypted**), `ingest.sqlite`, `memory.sqlite`, logs, benchmark reports

**Shared systems:** Redis `redis://10.0.17.206:6379/0` (no password yet); SpacetimeDB Maincloud `at350-freightops`
(the harness writes `dispatch` only); W4 source dataset `~/w4data/data/source.json`.

## 5. Contracts you must not break
- **Event in:** `XADD events * json '<event>'`, with `event_id` (unique and permanent; it's the dedup key), `type`,
  `payload` (object), and `case_id` (event or payload; **`case_hint` is never turned into a case**). Details:
  `DATA_CONTRACT.md`.
- **A2A out:** `Authorization: Bearer $A2A_TOKEN_ROUTER`; `SendMessage` with `contextId = case_id`,
  `parts[0].data = TaskEnvelope` (the schema is frozen in `common/envelope.py`; review flags and context ride in
  `subject`); metadata `from_agent=router`, `delegation_depth=0`, `visited=[]`, `idempotency_key=event:<id>`,
  `correlation_id=case_id`, `causation_id=event_id`; `returnImmediately`.
- **Tokens:** `[[KIND_8hex]]`, deterministic per vault. Agents never see real values. Contact details come from the
  verified directory, never from a document.
- **Telemetry:** `record_dispatch` with `write_id = dispatch:<redis-entry>:<status>`. GPS pings get no row. A failed
  write leaves the entry pending (durable acceptance).
- **Streams:** `triage` holds items people must resolve; `events:dead_letter` holds malformed entries (fields
  `entry_id`, `reason`, `json`).

## 6. Quality bar and how to check it
```bash
make lint test                                              # 814 tests; run with `set -o pipefail` when chaining
python -m harness.demo                                      # live, 6 events end to end
python -m harness.tests.batch_bench [--limit N] [--dispatch] # 300 docs vs W4 ground truth
python -m harness.tests.slot_eval qwen4bfp8                 # classifier, 60 labelled documents
python -m harness.tests.validation_eval                     # validation + memory vs the answer key (no models)
python -m harness.tests.history_eval                        # why history checks aren't shipped
```
Current numbers (300 documents unless noted): 163–170 docs/min · p50 ~1.6 s · redaction recall 100% (0 of 1,215) ·
invoice totals and line items 132/132 · 0/83 clean invoices flagged · planted math errors 4/4 · duplicates 45/45 ·
identity fraud signals with 0 false flags. **Don't merge a change that lowers any of these** without saying so.

## 7. Gotchas learned the hard way
- **`pkill -f` / `pgrep -f` match their own shell** when the pattern appears in the command line. Kill by PID, or use
  `ps -C python -o pid,args | awk …`.
- **`cmd | tail` hides failures.** Use `set -o pipefail`; one commit went in on a failing test this way.
- **gpt-oss on GB10:** the MXFP4 Marlin kernel emits NaN logits (token 0 "!") under load, batched or not. Parked.
- **Nemotron Omni:** don't zero all media limits (`KeyError '0.weight'`); keep NVIDIA's limits at 1.
- **`hf download`:** one `--include`/`--exclude` per pattern. On flaky DNS it discards partial files; use
  `infra/download_open_models.py` (curl resume + SHA-256).
- **Elasticsearch:** numbers in a date range query are milliseconds unless the query says `"format": "epoch_second"`.
- **Models and numbers:** models misread amounts and misjudge equal numbers ("9151.8 ≠ 9151.80"). Money is
  read and compared by code; models only do language.
- **The verifier listed our own tokens as leftovers.** Only items that appear in clear text count.
- **FP8 Qwen's first requests after a start can error.** Re-run before judging accuracy.
- **Demo reruns:** memory remembers invoices, so demo events need unique loads per run (done).
- **Tests and benchmarks must not touch live memory:** pass `memory=Memory(tmp)`, `context=ContextStore("")` or
  their own index.

## 8. Decisions on record
| Decision | Why |
|---|---|
| ASUS is intake + first hop only (PRD v1.3) | agents own inference and delegation |
| W1 router replaced W3's bridge, keeping its conventions | one router; W3's tests ported to `tests/test_router.py` |
| Speed + accuracy over fully open models | team decision after Hyphenate conversation; open lineup documented as an alternative |
| Money handled by code, language by models | measured: models fail at amounts (OLMo-3 0/132), code is 132/132 |
| One model for find + audit (independence from deterministic layers) | a Nano auditor cost 3× speed and 13/54 false flags |
| History is context, not a review trigger | as a trigger it would flag ~1 in 4 invoices (mostly legitimate factoring) |
| No history-based validation | evaluated; plain P-001 catches 100% with 0 false flags |
| Telemetry write is part of durable acceptance | W2's contract; both writes idempotent |
| `triage` and `events:dead_letter` stay separate | people vs malformed data; the PRD line saying otherwise should be corrected |

## 9. Rules for agents working here
- Edit only `harness/` (and `router/main.py`, `tests/test_router.py` when the change needs it) unless the W1 owner
  approves. Contracts in `common/envelope.py` change only with both owners.
- Never commit `.env`, tokens, keys, model weights, `harness/data/`, or `data/team-release/`. Check staged diffs.
- Run `make lint test` (with `pipefail`) before every push. Branch `w1-harness`; merge to `main` only when asked.
- Measure before claiming: every check here is scored against the simulator's answer key. Report numbers that got
  worse, not just the ones that improved.
- Shared Maincloud is shared: label test events (`EVT-DEMO-…`, `EVT-W1-…`) and don't replay financial cases.
