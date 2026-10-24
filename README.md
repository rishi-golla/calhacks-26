# FreightOps: Autonomous Back Office for Freight Brokerages

Specialized finance agents (Carrier AP, Shipper AR, CFO, Risk & Compliance, Controller, Verification,
Validator, Voice Desk) run as independent A2A services on a sandboxed multi-model harness, coordinate
through SpacetimeDB, and act on the books only through deterministic controls with a Git-style audit trail.

## Layout (owner in brackets)
| Path | What | Owner |
|---|---|---|
| `harness/` | OpenAI-compatible multi-model router + tool sandbox (mock for now) | W1 ASUS |
| `spacetime/` | SpacetimeDB module: cases, tasks, ledger, holds, approvals, audit, memory | W2 |
| `web/` | Next.js dashboard (network, feed, case tree, audit grid, flags, demo console) | W2 |
| `common/` | Shared contracts (Pydantic), A2A runtime, policy engine, outreach guard, clients | W3 |
| `agents/<role>/` | One A2A service per agent: `card.py`, `instructions.md`, `knowledge/`, `tools.py`, `main.py` | W3 |
| `router/` | Redis Streams consumer, first-hop routing only | W3 |
| `gateway/` | Cartesia webhooks, in-call tools, demo endpoints | W3 + W4 |
| `sim/` `ingest/` | Synthetic brokerage + real counterparties, ingestion | W4 |
| `integrations/` | Apify, context.dev, contact data, CRS, Nessie, Cartesia, email (mockable) | W4 |
| `evaluator/` | Answer-key scoring | W4 |

## Ports
Gateway 9000 · Harness 8000 · AP 9101 · AR 9102 · CFO 9103 · Risk 9104 · Controller 9105 ·
Verification 9106 · Voice Desk 9107 · Validator 9108 · Web 3001

## Quick start
```bash
cp .env.example .env
make setup && make infra
make harness   # terminal 1
make agents    # terminal 2
make demo      # hero smoke test: Carrier AP -> Risk over real A2A
make test
```

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
refresh the public counterparty directory and `make ingest` to run the consumer. Simulator retention is
configurable with `SIM_DATA_MAX_MB` and `SIM_KEEP_LOADS`; hero fixtures and event/answer-key history are preserved.

For a Redis-free dataset and formal checks:
```bash
.venv/bin/python -m sim.generator --offline --loads 40 --data /tmp/w4-data
make verify-data SIM_DATA_DIR=/tmp/w4-data
```
`make verify-data` defaults to `data/` and writes `verify_report.json` there.

## Rules
- `OUTREACH_MODE=demo`: calls/emails are only ever routed to `DEMO_ALLOWLIST_*`; the real contact is shown as `intended_to`.
- Money and ledger changes go through `common/policy.py` and SpacetimeDB reducers; never through the LLM.
- Never commit `.env` or keys. The PRD lives at `docs/PRD.md` (gitignored, shared privately).
