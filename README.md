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

## Live data (W4)
`make live` starts Redis, all 8 agents, the router, the gateway and the real-time simulator, then open
`http://localhost:9000/console` and paste `GATEWAY_TOKEN`.
- **Counterparties are real.** Carriers come from FMCSA's Company Census (active, for-hire, general freight, with a
  registered phone and email); shippers come from SEC EDGAR (manufacturing, wholesale and retail filers). `make world`
  refreshes `data/world.json`. Contacts are shown unmasked; outreach still only reaches the demo allowlist.
- **Events are real-time, on a business clock.** Every ~20s a new load starts on a real lane (Census-geocoded
  facilities, real mileage, FSC, accessorial schedule). Business time runs `SIM_TIME_RATIO`x wall time (default
  1 s = 10 min), so tender -> pickup -> transit -> delivery -> invoice -> shipper payment plays out in minutes with
  consistent timestamps. Each load produces the full paper trail: TMS status and GPS breadcrumbs, PDF rate con /
  BOL / signed POD / carrier invoice (per-carrier layout) / shipper invoice under `data/docs/`, raw `.eml` inbox
  messages under `data/mail/` (never delivered), and BAI2-style bank lines under `data/bank/`.
- **Planted exceptions** (unsupported/supported detention, shipper short-pay, lookalike-domain bank change,
  unverified factoring remit-to, duplicate and renumbered-duplicate invoices, linehaul over contract, fuel-surcharge
  error, line items that don't sum, lumper without receipt, unsigned POD) are logged with the planted field values
  and the expected outcome in `data/answer_key.jsonl`.
- **Humans play the counterparty.** Calls and emails park in the console until someone answers as the carrier/shipper.

## Rules
- `OUTREACH_MODE=demo`: calls/emails are only ever routed to `DEMO_ALLOWLIST_*`; the real contact is shown as `intended_to`.
- Money and ledger changes go through `common/policy.py` and SpacetimeDB reducers; never through the LLM.
- Never commit `.env` or keys. The PRD lives at `docs/PRD.md` (gitignored, shared privately).
