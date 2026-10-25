# W2 validation record

Verified locally on 2026-10-24 with SpacetimeDB 2.10.2. These results cover W2's
database, Python boundary, generated subscriptions, and dashboard. They do not
claim completion of W1/W3/W4 agent execution or external integrations.

## Automated checks

`make w2-check` passed:

- Python lint and 92 tests, including 82 focused client tests.
- 21 module tests and a successful SpacetimeDB module build.
- Dashboard lint, six tests, and a successful production Next.js build.

`make spacetime-test` passed against the isolated local database
`freightops-test-7422d9ffe1`. Its 12 acceptance groups cover public-table queries,
identity/role authorization, rollback, exact cents and invoice bounds, approvals
and single-use codes, payment binding and duplicate prevention, journal balance
and separation of duties, task ownership/delegation, durable flags, versioned
learning, human takeover, concurrency, and independent audit verification.

The run recorded 25 cases, 74 change sets, 74 audit events, and 708 cell changes.
Independent verification recomputed hashes from the actual stored cell deltas
and metadata and checked every per-case parent link. Twenty-four concurrent case
writes had a 4.1 ms median and 7.3 ms maximum HTTP latency on this machine.

The generated WebSocket client observed all 12 measured reducer updates, with a
7.0 ms median and 12.5 ms maximum. These are local reducer-to-subscription timings,
not browser paint timings or a production SLA. The PRD's 30-minute, two-events-per-
second soak has **not** been run. Reports are written under the ignored local
`data/w2/<test-database>/` directory; rerun the command to obtain fresh evidence.

## Browser and HTTP checks

The production dashboard connected to the real `freightops` module and displayed
the five explicitly labeled W2 fixture cases. Checks included mobile navigation,
live reconnect after module publication, audit cell author/task/source provenance,
persisted human feedback with a pending router work item, and wrong/correct
approval-code decisions through the generated client. Invalid codes show an
accessible error inside the form and increment the stored attempt count; valid
codes produce a subscribed decision-history row. Recording a fixture approval
did not release a hold or execute a payment.

The read-only snapshot endpoint returned HTTP 200. Both localhost and 127.0.0.1
same-origin demo requests returned the expected HTTP 503 while the W4 gateway
was disabled; a cross-origin request was rejected with HTTP 403. No calls,
emails, inference requests, or financial transfers were made by these checks.

## Remaining cross-owner validation

Follow [INTEGRATION.md](INTEGRATION.md) to connect W3's runtime and leased outbox
consumer, W4's source records/demo gateway/evaluator, and W1's model telemetry.
Run the sustained simulation and end-to-end hero scenarios once those services
are connected. Fixture records and queued work are visibly distinguished from
executed agent work throughout the dashboard.

## Maincloud deployment follow-up

Published `at350-freightops` under the verified `at350` publisher identity and
confirmed its authenticated web dashboard shows Running, 34 tables, and 36
reducers including the lifecycle initializer. Provisioned distinct cloud service
identities and seeded the five labeled W2 fixture cases through authenticated
reducers. The frontend's browser and HTTP fallback both now target Maincloud.

Browser verification confirmed live WebSocket subscriptions, the configured cloud
server/database, the demo browser's human role, and a new human flag committed to
the cloud with pending Validator delivery. The cloud-backed snapshot returned
HTTP 200. Cloud helper validation added 18 mocked tests; `make lint test` now
passes 110 Python tests. The connection-label changes passed dashboard lint,
six tests, and the production build. Local latency figures above are not cloud
latency measurements. See [CLOUD.md](CLOUD.md) for connection and provisioning details.

## Maincloud cutover

**Status: not yet run on Maincloud.** The cutover code, env generation and verifier are in
place; the republish, `provision` and the cloud checks below run after `main` carries the
cloud-backbone tickets and the `release_hold` fix, following the sequence in
[CLOUD.md](CLOUD.md). Nothing in this section is a cloud measurement unless its row says so.

`make cloud-verify` (`scripts/cloud_verify.py`) prints the checklist from anonymous SELECTs
and exits 1 when a check fails; `--readyz` adds the agents' readiness from the Mac.

| Check | How | Maincloud result |
|---|---|---|
| `agent_config` has nine rows including `documents` | `cloud-verify` | not yet run |
| nine `agent` rows, Mac `card_url`, `last_seen` advancing (15 s heartbeat) | `cloud-verify` (two reads 20 s apart; flags a card that moves between fleets) | not yet run |
| accounting periods: current month plus 18 open | `cloud-verify` | not yet run |
| a new simulator load yields `source_record` → `case` → `dispatch` queued → accepted → `task` / `delegation_edge`, in that order | `cloud-verify` case chain (newest routed case, or `--case`) | not yet run |
| no `first_hop` row older than 60 s while the worker is up | `cloud-verify` | not yet run |
| outbox backlog by kind with `last_error` | `cloud-verify` | not yet run |
| dashboard on Maincloud shows the case within seconds | browser on `web/.env.local` pointed at Maincloud | not yet run |
| every agent `/readyz` is 200 | `cloud-verify --readyz` on the Mac | not yet run |
| enqueue-to-accept latency over at least 20 events | `cloud-verify` latency line (outbox `created_at` to accepted `dispatch.ts`, both module clock) | not yet run |
| 30-minute soak at two events per second (PRD) | simulator at that rate, then `cloud-verify --latency-window 30` | not yet run |

**Local evidence (not cloud).** On the local server, the T5 run delivered `EVT-T5-live` from
enqueue to `accepted` in **1.2 s** at the 1 s poll (outbox created 04:07:43.850Z, dispatch
accepted 04:07:45.046Z); the verifier reproduces that figure from SQL. Local SQL round trips
were 6 ms median during the verifier run, against 363–1054 ms measured to Maincloud on Oct 24,
so local latencies say nothing about the cloud.

Provisioning steps against a fresh local database (`freightops-t6-9c815c`, through the local
admin helper): `ensure_agent_defaults` with only `{write_id}` was accepted and was a no-op
(init seeds all nine; no change set). With 2027-03 locked by hand first, period opening
created 18 periods from 2026-10 to 2028-04, left 2027-03 locked, and a second run made no calls.

The verifier against the T1–T5 test database `freightops-t1-3d6644` passed `agent_config` and
failed, as expected there, on heartbeats (no agents running), periods (none opened), the newest
case (a T4b test event with no source record or worker) and two deliberately stuck `first_hop`
rows. `--case T5-CASE-LIVE` shows the full chain: source record, case, enqueue, accepted after
1.2 s, task, outbox `done`.

**Readiness 503 (`SpacetimeHTTPError`).** In fixture mode `/readyz` makes no SpacetimeDB call:
an agent with `SPACETIME_DISABLED=0` returned 200 against both an existing and a nonexistent
database, and the failure only appears as an "Agent presence deferred" warning. In live mode
`ready` reads `agent_config`, and a database that does not exist answers 404, which `ready`
reports as 503 `SpacetimeHTTPError`. The checkout those agents ran from (`a2a-brokerage`) sets
`FREIGHTOPS_MODE=live` and `SPACETIME_DB=freightops-a2a-live` in both `.env` and
`data/live/runtime.env`, the same nonexistent database its review worker was polling. So the
most likely cause is that profile, not `ready`; no code change was made, and the handoff files
fix the launch. At 21:15 PDT on Oct 24 all 18 agents then running (two Mac fleets, live,
Maincloud) returned 200. The response names only the exception class; the agent log shows the
SQL status.
