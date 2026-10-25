# FreightLine: Autonomous Back Office for Freight Brokerages

Nine brokerage-specific agents coordinate through real A2A services and SpacetimeDB: Carrier Settlement,
Shipper Billing & Collections, Load Profitability, Accessorial Audit, Carrier Trust & Verification,
POD & Document Chase, Settlement Approvals, Dispatcher Outreach, and Settlement Quality Review.
Each service owns its provider/tool execution. ASUS handles intake and first-hop routing only.
Financial changes go through deterministic controls with a Git-style audit trail.

The current sponsor work focuses on **Cartesia and Apify**. CRS and Nessie are deferred.
Apify carrier/website observations have been verified through live A2A tasks. Cartesia has three
hosted personas and an outbound number configured. The team phone demo now supplies a complete
synthetic load brief and five case-scoped lookup tools; see the [role-play guide](docs/VOICE-DEMO.md).
The inbox distinguishes a synthetic **Demo case** from **Live external evidence** returned by tools.

## Layout (owner in brackets)
| Path | What | Owner |
|---|---|---|
| `harness/` | Legacy mock endpoints; ASUS intake classification is owned by W1 | W1 ASUS |
| `spacetime/` | SpacetimeDB module: cases, tasks, ledger, holds, approvals, audit, memory | W2 |
| `web/` | Next.js dashboard (network, feed, case tree, audit grid, flags, demo console) | W2 |
| `common/` | Shared contracts (Pydantic), A2A runtime, policy engine, outreach guard, clients | W3 |
| `agents/<role>/` | One A2A service per agent: `card.py`, `instructions.md`, `knowledge/`, `tools.py`, `main.py` | W3 |
| `router/` | Redis Streams consumer, first-hop routing only | W1 ASUS |
| `gateway/` | Cartesia completion polling, optional webhooks/in-call tools, demo endpoints | W3 + W4 |
| `sim/` `ingest/` | Synthetic brokerage + real counterparties, ingestion | W4 |
| `integrations/` | Real bounded Apify adapters; legacy enrichment/CRS/Nessie/email scaffolds remain unavailable or deferred | W4 |
| `evaluator/` | Answer-key scoring | W4 |

## Ports
Gateway 9000 · Harness 8000 · AP 9101 · AR 9102 · CFO 9103 · Risk 9104 · Controller 9105 ·
Verification 9106 · Voice Desk 9107 · Validator 9108 · Documents 9109 · Web 3001

Service IDs and existing ports remain stable. Override a destination with `AGENT_URL_<NAME>`
(for example `AGENT_URL_DOCUMENTS=https://documents.example.com`), and a local bind port with
`AGENT_PORT_<NAME>`. The six frontline specialists and three supporting specialists are nine
services total; each case invokes only the specialists it needs.

## Quick start
```bash
cp .env.example .env
make setup && make infra
make agents    # terminal 1: nine independent services
make demo      # regression fixture over real A2A; not a live integration claim
make test
```

Service targets (`agents`, `gateway`, `router`, `ingest`, `sim`, `world`, `source`, `harness`,
`review-worker`) load `.env` themselves; pick another profile with `ENV_FILE=path/to/file.env`.
Variables already set in your shell take precedence. `make test`, `make lint` and `make demo` never
load it, so a live profile in `.env` cannot reach the test suite. Don't `source` a live `.env` into
the shell you run tests from.

See [the A2A runbook](docs/A2A-RUNBOOK.md) for the implemented milestone, live credentials,
source-data contract, recovery behavior and remaining work. Model and call results are explicitly
simulated until live mode is configured and separately validated.

## Cartesia and Apify locally

Save `CARTESIA_API_KEY` and `APIFY_TOKEN` in the ignored `.env`. The nine agents use 22 scoped
tools, including public carrier lookup, company website inspection and voice readiness checks.
Tool calls and results appear in the inbox with model, timing, status and cited evidence.

The configured local stack uses the private profile loaded by these commands in separate terminals:

```bash
make live-agents
make live-gateway
make web
```

The gateway polls Cartesia locally, so the default setup does not require a public URL or webhook
secret. The hosted personas cover verification, collections and approvals. Calls require
a real allowlisted team destination and a verified case contact. Ordinary calls also require an
open calling window and respect one attempt per company per hour. Source
and phone configuration stay private; API keys are never sent to the browser.

Readiness and the public-data demonstration can be inspected or run separately:

```bash
.venv/bin/python -m scripts.setup_cartesia       # read-only account/resource check
.venv/bin/python -m scripts.live_sponsors check-voice  # no phone call
.venv/bin/python -m scripts.live_sponsors lookups --run-id sponsors-next
```

The lookup command uses the prepared local sponsor case and may spend model/Apify credits;
the [runbook](docs/A2A-RUNBOOK.md#use-real-apify-evidence) explains first-time preparation and limits.
Actor starts are durable, results are cached for 24 hours, and pending runs can be polled without
resubmission. Public contacts remain unverified observations and cannot authorize outreach,
bank changes or payment. The completed report is `data/live/sponsor-lookups-sponsors-002.json`;
it reused the original actor runs with updated normalization and no new paid run. Validation
passed 414 backend tests and 41 frontend tests, plus lint and the frontend production build.

The Demo Console now includes **Call my phone** for the prepared team case. Each explicit click
starts a durable Verification → Dispatcher Outreach A2A workflow. This local operator control
can call outside business hours and repeat immediately after the previous call and agent work
finish. It allows only one active request; reloads and retries reuse the saved request.
Enable `DEMO_PHONE_ENABLED=1` and the same private `DEMO_CONTROL_TOKEN` in the gateway profile
and `web/.env.local`, with `GATEWAY_URL=http://127.0.0.1:9000` in the latter. Never use a
`NEXT_PUBLIC_*` variable for this token. The button cannot choose another destination.

The fictional GG-1042 scenario covers an Oakland–Sacramento dry-van load with a $250 detention
claim and a provisional $125 calculation pending signed support. Voice can look up the load,
documents, case summary, invoice and payment status. Tools run through a separate HTTPS-tunneled
read-only service on :9001, authenticated with a short-lived per-call capability. Call completion
still uses the local poller. The console includes an expandable **Demo call guide** with suggested
questions and expected facts. No additional phone call was placed while implementing this update.
The explicit call command and hosted
webhook-tool setup are documented in the [voice runbook](docs/A2A-RUNBOOK.md#enable-a-real-demo-phone-call).

For W2's live database and dashboard, follow [spacetime/README.md](spacetime/README.md).
Run `make spacetime-start`, then `make spacetime-publish spacetime-generate` and `make web`.
The [integration guide](spacetime/INTEGRATION.md) and [reducer contracts](spacetime/module/CONTRACT.md)
describe service identities, payloads and the Validator work queue for the other workstreams.

## Live data (W4)
`make sim` first backfills completed history and warm-starts in-flight loads, then starts new loads about every
8 seconds. It writes synthetic PDFs/JSON, raw `.eml` messages, and BAI2 bank records under `data/`, publishing
arrival metadata to Redis `raw` rather than extracted answers. The simulator also appends its raw event log to
`data/events.jsonl` for formal verification. The `ingest` consumer reads Redis `raw` and the referenced files,
combines them with the structured TMS feed, and publishes exactly the routable case events to Redis `events`.
The ASUS/W1 router consumes `events`; extracted and reviewable document results go separately to Redis
`documents` for audit and W2 workflows, never to the router. All three stream names are configurable through
`RAW_STREAM`, `EVENTS_STREAM`, and `DOCUMENTS_STREAM` (defaults: `raw`, `events`, and `documents`). Its
transactional load, invoice, remittance, and bank-offset state lives in `data/ingest.db`. Use `make world` to
refresh the public counterparty directory and `make ingest` to run the consumer.

Ingest also keeps the agents' source dataset current in `data/source.json` (`SOURCE_EXPORT_PATH`), the file live
agents read through `SOURCE_DATA_PATH`. It is rewritten atomically before any case-opening event is published, so
a routed case is always readable. One case per resolved case ID: `CASE-<load>` (carrier invoice), `-R` (re-numbered
invoice), `-BANK` (bank-change request) and `-AR` (shipper receivable). Brokerage records are marked
`"synthetic": true`; companies and contacts come from `data/world.json` with their FMCSA/SEC provenance. Values
ingest never observed are omitted rather than defaulted. `make source` re-exports from `data/ingest.db`. Simulator retention is
configurable with `SIM_DATA_MAX_MB` and `SIM_KEEP_LOADS`; hero fixtures and event/answer-key history are preserved.

For a Redis-free dataset and formal checks:
```bash
.venv/bin/python -m sim.generator --offline --loads 40 --data /tmp/w4-data
make verify-data SIM_DATA_DIR=/tmp/w4-data
SIM_DATA_DIR=/tmp/w4-data WORLD_FILE=/tmp/w4-data/world.json .venv/bin/python -m ingest.source_export \
  --replay /tmp/w4-data/events.jsonl --db /tmp/w4-data/replay.db   # -> /tmp/w4-data/source.json
```
`make verify-data` defaults to `data/` and writes `verify_report.json` there.

## Rules
- `OUTREACH_MODE=demo`: calls/emails are only ever routed to `DEMO_ALLOWLIST_*`; the real contact is shown as `intended_to`.
- Money and ledger changes go through `common/policy.py` and SpacetimeDB reducers; never through the LLM.
- Never commit `.env` or keys. The versioned requirements live at `docs/PRD.md`; private implementation notes stay under ignored `docs/private/`.
- The fixed hero case is one regression fixture. Production workflows use each case’s own records, amounts and evidence.
- Fixture/provider/voice tests do not establish live integration. Real inference, calls and external data require configured credentials and separate acceptance checks.
