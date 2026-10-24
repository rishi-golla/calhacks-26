# FreightOps: Autonomous Back Office for Freight Brokerages

Nine brokerage-specific agents coordinate through real A2A services and SpacetimeDB: Carrier Settlement,
Shipper Billing & Collections, Load Profitability, Accessorial Audit, Carrier Trust & Verification,
POD & Document Chase, Settlement Approvals, Dispatcher Outreach, and Settlement Quality Review.
Each service owns its provider/tool execution. ASUS handles intake and first-hop routing only.
Financial changes go through deterministic controls with a Git-style audit trail.

## Layout (owner in brackets)
| Path | What | Owner |
|---|---|---|
| `harness/` | Legacy mock endpoints; ASUS intake classification is owned by W1 | W1 ASUS |
| `spacetime/` | SpacetimeDB module: cases, tasks, ledger, holds, approvals, audit, memory | W2 |
| `web/` | Next.js dashboard (network, feed, case tree, audit grid, flags, demo console) | W2 |
| `common/` | Shared contracts (Pydantic), A2A runtime, policy engine, outreach guard, clients | W3 |
| `agents/<role>/` | One A2A service per agent: `card.py`, `instructions.md`, `knowledge/`, `tools.py`, `main.py` | W3 |
| `router/` | Redis Streams consumer, first-hop routing only | W1 ASUS |
| `gateway/` | Cartesia webhooks, in-call tools, demo endpoints | W3 + W4 |
| `sim/` `ingest/` | Synthetic brokerage + real counterparties, ingestion | W4 |
| `integrations/` | Apify, context.dev, contact data, CRS, Nessie, Cartesia, email (mockable) | W4 |
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
set -a; source .env; set +a
make setup && make infra
make agents    # terminal 1: nine independent services
make demo      # regression fixture over real A2A; not a live integration claim
make test
```

See [the A2A runbook](docs/A2A-RUNBOOK.md) for the implemented milestone, live credentials,
source-data contract, recovery behavior and remaining work. Model and call results are explicitly
simulated until live mode is configured and separately validated.

For W2's live database and dashboard, follow [spacetime/README.md](spacetime/README.md).
Run `make spacetime-start`, then `make spacetime-publish spacetime-generate` and `make web`.
The [integration guide](spacetime/INTEGRATION.md) and [reducer contracts](spacetime/module/CONTRACT.md)
describe service identities, payloads and the Validator work queue for the other workstreams.

## Rules
- `OUTREACH_MODE=demo`: calls/emails are only ever routed to `DEMO_ALLOWLIST_*`; the real contact is shown as `intended_to`.
- Money and ledger changes go through `common/policy.py` and SpacetimeDB reducers; never through the LLM.
- Never commit `.env` or keys. The versioned requirements live at `docs/PRD.md`; private implementation notes stay under ignored `docs/private/`.
- The fixed hero case is one regression fixture. Production workflows use each case’s own records, amounts and evidence.
- Fixture/provider/voice tests do not establish live integration. Real inference, calls and external data require configured credentials and separate acceptance checks.
