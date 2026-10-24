# FreightLine operational database (W2)

SpacetimeDB **2.10.2**, TypeScript module, generated TypeScript dashboard bindings, and a
Python HTTP integration boundary. Operational records, identity permissions, financial controls,
delivery work and audit history live in the database. Agent execution stays with W3.

The deployed team database is **`at350-freightops` on Maincloud**. See
[CLOUD.md](CLOUD.md) for the account dashboard, connection settings, and update commands.
The local workflow below remains available for development.

## Run locally

Prerequisites: Python 3.12, Node.js, and the `spacetime` 2.10.2 CLI.

```bash
make setup
make spacetime-setup
make spacetime-start                 # terminal 1, persistent local server on :3000
make spacetime-publish               # terminal 2, build + publish + service identities
make spacetime-generate              # refresh typed WebSocket client bindings
make web                            # dashboard on :3001
```

`spacetime-publish` uses an isolated CLI configuration and refuses destructive data resets.
Credentials live in ignored `data/w2/freightops/`, with owner-only permissions. Re-running it
reuses identities. No remote deployment or global CLI login is changed.

Open the dashboard and grant its displayed identity a human role:

```bash
.venv/bin/python -m scripts.spacetime_admin grant-human <identity> --name alan
```

For a populated development view, explicitly opt into labeled synthetic fixtures:

```bash
make spacetime-demo
```

Fixtures are stored through the real reducers with idempotent IDs, and all case titles carry
`[W2 fixture]`. They do not place calls, send email, run agents, perform inference or fabricate
performance scores. W4's actual directory and simulation can coexist under different IDs.

## What is implemented

- The 24 PRD tables, plus stored invoice/period/journal-request truth, principals, forecasts,
  immutable memory revisions, leased routing work, and private approval/audit bookkeeping.
- Authenticated role checks on every reducer; infrastructure admin cannot impersonate an agent.
- Dollar inputs validated to cents, balanced journals, open periods, requester/approver separation,
  invoice hold/payment bounds, duplicate-payment protection, and target-bound human approvals.
- Atomic change sets, cell deltas and SHA-256 audit chains for mutations; append-only journals.
  Audit hashes cover the actual changed cells, author and parent hash.
- Task ownership/state transitions, allowlisted delegation, cycle/depth/fanout guards, and
  durable Validator delivery work with claim/ack leases.
- Human flags/takeover, versioned memory/instructions and compensating instruction revert;
  caller-selected learning text cannot modify policy or permissions.
- Generated WebSocket subscriptions powering the dashboard, with a read-only HTTP fallback.

Read [the reducer payload reference](module/CONTRACT.md) before integrating. Read
[the teammate handoff](INTEGRATION.md) for service credentials, ownership and work delivery.
The [dashboard README](../web/README.md) covers browser settings and views.

## Verify

```bash
make w2-check                       # Python lint/tests, module tests/build, dashboard lint/build
make spacetime-test                 # real HTTP invariants in a fresh local test database
```

The smoke test publishes a uniquely named `freightops-test-*` database, provisions separate
identities and exercises authorization, rollback, cents, holds, human approvals, replay,
journals, flags, concurrency and independent audit-hash verification. It leaves an acceptance
report in `data/w2/<test-database>/acceptance.json`. It never replaces your working database.

See [VALIDATION.md](VALIDATION.md) for the completed checks, measured local subscription
latencies, browser verification, and the remaining sustained end-to-end validation.

## Integration responsibilities

W3 must call the client from its runtime/financial tools and consume `routing_outbox`. W4 must
mirror source invoices and counterparties, implement the demo gateway endpoints, and publish
measured scores. W1 must publish real telemetry and read model configuration. The dashboard
shows pending work and unavailable upstream services honestly until those integrations run.

Read access to the operational tables is public by design for this single-company hackathon
dashboard; mutation authorization is enforced by identity. Do not deploy sensitive company
data publicly without adding read authorization. Approval secrets and tokens are never public.

Official API: [HTTP reducers and SQL](https://spacetimedb.com/docs/http/database/),
[TypeScript modules](https://spacetimedb.com/docs/quickstarts/typescript/),
[identity authorization](https://spacetimedb.com/docs/http/authorization/).
