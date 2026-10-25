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

**Status: published, provisioned and verified on Maincloud on Oct 24, 2026 (22:00–22:20 PDT);
the ASUS-driven checks remain.** The module was published from `main` twice with
`--delete-data=never` (first 9bc5aef, then 0777544 after PR #25 merged; 47 module tests and a
clean build each time). Row counts were identical before and after each publish (367 cases,
206 invoices, 418 companies); `source_record` and the three new reducers appeared.
`spacetime_cloud provision` reused the stored identities, found no missing `agent_config` row and
opened 2028-01 to 2028-04 on top of the 15 periods already open. The Mac stack (main checkout,
:9101–9109, gateway :9000, dispatch worker) was relaunched at 05:14 UTC through `load_live_env`
with `SOURCE_DATA_BACKEND=spacetime`, `FIRST_HOP_TRANSPORT=spacetime`, `SOURCE_DATA_PATH` unset
and `HUMAN_APPROVER_ID` set. The witty-raven fleet was repointed to a local database.

**First real first hop on Maincloud, without the ASUS.** For the open case
`CASE-LD-1025-1FF2EA39` its genuine source records (case, carrier and shipper company and
contacts, `dataset:meta`, taken from the ingest export that opened it) were written as `ingest`,
then `enqueue_first_hop` was called as `router` (`EVT-CUTOVER-1792905482`, P2). The outbox row
went pending → processing → done in one attempt and the dispatch row queued → accepted
**3.4 s after enqueue** (module timestamps; 1.2 s locally, the difference being the worker's
cloud round trips). Carrier Settlement ran live: `openai/gpt-6-luna`, six tool calls beginning
with `read_load_record` served from `source_record`, 15 audit rows, outcome `approve` at 0.8,
completed 22 s after acceptance. The worker then delivered the resulting `review_output` to the
Validator. No 530s and no `SourceUnavailable` occurred. Cosmetic verifier bug: the chain line
"task … completed" prints the task's `started_at`, not its completion time.

`make cloud-verify` (`scripts/cloud_verify.py`) prints the checklist from anonymous SELECTs
and exits 1 when a check fails; `--readyz` adds the agents' readiness from the Mac.

| Check | How | Maincloud result |
|---|---|---|
| `agent_config` has nine rows including `documents` | `cloud-verify` | PASS, 9/9 (Oct 24 22:17 PDT) |
| nine `agent` rows, Mac `card_url`, `last_seen` advancing (15 s heartbeat) | `cloud-verify` (two reads 20 s apart; flags a card that moves between fleets) | PASS, 9/9 at 127.0.0.1:9101–9109, all seen within 45 s |
| accounting periods: current month plus 18 open | `cloud-verify` | PASS, 19/19 (2026-10 to 2028-04) |
| a new simulator load yields `source_record` → `case` → `dispatch` queued → accepted → `task` / `delegation_edge`, in that order | `cloud-verify` case chain (newest routed case, or `--case`) | PASS for the synthetic first hop `--case CASE-LD-1025-1FF2EA39` (source records, enqueue, accepted +3.4 s, task, outbox done); the newest *simulator* case still shows direct-A2A dispatches and no source record until the ASUS ingest and router restart on `main` |
| no `first_hop` row older than 60 s while the worker is up | `cloud-verify` | PASS, 0 |
| outbox backlog by kind with `last_error` | `cloud-verify` | 170 not done: 169 `review_followup` pending + 1 processing, all `SourceUnavailable` (tasks from other hosts); inert under the worker's exhaustion rule |
| dashboard on Maincloud shows the case within seconds | browser on `web/.env.local` pointed at Maincloud | not yet run |
| every agent `/readyz` is 200 | `cloud-verify --readyz` on the Mac | PASS, 9/9 "200 live" |
| enqueue-to-accept latency over at least 20 events | `cloud-verify` latency line (outbox `created_at` to accepted `dispatch.ts`, both module clock) | 1 event so far: 3.41 s; the 20-event figure waits for ASUS traffic |
| 30-minute soak at two events per second (PRD) | simulator at that rate, then `cloud-verify --latency-window 30` | not yet run |

Cloud SQL round trips during these runs: 97–152 ms median, 259–596 ms max, from the Cal Hacks
network.

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
