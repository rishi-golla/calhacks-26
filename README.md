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

## Rules
- `OUTREACH_MODE=demo`: calls/emails are only ever routed to `DEMO_ALLOWLIST_*`; the real contact is shown as `intended_to`.
- Money and ledger changes go through `common/policy.py` and SpacetimeDB reducers; never through the LLM.
- Never commit `.env` or keys. The PRD lives at `docs/PRD.md` (gitignored, shared privately).
