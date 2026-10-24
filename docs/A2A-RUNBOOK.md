# A2A implementation and local runbook

As of October 24, 2026, the first runnable nine-agent implementation is present. The services use
`a2a-sdk==1.2.3` and protocol 1.0. Fixture runs use real JSON-RPC transport and deterministic
model/call results. They do not make provider calls, contact counterparties, or demonstrate live
financial writes.

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
4. Set **OpenAI and Anthropic API keys and exact model IDs** in the ignored `.env` or a secret
   manager. Defaults assign OpenAI to Settlement, Billing, Profitability and Approvals;
   Anthropic to Audit, Verification, Documents, Outreach and Quality Review. Per-agent provider
   and model overrides are available. UI model pins must belong to the configured catalog.
5. Set distinct `A2A_TOKEN_<ID>` secrets for all nine services, `ROUTER` and `GATEWAY`. They are
   service credentials, not model-provider keys. Never put them in browser `NEXT_PUBLIC_*` config.
6. Start `make agents`, `make gateway`, `make router`, and `make review-worker` separately.
   The router consumes W1/W4 normalized business events. Missing case IDs/unclassified events
   go to `events:dead_letter`; failed A2A admissions remain pending for recovery. The review
   worker consumes W2's review outbox, not the business event stream.
7. Start the existing dashboard with `make web`. W2 subscriptions show actual task statuses,
   delegation edges, artifacts, holds, reviews and audit. A Grok-style conversational agent
   inbox and direct operator chat are a separate UI milestone.

The first live acceptance check should be a clean invoice with a different amount, followed by
an exception, a missing document and a service restart. Confirm the W2 case/source rows exist,
policy checks and audit projections succeed, and no simulated provider is selected. The current
active-hold rule blocks all payment proposals on that invoice; partial undisputed payment is
not implemented as an exception.

## Enable a real demo phone call

After model/database checks pass, provide `CARTESIA_API_KEY`, `CARTESIA_FROM_NUMBER_ID`, and the
hosted agent IDs in `.env.example`. Configure a public HTTPS `PUBLIC_BASE_URL`, a webhook secret,
and a real teammate number in `DEMO_ALLOWLIST_PHONES`. Keep `OUTREACH_MODE=demo`.

Configure Cartesia completion callbacks to `/voice/webhook` with the `x-webhook-secret` header.
Configure in-call tool requests with `x-call-token` from the call's `call_token` dynamic variable,
and include `attempt_id` and `case_id` in their JSON body. Hosted personas must disclose that
they are automated, follow the requested purpose, and use only the allowed tool endpoints.
The gateway validates the capability, fetches authoritative call state from Cartesia, redacts
transcripts, freezes the result before the W2 write, and resumes the original Voice Desk task.
It also polls known calls if a callback is missed. `GATEWAY_API_TOKEN` protects explanation reads.

A timed-out create request remains **uncertain**. Do not automatically redial: reconcile the
provider call first. Calls outside the configured contact time window fail. A spoken statement
can be reported with an exact supporting quote but does not provide a receipt or authorize a
financial mutation. Inbound dispute intake and email delivery still require W4 integration.

## Remaining implementation and acceptance work

The full target and dependency order remain in the private A2A implementation plan. This pass
ships bounded, code-directed workflows and structured model assessments. It does **not** yet
ship the complete target described in PRD v1.4. Remaining work includes:

- Live Postgres/W2/provider/Cartesia acceptance and a two-host demonstration; fixtures cannot
  establish those integrations. W4 live source adapters, email replies, inbound intake and
  arbitrary scenario normalization need their owning workstream. Demo injection endpoints
  return 503 until connected instead of reporting false success.
- A model-selected investigative tool loop, durable prompt/config snapshots before inference,
  sandboxed document/code execution, and the telemetry collector. Current workflows use
  deterministic source readers and bounded model output; no arbitrary code tool is exposed.
- Comprehensive external-wait expiry/dead-letter handling, priority scheduling, versioned SQL
  migrations and deployment packaging. Task projections now use a durable ordered outbox;
  telemetry and other planned delivery channels still need their own integration.
- Atomic expected-base-version checks for instruction publication. Validator applies scoped
  memory, proposes validated instruction diffs, and supports reconstruction/reverts; it leaves
  automatic instruction publication pending that reducer contract. Policy and permissions are
  immutable to learned instructions. Corrective task lineage is capped at one rerun.
- The conversational agent workspace and operator messaging, broader audit/trace telemetry,
  and live learning/precedent replay acceptance. Existing dashboard network/activity is wired
  to real W2 updates when W2 is enabled.

No API keys are committed. Tests use synthetic secrets and mock HTTP responses; live readiness
and acceptance must be checked separately with the configured accounts.
