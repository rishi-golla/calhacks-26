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
