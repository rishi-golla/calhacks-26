# A2A implementation and local runbook

As of October 24, 2026, the first runnable nine-agent implementation is present. The services use
`a2a-sdk==1.2.3` and protocol 1.0. Fixture runs use real JSON-RPC transport and deterministic
model/call results. They do not make provider calls, contact counterparties, or demonstrate live
financial writes.

The dashboard now opens to the Agent inbox: a nine-agent roster with a shared team thread and
per-agent threads. Live entries use W2 task, delegation, audit, call and review records. An
optional recorded fixture replay lets you explore the inbox before configuring live services.
The current sponsor milestone connects Apify public-data tools and configures Cartesia voice.
CRS and Nessie are deferred. The local Apify A2A check is complete; live phone-call acceptance
is still pending.

## Service roster

| Display name | Service ID | Port | Main responsibility |
|---|---|---|---|
| Carrier Settlement | carrier_ap | 9101 | Invoice matching, duplicates, holds and payment proposals |
| Accessorial Audit | risk | 9104 | Contractual extras, receipts, dwell and suspicious charges |
| Carrier Trust & Verification | verification | 9106 | Verified directory, authority/insurance and independent callback |
| POD & Document Chase | documents | 9109 | Missing/signed/load-matched document packets and continuation |
| Shipper Billing & Collections | shipper_ar | 9102 | Receivables, short payments, collections and disputes |
| Load Profitability | cfo | 9103 | Load margin, charge recovery and cash forecasts |
| Settlement Approvals | controller | 9105 | Separate approval authority, holds and balanced journals |
| Dispatcher Outreach | voice_desk | 9107 | Purpose-scoped, demo-routed calls and transcript findings |
| Settlement Quality Review | validator | 9108 | Recomputations, flags, scoped memory and bounded corrections |

The first six rows are frontline specialists; the final three support their work. A case invokes
only the roles it needs. Existing IDs, skills and ports remain compatible; Documents is the new
service. Amounts and case IDs come from source records. The hero fixture is not a production rule.

## Run without credentials

From the repository root, install Python 3.12 dependencies using `make setup`. Then:

```bash
cp .env.example .env
set -a
source .env
set +a
make agents
```

In another terminal, load the same `.env` and run `make demo`. It sends a fixture case through
Carrier Settlement and its required peers. Run `make lint test` for the full hermetic suite,
including isolated nine-process integration, replay, document continuation, runtime recovery,
provider contract tests and call security. The source fixture includes suspicious, clean and
short-payment cases with different amounts.

Fixture mode binds to loopback and stores task history under `data/a2a/`. Reusing the same
idempotency key returns the same task. Use a new logical request key to deliberately create a
new task. Do not delete runtime data to retry a live action.

Each agent exposes `/healthz`, `/readyz`, `/.well-known/agent-card.json`, and JSON-RPC at `/`.
`AGENT_URL_<ID>` changes the advertised destination; `AGENT_PORT_<ID>` changes the bind port.
Service IDs are uppercased for environment variable suffixes.

## Explore the Agent inbox

Run `make web` and open the dashboard. Agent inbox is the default view. Select a specialist or
the team thread, search the recorded activity, filter by case, and open a message's evidence
inspector to see task, source and policy context. Read markers are local to this browser. The
feed shows actual handoffs, published results and model-selected tool calls. A tool entry follows its
lifecycle from running through completion, waiting or failure. Its inspector shows sanitized arguments,
results, model and duration; private model reasoning is never displayed.
Synthetic business context is labeled **Demo case**. Successful real Apify results carry a separate
**Live external evidence** label, and the inspector distinguishes case data from tool evidence.

The labeled replay uses `web/lib/agent-inbox-replay.json`, captured by launching all nine A2A
services in fixture mode. It includes 11 tasks and six recorded handoffs across the invoice
exception and short-payment cases. Playback performs no live actions. To regenerate it:

```bash
.venv/bin/python -m scripts.capture_inbox_replay
```

The script uses temporary ports and storage, no API keys, and sanitized final artifacts plus
persisted SDK status timestamps. The replay does not assert that agents are currently online.
For actual activity, connect W2 and run the services as below. Each runtime emits a heartbeat
every 15 seconds when W2 is enabled; the UI marks a heartbeat stale after 60 seconds.

This milestone is an observation inbox. Direct operator messaging and free-form task dispatch
remain future work; the inbox does not show a nonfunctional chat composer.

## Verified local live concurrency

The October 24 local acceptance run `live-002` completed nine root tasks with real OpenAI
inference, authenticated A2A 1.0 JSON-RPC, PostgreSQL task persistence and live SpacetimeDB
subscriptions. Persisted SDK WORKING intervals overlap for all nine specialists; parked time
is excluded. Shipper Billing delegated to Load Profitability and Accessorial Audit delegated to
Carrier Trust & Verification. The Billing parent parked and resumed after its child completed.
All nine returned OpenAI artifacts. No call, payment proposal, hold, journal request or ledger
entry was added. Forecasts, document reads, review records and task activity were real writes.

This uses the explicitly synthetic source case `SYN-A2A-CONCURRENCY`, titled
`[Live synthetic] Nine-agent A2A concurrency check`. The inbox distinguishes synthetic inputs
from live model inference and shows current online/working counts. This establishes concurrency
and local integration, not business-data correctness, financial execution or live telephony.
Dispatcher Outreach analyzes a supplied synthetic inbound transcript; it does not place a call.

The configured local stack is:

- SpacetimeDB: `http://127.0.0.1:3000`, database `freightops-a2a-live`.
- PostgreSQL: Docker container `freightops-a2a-live-postgres`, loopback port `55432`, persistent
  volume `freightops-a2a-live-pgdata`.
- Nine services: loopback ports `9101`–`9109`; dashboard: `http://127.0.0.1:3001`.
- Model key in `.env`; private service identities in `data/w2/freightops-a2a-live/services.env`;
  runtime connection settings and distinct A2A tokens in `data/live/runtime.env`.

While these services are running, execute one fresh bounded workload with:

```bash
make live-demo
```

Each run spends model API credits and saves an acceptance report under `data/live/`. A new run
ID creates new tasks; reusing an ID retrieves the same idempotent tasks. The first passing report
is `data/live/acceptance-live-002.json`. Failed earlier attempts remain visible in the audit trail.

To restart the configured stack, start the Postgres container with
`docker start freightops-a2a-live-postgres`, then run `make spacetime-start`, `make live-agents`,
`make live-gateway`, and `make web` in separate terminals. `make live-agents` reads the private local profile without
executing shell expressions and starts all nine processes; Ctrl-C stops those processes. Logs
are in `data/live/logs/`. Starting services submits no new work, but recovery can resume
unfinished admitted tasks and invoke models. The browser receives
only database coordinates, never provider or service credentials.

## Connect live infrastructure

1. Start Postgres/Redis (`make infra`) and provision W2 using `spacetime/README.md`. Publish the
   updated module and provision the new Documents identity before using it. Load the generated
   `SPACETIME_TOKEN_<ID>` credentials into each service environment.
2. Provide a trusted W4 source export through `SOURCE_DATA_PATH`: a JSON object with `cases`,
   `companies`, and `contacts`. Cases are keyed by resolved `case_id`; contacts are keyed by
   company ID and contain verified directory provenance. The bundled fixture is only for an
   explicitly simulated run. Live mode never promotes task-supplied financial values to facts.
3. Set `FREIGHTOPS_MODE=live`, `SPACETIME_DISABLED=0`, and `POSTGRES_URL` (asyncpg SQLAlchemy URL).
   All agents, gateway and review worker share this durable database. Local SQLite is for
   fixtures/tests. Schema creation is automatic; versioned production migrations remain a
   deployment follow-up.
4. Set **`OPENAI_API_KEY`** in the ignored `.env` or a secret manager. All nine agents default
   to OpenAI; an Anthropic key is optional. The starter configuration selects `gpt-6-luna`
   through `OPENAI_MODEL` and `gpt-6.1-sol` for Settlement Quality Review through
   `AGENT_MODEL_VALIDATOR`. This is a separate model review, not a cross-provider review.
   These choices follow the [OpenAI model catalog](https://developers.openai.com/api/docs/models);
   account access and structured-output acceptance still need a successful provider check.
   The model-selected live tool loop currently supports OpenAI only. The standalone assessment
   adapter supports Anthropic, but an Anthropic override is rejected by live service readiness
   until its tool-loop adapter is implemented. UI model pins must belong to the
   configured catalog. Keep keys out of chat and browser-visible variables. Adding a key
   does not switch `FREIGHTOPS_MODE=fixture` to live or satisfy the database prerequisites.
5. Set distinct `A2A_TOKEN_<ID>` secrets for all nine services, `ROUTER` and `GATEWAY`. They are
   service credentials, not model-provider keys. Never put them in browser `NEXT_PUBLIC_*` config.
6. Start `make agents`, `make gateway`, `make router`, and `make review-worker` separately.
   The router consumes W1/W4 normalized business events. Missing case IDs/unclassified events
   go to `events:dead_letter`; failed A2A admissions remain pending for recovery. The review
   worker consumes W2's review outbox, not the business event stream.
7. Start the dashboard with `make web`. The Agent inbox subscribes to actual task statuses,
   handoffs, artifacts, audit, redacted calls and reviews; the other operational views remain
   available. Live service configuration and provider acceptance are separate from replay playback.

The first live acceptance check should be a clean invoice with a different amount, followed by
an exception, a missing document and a service restart. Confirm the W2 case/source rows exist,
policy checks and audit projections succeed, and no simulated provider is selected. The current
active-hold rule blocks all payment proposals on that invoice; partial undisputed payment is
not implemented as an exception.

## Enable a real demo phone call

The current account has three FreightOps hosted personas—verification, collections and approvals—
and one Cartesia-provided outbound number. Their IDs and the operator's team destination are saved
only in private local configuration. This provisioning did not place a phone call. The calling
window was closed at setup, so the first end-to-end team call remains pending.

Required configuration is `CARTESIA_API_KEY`, `CARTESIA_FROM_NUMBER_ID`, the relevant
`CARTESIA_AGENT_ID_<PURPOSE>`, and a real E.164 teammate destination in `DEMO_ALLOWLIST_PHONES`.
The example phone numbers are rejected. Keep `OUTREACH_MODE=demo`; a public scraped number is
never used as the routed destination. Calling hours, verified-directory provenance, purpose
permissions, W2 controls and the per-counterparty rate limit still apply.

The default completion path is **local gateway polling**. With
`CARTESIA_WEBHOOK_TOOLS_ENABLED=0`, neither a public URL nor a webhook secret is required.
Start the poller in a separate terminal and check configuration without dialing:

```bash
make live-gateway
# In another terminal:
.venv/bin/python -m scripts.setup_cartesia
.venv/bin/python -m scripts.live_sponsors check-voice
```

`setup_cartesia` without flags only inspects account resources. On a fresh account, the explicit
`--apply --provision-number` flags create missing named personas/number resources and save their
IDs locally; they never place a call. The helper reuses matching resources and rejects ambiguous
duplicates. The current local account is already provisioned.

Once the intended team test and calling window are ready, this command creates an actual A2A
verification task, delegates to Dispatcher Outreach and can ring the allowlisted team phone:

```bash
.venv/bin/python -m scripts.live_sponsors call --run-id team-call-001
```

The script checks readiness and calling hours before admission. Do not widen the calling window
or change the recipient merely to make a test pass. A new run ID creates a new logical request;
reusing the same ID retrieves the same durable task. Reports are private files under `data/live/`.

Hosted webhook tools are optional and are not attached to the current personas. To enable them,
configure genuine public HTTPS `PUBLIC_BASE_URL` and `CARTESIA_WEBHOOK_SECRET`, attach the scoped
tools to the hosted personas, and then set `CARTESIA_WEBHOOK_TOOLS_ENABLED=1`. Completion callbacks
use `/voice/webhook` with `x-webhook-secret`. In-call tool requests use `x-call-token` from the
call's dynamic variables and include `attempt_id` and `case_id`. The gateway checks the capability
and fetches authoritative Cartesia state; untrusted webhook payloads cannot finalize a call.

Both paths redact transcripts, persist the final result before the W2 write, and resume the
original Voice Desk task. Personas disclose that they are AI assistants, follow the requested
purpose, end on stop/voicemail, and cannot authorize payments or bank changes.

A timed-out create request remains **uncertain**. Do not automatically redial: reconcile the
provider call first. Calls outside the configured contact time window fail. A spoken statement
can be reported with an exact supporting quote but does not provide a receipt or authorize a
financial mutation. Inbound dispute intake and email delivery still require W4 integration.

## Use real Apify evidence

Set `APIFY_TOKEN` in the ignored `.env`. The reviewed defaults are
`humble-echidna/fmcsa-carrier-check` build `0.1.2` and
`vdrmota/contact-info-scraper` build `0.2.242`. Other actor schemas require a reviewed adapter;
the model cannot choose an actor, URL or registration identifier. Current-case source records
supply `companies[carrier_id].usdot` or `mc` for carrier lookup and the relevant company's
`website` or `website_url` for a website check. Public HTTPS targets are validated before submission.

`lookup_carrier_authority` is available to Accessorial Audit and Carrier Trust & Verification.
`inspect_company_website` is available to Carrier Trust & Verification, POD & Document Chase,
and Shipper Billing & Collections. Carrier observations must match the supplied identifier;
company observations remain tied to the ingested website. Results include actor/run provenance,
source URLs, timestamps and evidence references. They do not overwrite the source directory.
Scraped phones/emails are unverified candidates, never approved callback or financial targets.
Failed, mismatched or negative carrier checks prevent a verified/clear screening outcome.

Each carrier run requests at most one result with a $0.05 charge cap; each company run crawls
one page with a $0.50 cap. Runs have a 60-second provider timeout and bounded local polling.
Durable run-start records prevent blind resubmission after ambiguous timeouts. A 24-hour cache
avoids repeated paid runs for the same reviewed actor/input. Pending lookups can be polled by a
later tool call without submitting a second run; completed task observations stay frozen.
These are configured bounds, not a statement about the provider's current prices.

For a fresh local sponsor demonstration, prepare two labeled cases and private source data,
then restart the live agents and gateway so they read the updated source profile:

```bash
.venv/bin/python -m scripts.live_sponsors prepare
# Restart make live-agents and make live-gateway in their own terminals.
.venv/bin/python -m scripts.live_sponsors lookups --run-id sponsors-next
```

`prepare` writes the source overlay, updates local source-path configuration, and seeds case/company
metadata plus the configured team demo target. It performs no external lookup or phone call.
The overlay retains the original concurrency case, so `make live-demo` still works.
The `lookups` action requests public Apify checks over authenticated A2A and does not request outreach.
The business scenario stays labeled synthetic even though the fetched public records are real.

The completed `sponsors-002` task finished all four tool calls without an assessment error and
returned a real FMCSA observation and a real company-website observation, each with
`source_simulated=false`. It reused the actor runs from `sponsors-001`, refreshing cached records
with normalization v2 without starting a new paid run. The private report is
`data/live/sponsor-lookups-sponsors-002.json`; inspect the corresponding tool calls in the inbox.
Public observations remain subject to independent identity, insurance, financial and contact
authorization checks. Validation passed 414 backend tests and 41 frontend tests; lint and the
frontend production build passed. Cartesia call acceptance is separate and remains pending.

## Model-selected tools

Live mode routes all nine handlers through `common/tool_loop.py`. Each role receives only the
functions in its `common/tool_registry.py` registry. Models choose investigative tools and may
delegate to allowed peers through the existing A2A runtime. The task-bound workflow action reuses
the source validation, policy, approval, hold, outreach routing and idempotency controls already
implemented by that role. The model's summary cannot replace the workflow's business outcome.

The registries expose **22 distinct tools across up to 68 role grants**. Shipper credit-review tasks omit optional A2A delegation. Counts include the three shared
tools: `read_load_record`, `integration_availability`, and `execute_current_skill`.

| Brokerage agent | Tool count | Additional role tools |
| --- | ---: | --- |
| Carrier Settlement | 8 | Invoice matching, duplicate detection, packet inspection, settlement state, accessorial audit |
| Shipper Billing & Collections | 10 | Deposit matching, ingested credit evidence, journal inspection, verified directory, website inspection, outreach readiness, specialist A2A delegation |
| Load Profitability | 7 | Margin calculation, cash exposure, settlement state, specialist A2A delegation |
| Accessorial Audit | 9 | Charge audit, duplicate detection, packet inspection, verification evidence, Apify carrier lookup, specialist A2A delegation |
| Settlement Approvals | 7 | Settlement state, journal inspection, charge audit, specialist A2A delegation |
| Carrier Trust & Verification | 9 | Verified directory, ingested verification evidence, packet inspection, Apify carrier lookup, website inspection, outreach readiness |
| Dispatcher Outreach | 6 | Verified directory, redacted ingested inbound transcript, outreach readiness |
| Settlement Quality Review | 5 | Persisted review target/history, independent quality checks |
| POD & Document Chase | 7 | Packet inspection, verified directory, website inspection, outreach readiness |

Tool schemas reject unknown fields and are bound to the current case. There is no arbitrary SQL,
shell, URL-fetch or unrestricted payment tool. Provider turns are checkpointed before tools run,
and completed tool results are checkpointed before the next model turn. Park/resume reuses the
selected call. A task is limited to 12 model turns and 10 tool calls, within its existing deadline.
Once the guarded workflow completes, tools are closed and the model can only summarize.
Existing idempotent reducers protect writes across an interrupted execution. Tool telemetry
shares the durable activity outbox, so a transient dashboard outage cannot erase a completed
outcome or turn a parked dependency into a failed task. Failed final narration preserves the
domain result and is shown as a summary note.
The Responses API runs with `store: false`; encrypted reasoning continuation stays in private
runtime storage, outside inbox telemetry. Audit events include bounded, sanitized arguments/results.

The OpenAI key covers model execution; Apify and Cartesia use their own configured credentials.
External tools report unavailable configuration explicitly and never substitute W4 mocks.
`inspect_outreach_readiness` reports Cartesia setup without dialing. Source-directory reads remain
reads of ingested data, while the separate Apify tools produce cited public observations.
CRS and Nessie are deferred and require no keys for this milestone. Other contact enrichment,
email delivery and document OCR remain unimplemented.

`make live-demo` now requires paired started/completed tool events for every root agent as well
as nine-way concurrency, live provider results and A2A handoffs. It only uses the labeled synthetic
source case and rejects a run that adds calls, payment proposals, holds, journal requests or ledger entries.

The final local tool-loop acceptance, `tools-002`, completed all nine roots with **38 completed
model-selected tool calls**, two A2A handoffs, nine distinct agents concurrently working, and no
task or final-summary failures. The report is `data/live/acceptance-tools-002.json`. No restricted
financial writes or calls were added. The inbox was verified against the live database, including
the tool inspector's arguments, outputs, model, duration and lifecycle. Hermetic validation passed
304 Python tests and 40 frontend tests, plus Python/frontend lint and the production web build.

## Remaining implementation and acceptance work

The full target and dependency order remain in the private A2A implementation plan. This pass
ships bounded model-selected tools backed by guarded workflows. It does **not** yet
ship the complete target described in PRD v1.9. Remaining work includes:

- Live financial/Cartesia acceptance and a two-host demonstration. Local Postgres/W2/provider
  concurrency and Apify public-data integration are verified with synthetic business scenarios.
  Full business-document ingestion, additional source adapters, email replies, inbound intake and
  arbitrary scenario normalization need their owning workstream. Demo injection endpoints
  return 503 until connected instead of reporting false success.
- Sandboxed document/code execution and the external telemetry collector. Current tools use
  scoped source readers and bounded model output; no arbitrary code tool is exposed.
- Comprehensive external-wait expiry/dead-letter handling, priority scheduling, versioned SQL
  migrations and deployment packaging. Task projections now use a durable ordered outbox;
  external telemetry and other planned delivery channels still need their own integration.
- Broader multi-deposit reconciliation: the existing AR workflow creates a child per posted
  deposit plus a CFO child, so more than two deposit children exceed the current three-child
  limit. Optional investigative delegations must not consume slots reserved for mandatory work.
- Atomic expected-base-version checks for instruction publication. Validator applies scoped
  memory, proposes validated instruction diffs, and supports reconstruction/reverts; it leaves
  automatic instruction publication pending that reducer contract. Policy and permissions are
  immutable to learned instructions. Corrective task lineage is capped at one rerun.
- Direct operator-to-agent messaging, broader audit/trace telemetry, and live learning/precedent
  acceptance. The observation inbox and dashboard network/activity are wired to W2 records;
  their recorded fixture replay does not establish live integration acceptance.

No API keys are committed. Tests use synthetic secrets and mock HTTP responses; live readiness
and acceptance must be checked separately with the configured accounts.
