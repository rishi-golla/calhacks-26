# PRD: Autonomous Back Office for Freight Brokerages
**Cal Hacks 13.0 · Ground truth v1.10 · Sat Oct 24, 2026 PDT**

> This is the one source of truth. If code, a chat message or a slide disagrees with this doc, the doc wins until someone changes it. To change it, edit this file, bump the version in the change log at the bottom, and post one line in the team chat. Section 7 (interface contracts) is frozen at **Sat 3:00 AM**. After that, contract changes need agreement from both owners on that interface.

---

## 1. Summary

**One line:** an autonomous finance back office for a mid-size freight brokerage. An intake and first-hop routing harness on an ASUS AI supercomputer dispatches incoming work to independent finance agents. Those agents call their configured model provider (OpenAI by default, Anthropic optional), delegate to each other over the A2A protocol, phone carriers and shippers with voice agents, and act on the books only through deterministic controls, recording every decision in an audit trail.

**Product boundary (v1.4):** freight brokerage back-office work from delivery paperwork to carrier settlement and shipper collections. Carrier sourcing, rate negotiation, dispatch, and live shipment tracking are outside this implementation. Six frontline specialists and three supporting specialists run as nine independent A2A services. Existing service IDs remain stable; `documents` is the new service on :9109. Generic role names elsewhere in this document (AP, AR, Risk, Controller, CFO, Verification, Voice Desk, Validator) refer to the matching stable IDs in section 8.

**Implementation status (October 24):** the first runnable nine-service A2A milestone is implemented,
including durable admission/checkpoints, direct provider adapters, brokerage workflows, document
continuation, call recovery, review delivery and scoped memory. The Agent inbox is now the default
dashboard view: nine specialists, team and per-agent threads from recorded W2 activity, case/search
filters, evidence inspection, local read markers and a clearly labeled capture replay. Agent
runtimes emit a heartbeat every 15 seconds when W2 is enabled. A local live acceptance run now
verifies all nine agents concurrently using actual OpenAI calls, authenticated A2A handoffs,
PostgreSQL task persistence and live SpacetimeDB projections, with explicitly synthetic source
data. The two delegated tasks completed, and a parked parent resumed successfully. No calls or
financial actions were requested. Each live agent now has a role-specific tool registry and a bounded,
durable model-selected tool loop. Tool activity appears in the inbox with arguments, results and status;
existing policy-controlled workflows remain responsible for financial authorization and execution.
Apify carrier and company-website lookups now return real public evidence through model-selected
tools. Cartesia account resources and the local completion poller are configured; a live phone call
has not yet been accepted end to end. Financial/Cartesia acceptance, full source ingestion, direct operator messaging,
sandbox/telemetry/deployment hardening, and automatic instruction
publication remain follow-ups. The inbox observes actual messages and results; it does not fabricate
dialogue or provide arbitrary chat dispatch. This PRD describes the full target; see
[A2A runbook](A2A-RUNBOOK.md) for current behavior and setup.

**Provider setup (v1.6):** one OpenAI key supports all nine agents. The starter configuration
uses `gpt-6-luna` for operational assessments and `gpt-6.1-sol` for Settlement Quality Review.
The standalone assessment adapter also supports Anthropic; the v1.8 live tool loop currently
requires OpenAI. A separate reviewer model is not cross-provider validation. Model choices
remain subject to account access and live acceptance; credentials alone do not enable live mode.

**Local acceptance (v1.7):** `live-002` completed all nine root tasks plus two delegated tasks and
an actual reviewer warm-up task. Persisted SDK WORKING intervals confirm nine distinct agents
active concurrently; parked intervals are excluded. The inbox subscribes to this live local
database and labels its synthetic source case. `make live-demo` repeats a bounded run and saves
its measured acceptance report. This verifies service concurrency and transport; it does not
establish business-data correctness or complete the cross-host/financial/telephony acceptance.

**Agent tools (v1.8):** all nine live services use model-selected tools for case evidence, role-specific
checks, scoped database reads, A2A delegation and task-bound guarded workflows. Provider turns and tool
results are checkpointed so a parked task resumes its selected call. Models cannot supply arbitrary SQL,
shell commands, payment amounts or contact destinations. Final business outcomes come from validated
workflow artifacts. SpacetimeDB tool audit events power the inbox's running/completed/failed/waiting
states and inspection view. The existing OpenAI credential covers these tools. Live external lookups,
email, sandbox banking and voice calls require their respective adapters/configuration; mocked W4
integration results are not exposed as live tools. Fixture workflows remain deterministic.
Local acceptance `tools-002` verified 38 completed tool calls across all nine concurrent agents,
two A2A handoffs and no task/summary failures, with no calls or restricted financial writes added.

**Sponsor implementation (v1.9):** focus is Cartesia and Apify; CRS and Nessie are deferred.
The tool catalog now contains 22 distinct tools across up to 68 role grants. Apify lookups use
case-bound USDOT/MC identifiers or an ingested public company URL, bounded actor runs, durable
start tracking, a 24-hour cache and source citations. Public records and scraped contacts remain
external observations and never become outreach or financial authorization automatically. The
`sponsors-002` A2A task completed with four successful tool calls and real carrier and website
observations in a labeled synthetic business scenario. It reused the original actor runs from
`sponsors-001` with normalization v2, without starting new paid runs. The private acceptance report
is `data/live/sponsor-lookups-sponsors-002.json`. Validation passed 414 backend tests and 41 frontend
tests, with lint and the frontend production build passing. Cartesia has three hosted personas (verification, collections, approvals) and
one provided outbound number configured. Its default completion path is the local gateway poller;
public HTTPS and webhook credentials are only needed when hosted webhook tools are enabled.
The configured calling window is still enforced. No phone call has been placed in this milestone;
the first team demonstration remains pending a suitable calling window and operator agreement.

**Operator phone demo (v1.10):** the Demo Console includes **Call my phone** for the fixed,
verified team destination. Each explicit click enters the real Verification → Dispatcher Outreach
A2A workflow. A private, task-bound authorization lets this operator-triggered team demo bypass
calling hours and the one-attempt-per-hour limit. Back-to-back demos are supported after the
previous call and its agent processing finish; only one request can be active. Directory checks,
demo routing and purpose permissions still apply. Ordinary calls and the CLI keep both guards.
The UI polls actual state, masks the destination and saves a request ID before submission so
retries/reloads cannot create another call. Model input cannot grant the exception. Server secrets
stay outside the browser. Mocked coverage verifies this control; no actual call was placed during
implementation, and the operator will trigger the first live test. Other demo presets remain
unconnected.

**Seats it fills (the line the Hyphenate judges ask for):** *"The carrier-payables clerk, payables fraud analyst and collections specialist at a 40-person freight brokerage, reporting to the controller."*

**Problem:** freight brokers lose money on carrier invoice padding (detention and lumper charges without proof), double-brokering, bank-change fraud and slow collections. The evidence is spread across the TMS, rate cons, PODs, GPS/ELD, email and the bank, and the sources disagree. Today humans reconcile all of it by hand.

**Target demo characteristics (current acceptance is recorded above):**
- Nine independently running brokerage-specialist services that talk over the real A2A protocol: Agent Cards, task IDs, artifacts, and tasks that park and resume.
- A continuous synthetic company with planted errors and an answer key, so we can prove accuracy.
- Our own ASUS harness classifies incoming events and dispatches the first A2A task. Independent agents then execute with their own provider adapters and scoped tools; subsequent delegations go directly between agents.
- Real counterparties: real carriers and shippers found by scraping (Apify: FMCSA, company sites), with real contacts surfaced through an enrichment waterfall (FMCSA, our own contact-data key, context.dev, website scrape), shown unmasked. Realistic synthetic invoices are written against them.
- Cartesia provides live calls, but **outreach is demo-only**: the UI shows the intended counterparty, and calls ring an allowlisted teammate playing that person. The first live call remains pending acceptance. CRS credit and Nessie banking are deferred from the current implementation.
- A Validator agent reviews every output and every bit of friction, then updates each agent's instructions and persistent memory.
- A Git-style audit trail shown as spreadsheets: source document, which agent read it, what it did, and which cells changed.
- Live shared state in SpacetimeDB: every agent, task, hold and audit row is visible as it happens.

## 2. Goals and non-goals

**Goals (must ship):**
- G1: The hero case runs live end to end. Carrier Settlement delegates a suspicious invoice to Accessorial Audit. Missing delivery paperwork or receipts go to POD & Document Chase; identity or payment-detail questions go to Carrier Trust & Verification. Either specialist may use Dispatcher Outreach in demo mode. Results return to Carrier Settlement, which delegates the hold decision to Settlement Approvals and the financial impact through it to Load Profitability. Settlement Quality Review reviews financial outputs, and all work lands in the audit trail. A case uses only the specialists it needs; it does not require nine serial handoffs.
- G2: In the demo flow, the teammate's phone rings ≤ 15 s after the demo input.
- G3: 20+ unrelated cases are processed concurrently while one case waits on a call.
- G4: Kill any agent mid-case, restart it, and the case finishes with no duplicate work.
- G5: Every state change goes through deterministic policy checks. The requester can never be the approver.
- G6: A precision/recall score against the planted-error answer key is shown live.
- G7: A judge can inject their own scenario and walk it through the demo call flow.
- G8: **Real-world data:** every counterparty in the demo is a real company with real public and enriched contact data (phone, email, title, source, confidence) shown in the UI.
- G9: **Validator loop:** every financial output (AP, AR, Controller, CFO) is reviewed. Friction and human flags turn into versioned memory and instruction updates, visible as diffs.
- G10: **Audit as Git:** for any number, show the source document, the agent that read it, the action taken, and the before/after cells. A spreadsheet view supports blame, diff and history.
- G11: **Human input:** humans can flag any item, approve or reject, and comment, and flags route to the Validator.

**Non-goals:** contacting real people (all calls and emails go to team-controlled numbers and inboxes), real TMS or QuickBooks connectors (simulated behind the same tool interfaces), real money movement (sandbox only), multi-tenant, mobile app, auth beyond bearer tokens, model fine-tuning, Fetch.ai, Coinbase.

## 3. Prize targets

Current implementation focus is **Cartesia + Apify**, alongside the already connected SpacetimeDB.
Other product/security submission targets remain in the plan; CRS and Nessie are not required for
the current milestone. Prize descriptions below are planning notes, not claims of completed integrations.

| Tier | Track | What their judges need to see | Owner who makes it land |
|---|---|---|---|
| 1 | **Hyphenate** ($8k/$5k/$3k) | Real enough, would you hire it, something new, "the moment". A video that opens with the moment, a live demo (they may bring a scenario), a seats line, proof of what's real vs simulated | All; pitch by W4 |
| 1 | **Microsoft: Built for the Real World** | Customer understanding, measurable usefulness, path to production, responsible design, end to end | All; metrics by W4 |
| 1 | **SpacetimeDB** ($2,048/$1,024/$512) | SpacetimeDB as the *core* real-time backend for agent coordination and live dashboards | W2 |
| 1 | **Cartesia** | Voice core to the product: originality, use of Cartesia, execution and edge cases, responsibility (25% each). 2-min demo-first video, README with features used | W3 (Voice Desk) + W4 (personas) |
| 1 | **Apify** ($5k pool + $400 Best Actor) | Meaningful use: real company and carrier scraping (FMCSA, websites), plus our own published "freight counterparty" actor | W4 |
| deferred | **CRS** ($1k / $500 agent / fraud interview) | Original target: live credit/sanctions sandbox; outside the current milestone | W4 |
| deferred | **Capital One Nessie** ($300/person) | Original target: sandbox deposits/transfers; outside the current milestone | W4 |
| 1 | **Armadin security** (criteria TBD) | Isolated intake processing and agent tools, prompt-injection quarantine, SoD, tamper-evident audit | W1 + W3 |
| 2 | **Notability** | Real process notes from sketch to build | Everyone; start now |
| auto | **Cal Hacks: Most Technically Complex** | Comes from the main build | n/a |
| stretch | Bright Data: live public web data on the counterparties (site, news, reviews). It must be essential and public only; personal contacts come from the enrichment waterfall (section 6, W4 R2). Factory Droid also stretch. | Only if Tier 1 is done | W4 |
| out | Fetch.ai, Coinbase | Not doing them for now | n/a |

Check Devpost for a cap on how many tracks we can enter.

## 4. The demo (3 minutes, also the video)
1. **0:00, the moment (cold open):** the phone rings on stage. The Cartesia agent: "Hi, this is an AI assistant calling for Golden Gate Freight about load 4821. You billed two hours of detention, but our GPS shows your truck on site for 47 minutes. Can you send the receipt?" A teammate plays the carrier's dispatcher and hedges.
2. **0:30, real-world data:** the case panel shows a *real* carrier: FMCSA authority, USDOT/MC, the registered phone, the scraped website, and the dispatcher contact from the enrichment waterfall (name, title, phone, email, source, confidence), all unmasked. A banner reads: **"Outreach: DEMO MODE. Would dial +1 775 555 0112 (FMCSA-registered, verified). Routed to the team line."**
3. **0:50, the network (SpacetimeDB live):** delegations animate Carrier Settlement → Accessorial Audit → POD & Document Chase → Dispatcher Outreach, while unrelated cases keep flowing. Identity disputes use Carrier Trust & Verification as a sibling investigative path. Financial decisions follow a separate Carrier Settlement → Settlement Approvals → Load Profitability branch, keeping depth ≤3.
4. **1:05, resolution and Validator:** the call ends and the transcript becomes findings. Settlement Approvals holds the disputed $300. The $1,850 undisputed amount is shown as blocked while the invoice has an active hold; the demo never bypasses P-002 to propose or send it. The Validator re-checks the entry and the CFO projection, notices friction (AP had to re-request the rate con), and writes an AP memory note. The UI shows the memory or instruction diff.
5. **1:30, audit spreadsheet:** open the AP subledger grid and click the $300 hold cell. Blame shows the source invoice PDF (page 1, line 3), read by AP in task TASK-002, the change made by the Controller in change set `a1f3c9`, and policy P-001. The history tab shows every version of that cell.
6. **1:55, a human flag:** a judge flags a CFO forecast line as wrong. The Validator reviews the flag, updates CFO memory and re-runs the projection. The new change set appears with a diff.
7. **2:15, recovery:** `kill -9` Risk on a new case, restart it, and the case finishes.
8. **2:30, "never asks twice" and proof:** the same pattern resolves with no call and cites the precedent, then the scoreboard. "Want to try one? Type a scenario into the demo console."

## 5. System architecture

```text
W4 simulator / demo / judge input
              |
              v
Ingest -> Redis Streams (events)
              |
              v
W1 ASUS: intake + first-hop routing harness
  classify event -> select allowed skill/owner -> A2A SendMessage
              |
              v
W3 independent A2A services (separate deployment hosts)
  Carrier Settlement -> Accessorial Audit -> POD & Document Chase -> Dispatcher Outreach
  Carrier Trust & Verification / Shipper Billing & Collections
  Settlement Approvals / Load Profitability / Settlement Quality Review
       |                  |                  |
       v                  v                  v
  Provider adapters   Scoped tools       Cartesia gateway
  OpenAI live loop    Apify              (local polling;
  / other adapters    internal tools      HTTPS tools optional)
       |
       +-- Agent-owned inference and tool loops; direct A2A delegation

W1 dispatch records + W3 tasks / financial changes / audit
              |
              v
W2 SpacetimeDB reducers -> live dashboard subscriptions

Postgres: source records and durable A2A task history
W4 evaluator: answer key -> outcome metrics
```

**Execution boundary (decided in v1.3):** ASUS is the first layer. It consumes new business events, classifies ambiguous input, and dispatches the initial task to an allowed A2A service. After handoff, each agent owns its model calls and tool loop and delegates directly over A2A. Agent inference, downstream delegation, voice processing, and financial execution do not pass through ASUS. Model providers such as OpenAI and Anthropic Claude supply inference to the independently deployed agent services; the providers themselves are not A2A services.

**Where state lives (decided):**
| Data | System of record | Why |
|---|---|---|
| Simulated source systems (loads, contracts, rate cons, invoices, PODs, GPS pings, emails, carrier directory) | Postgres | Plays the role of the "existing systems" the agents read |
| A2A task store (task history, status, artifacts) | Postgres via the SDK's `DatabaseTaskStore` | The SDK supports it; survives restarts |
| Operational truth: cases, task graph, agent status, **ledger, holds, approvals, payments-proposed**, audit events, call status, precedents, metrics | **SpacetimeDB** | Transactional reducers enforce invariants next to the data, and the live UI subscribes. This makes SpacetimeDB core (for its track) |
| Event stream | Redis Streams | Ingest buffer, consumer groups, replay |

**v1.1 additions to the diagram:** a **Validator :9108** in the A2A mesh (it reviews every output, owns memory and instruction updates, and receives human flags), and a **W4 enrichment pipeline** (Apify scrape → enrichment waterfall: FMCSA, own key, context.dev → `company` and `contact` directory) that feeds Verification, the Voice Desk and the UI.

**Deployment:** ASUS hosts W1 intake/routing and any models used for intake classification only. W3 agent services and their tool runtimes run on separately configured hosts and call their configured model providers directly. Redis, Postgres, SpacetimeDB, the dashboard and gateway have independently configured addresses; none is assumed to live on ASUS. Cartesia completion uses the local `gateway:9000` poller by default. A public HTTPS tunnel is only needed for optional hosted webhook tools and callbacks. Already-dispatched A2A tasks must continue if ASUS is unavailable; new events remain durably queued.

## 6. Workstreams and owners

| ID | Workstream | Owner | Owns |
|---|---|---|---|
| W1 | **ASUS intake/routing harness** | _name_ | Redis event consumer, input classification, first-hop routing, dispatch retries/deduplication, intake isolation, routing telemetry; optional routing-policy experiments |
| W2 | **SpacetimeDB + dashboard** | _name_ | Module schema and reducers (ledger invariants), subscriptions, the Next.js dashboard (network graph, case tree, feeds, approvals inbox, scoreboard) |
| W3 | **A2A mesh** | _name_ | 9 agent services (six brokerage specialists plus approvals, outreach, and quality review), AgentRuntime, per-agent provider adapters and tool isolation, delegation and park/resume, review-outbox delivery, policy engine, audit/change-set writer, memory store, Voice Desk logic, webhook gateway |
| W4 | **Real-world data, enrichment & demo outreach** | _person 4_ | Apify public-data tools and Cartesia personas now; full enrichment/ingestion, realistic synthetic documents, simulator and answer key, demo console/email, pitch, video and Devpost remain broader work; CRS/Nessie deferred |

Rule: **each owner can build against mocks of the other three from hour 0.** The contracts in section 7 are what make that possible. Each owner ships a mock of their side by **Sat 3:00 AM** (a stub intake router that dispatches seeded events, a SpacetimeDB module with tables and no-op reducers, an agent that returns canned artifacts, a simulator that emits 10 fixed events).

### W1: ASUS intake and first-hop routing harness
**Purpose:** accept incoming business events and dispatch the first task to the correct independent A2A service. ASUS is the entry layer, not the execution host or inference proxy for the A2A mesh.

Requirements:
- **H1 Intake:** consume the Redis `events` stream using consumer group `router`. Validate the event schema, resolve source/evidence references, and use the existing `TaskEnvelope` and delegation metadata contracts without adding fields.
- **H2 Routing:** map known event types to the skill owner in section 7.3. Classify ambiguous email or scenario input into an allowlisted skill. The initial policy is deterministic for typed events; a classifier may assist ambiguous events. Record the selected skill, destination and reason. Do not choose a different financial role merely because its model is cheaper or less busy.
- **H3 Agent discovery:** resolve independently configured service URLs and validate their A2A Agent Cards. Check that the selected service advertises the required skill and is enabled. Provider/model configuration belongs to the destination agent, not the ASUS router.
- **H4 Dispatch:** send the first A2A task, with the event ID as a stable idempotency key and the case context attached. Persist the event-to-task mapping and acknowledge Redis only after durable acceptance. Reclaim abandoned deliveries and retry with backoff; the receiving runtime deduplicates repeated submissions. A lost response must not create duplicate work.
- **H5 Priority and backpressure:** reserve dispatch capacity for P0 demo events, bound in-flight submissions per destination, and expose queue depth and age. Work waiting for a destination stays queued without blocking unrelated destinations.
- **H6 Classification output:** validate classifier output against a schema and the skill allowlist. Unknown or low-confidence classifications go to a visible triage queue. Never invent a target, authorize payment, or bypass policy based on input text.
- **H7 Intake isolation:** any local document extraction or classification preprocessing runs with read-only source files, bounded CPU/memory/time and no network by default. Agent tool execution is owned and isolated by W3 on its execution hosts.
- **H8 Secrets:** ASUS holds only credentials required for event intake, first-hop A2A dispatch and routing telemetry, plus an intake-classifier credential if needed. Agent provider keys belong to the corresponding service deployment. Neither prompts nor sandbox jobs receive raw keys.
- **H9 Injection quarantine:** treat incoming document and email text as untrusted evidence. Classifiers may label the work but cannot rewrite routing permissions or financial controls. Downstream agents enforce the same boundary on their own inputs.
- **H10 Telemetry:** record event ID, case ID, selected skill/agent, dispatch task ID, routing policy version, reason, queue wait, classification latency and acceptance/retry status. Keep intake metrics separate from downstream provider/model usage. W1 and W2 agree any additive telemetry schema before implementation.
- **H11 Failure behavior:** if optional classification inference is unavailable, known event types still route deterministically; ambiguous work waits for triage. If ASUS stops, Redis retains incoming work while already-dispatched agents continue independently.

Done when: the hero event reaches AP over real A2A; re-delivering its event ID does not create a second task; a queued event survives a router restart; and AP -> Risk -> Verification continues after the ASUS router is stopped. Measure intake-to-acceptance latency separately from full case resolution and report measured throughput.

**Routing learning / RL (proposed, not a shipped requirement):** a contextual-bandit experiment may select among pre-approved intake classification strategies or equivalent deployments of the same skill. It must preserve required role/skill ownership. Inputs may include event type, classifier confidence and destination queue depth. Evaluate delayed outcomes against the simulator answer key, with explicit penalties for latency, cost and unnecessary downstream work. W1/W4 must define reward weights, feedback attribution, allowed actions and held-out replay evaluation before claiming RL results. Begin in shadow mode against fixed routing; do not let exploration affect payment permissions. This experiment is distinct from W3's Validator memory/instruction loop and does not route downstream model calls through ASUS.

### W2: SpacetimeDB + dashboard
**Purpose:** the live, transactional operational state of the company and the agents, and the UI that shows it.

Requirements:
- **S1 Module:** tables and reducers as in section 7.4, built on **SpacetimeDB 2.x**. Modules can be written in TypeScript (runs on V8), Rust, C# or C++. TypeScript is recommended so the module and the dashboard share types. The 2.0 API changed substantially, so trust the current docs over older examples and confirm the HTTP reducer-call endpoint in section 7.4 against them by C0. 2.x also has HTTP handlers and procedures, which the gateway could use.
- **S2 Ledger invariants in reducers:** `post_journal_entry` rejects unbalanced entries, entries in locked periods, and entries where `approved_by == requested_by`. `place_hold` and `release_hold` check amounts against the invoice. `release_hold` over $5,000 requires an `approvals` row with a human approver.
- **S3 Auth:** agents call reducers over HTTP with a service identity/token per agent. Humans use the dashboard identity. Reducers check the caller's role (for example, only `controller` may post journal entries).
- **S4 Dashboard (Next.js + React Flow + a spreadsheet grid component) views:**
  1. **Agent network:** 9 nodes with live status, active, queued and done counts, and an edge animation on every A2A delegation.
  2. **Live task feed:** newest first, filterable by agent or case.
  3. **Case drill-down:** the task tree, each task's artifacts, evidence, policy ids, model used, and call transcript and recording link.
  4. **Approvals inbox:** approve or reject with a one-time code; the decision goes to a reducer.
  5. **Ledger and holds:** journal entries, holds, cash position.
  6. **Scoreboard:** precision, recall, $ caught, false holds, median time to resolution, calls placed.
  7. **Harness panel:** ASUS intake queue, classification/dispatch latency, selected skill and routing reason, retries and accepted task IDs. Show agent provider/model configuration and inference telemetry separately, sourced from W3; provider changes take effect in the agent runtime.
  8. **Demo console:** W4 backend; see W4 R6.
  9. **Counterparty panel:** the real company, the FMCSA data, the enriched contacts with source and confidence, and the outreach-mode banner.
  10. **Audit spreadsheets (Git-style):** grid views of the AP and AR subledgers, the GL, holds and the CFO forecast. Click a cell for **blame** (change set, agent, task, source document and page, policy). The **history** tab lists every version of the cell. **Diff** compares any two change sets. **Log** is the per-case change-set list, like `git log`. Export to XLSX or CSV.
  11. **Validator and learning:** reviews (pass or issue), friction items, and memory and instruction diffs per agent, with version, author (Validator or human) and a revert button.
  12. **Flags and human inbox:** a flag button on every row, cell, task, call and decision. Shows flag status and what changed because of it. Approvals live here too.
  13. **Agent inbox (default view):** a nine-agent roster, shared team thread and per-agent threads built from actual task artifacts, A2A handoffs, audit summaries, redacted calls and reviews. Case/search filters, local read markers and an evidence inspector make the work navigable. Missing/disconnected data remains explicit. A separately labeled replay uses captured local fixture execution; playback performs no live action. Direct operator messaging remains outside this milestone.
- **S5 Performance:** UI updates < 500 ms after a reducer call; handles 2 events/s for 30 min.

Done when: all 13 views run off live subscriptions, with no polling except a fallback. Recorded fixture replay is an explicit preview mode and does not satisfy live acceptance.

### W3: A2A mesh
**Purpose:** the agents themselves and how they collaborate.

Requirements:
- **A1 Nine services**, each its own process with an Agent Card at `/.well-known/agent-card.json` and a JSON-RPC endpoint (a2a-sdk **1.2.3**). See the roster in section 8.
- **A2 Shared `AgentRuntime`:** each agent owns its LLM/tool loop and calls its configured provider directly through a provider adapter (OpenAI, Anthropic Claude, or another supported provider). Normalize structured outputs and tool-call results into the existing contracts. Each agent has instructions, knowledge and an allowlist of tools; file/code tools run in an isolated per-task sandbox on the agent execution host. Provider credentials remain in the service environment, outside prompts and tool sandboxes. High-stakes multi-model review, when configured, runs within W3 and applies P-009 on disagreement. It provides `delegate(skill, envelope, mode)` with modes `await | park | fanout`, park/resume on `INPUT_REQUIRED`, and resume via follow-up messages on the same task id.
- **A3 Guards:** delegation depth ≤ 3, no cycles, fan-out ≤ 3, per-agent delegation allowlist, idempotency key per delegation, per-skill timeouts, retries with backoff, a reconciler loop every 5 s.
- **A4 Handoff boundary:** W1 owns the Redis business-event consumer and first `SendMessage` on ASUS. W3 owns receiving-task deduplication and all subsequent delegation through `FreightAgent.delegate()`. W3 also runs the durable Validator/review-outbox delivery worker; this internal delivery worker is separate from ASUS intake routing. Shared A2A helpers remain under W3 ownership.
- **A5 Policy engine:** a pure-Python function `check(action, actor, payload) -> allow | deny | needs_approval(rule_id)`. It is called before every state-changing tool, and the reducers enforce the same rules again (defense in depth).
- **A6 Audit writer:** every step becomes a SpacetimeDB `audit_events` row (hash-chained per case). It records structured rationale, never chain-of-thought.
- **A7 Voice Desk:** the `place_call` and `handle_inbound_call` skills. Cartesia API, webhook gateway, transcript → findings through the Voice Desk's provider adapter, `call_result` artifacts (section 7.5).
- **A8 Precedents:** after a case resolves, write `precedents`. Before investigating, agents query the precedents for the same carrier and claim type. A high-confidence match resolves the case automatically and cites the precedent.
- **A9 "Ask why":** `GET /cases/{id}/why?decision=` composes an answer from audit rows (data, rule, precedent, approver) using the responsible agent's provider adapter. It never makes up facts that aren't in the audit trail.

- **A10 Validator (:9108), the review and learning loop:** see section 7.7. It reviews every completed output from AP, AR, Controller and CFO. Deterministic re-checks: recompute totals, tie the subledger to the GL, check debits = credits, forecast rows sum. An LLM review of the case's A2A message history and tool log looks for friction: retries, rejections, policy denials, timeouts, missing documents, re-delegations, human overrides. Output: a `review` row plus `lessons`. Lessons become versioned **memory entries** (applied automatically) or **instruction patches** (applied automatically as versioned diffs; reverting is one click). The Validator also handles `review_flag` tasks from humans.
- **A11 Memory:** each agent's runtime loads its `instructions.md@version` and its top-k relevant `agent_memory` entries (by carrier, lane, claim type and skill) into every task. The memory and instruction version used is recorded on every task and audit row.
- **A12 Change sets:** every state-changing tool call writes one `change_set` (hash-chained, with a parent) plus `cell_change` rows (before/after) and `doc_read` rows (which document and page each agent read). This is the Git-style audit.

Done when: acceptance tests T1–T9 and T13–T17 (section 11) pass.

### W4: Real-world data, enrichment & demo outreach (person 4)
**Purpose:** make the world real. Real counterparties, real contacts, realistic documents, and a safe demo path that shows we *can* call and email them.

- **R1 Real counterparties:** choose a real lane cluster (for example Northern California ↔ Nevada produce). Pull about 300 real carriers from FMCSA via the Apify SAFER actor: legal name, USDOT/MC, authority status, registered phone and address, fleet size. Scrape about 40 real shippers (public company sites, maps listings) via Apify: name, address, website, the AP or logistics page. Store them in Postgres `company` with `source_url` and `scraped_at`. **Decided:** the home brokerage is fictional. We simulate a brokerage's internals under the name **Golden Gate Freight** (used in materials, the voice persona and the UI). Every counterparty is real.
- **R2 Contact enrichment waterfall (sponsor-first).** Every result stores `source`, `source_url`, `confidence` and `fetched_at`. Everything is cached, and the hero counterparties are pre-enriched by C1.
  1. **FMCSA via Apify:** the reviewed implementation uses `humble-echidna/fmcsa-carrier-check` build `0.1.2`, with source-bound USDOT/MC input, exact identity matching, a one-result request and a 24-hour cache. Public carrier, authority and contact fields are cited observations. They still require independent review before becoming verified directory contacts (P-008).
  2. **Apify Contact Details Scraper:** the reviewed implementation uses `vdrmota/contact-info-scraper` build `0.2.242`, restricted to the ingested public company website and one page. Public email/phone candidates remain unverified; social-profile crawling and enrichment add-ons are disabled.
  3. **context.dev (future adapter):** company data, people enrichment and company news remain outside the current Cartesia/Apify milestone.
  4. **Other contact-data APIs (future adapters):** direct dials and named billing contacts may fill gaps after the current milestone. No additional enrichment key is required now.
  - Merge rule: prefer FMCSA, then our key, then context.dev, then website scrape. Conflicting values are kept as alternates, and the disagreement is shown (Risk uses it as a signal).
  - **Display:** the intended counterparty directory view may show approved business contact data. The current inbox sanitizes tool results and labels synthetic business context as **Demo case** and confirmed public Apify observations as **Live external evidence**. Its inspector separates case data from tool evidence. Outreach stays demo-routed (R5).
- **R3 Realistic synthetic documents:** generate invoices, rate confirmations, BOLs and PODs as PDFs, plus email threads, all against the real counterparties (real names, MC numbers, addresses and lanes) with synthetic loads, amounts and line items. Mark them with a small "SIMULATED" footer. Planted errors (padded detention, lumper without a receipt, duplicates, rate above contract, bank-change fraud, double-brokering, short-pay, misapplied payment) go in an `answer_key`, with clean decoys mixed in.
- **R4 Simulator and ingest:** run a continuous event stream with a 3-month fast-forward history at boot. Ingest normalizes, timestamps and dedupes events, writes Postgres, publishes to Redis and opens SpacetimeDB cases. Train an anomaly model (IsolationForest) on clean history as the `score_anomaly` tool. The evaluator joins outcomes to the answer key and writes `metric` rows.
- **R5 Outreach safety (hard rule):** `OUTREACH_MODE=demo` is the only mode at the event. Real contacts are resolved and shown as `intended_to`. The actual dial and send target is replaced with `DEMO_ALLOWLIST` (team phones and team inbox). Code refuses any target not on the allowlist, even if someone flips the flag. Every call and email record stores both `intended_to` and `routed_to`.
- **R6 Demo console (UI by W2, backend by W4):**
  1. Pick a case, or type a scenario that W4 normalizes into a case/event before ASUS first-hop routing.
  2. Choose the channel (call or email) and the teammate playing the counterparty.
  3. **Start**. The Voice Desk places a real Cartesia call to the teammate, using the real counterparty's context (company, load, claim) as dynamic variables.
  4. The audience hears the back-and-forth on speaker. The transcript appears in the case (live if Cartesia exposes it, otherwise right after the call). The findings resume the A2A task.
  5. For email, the message lands in the team inbox, and replying resumes the task.
  6. Buttons for the preset scripts: PHONE (detention), EMAIL (bank change), SHORT-PAY (collections).
- **R7 Integrations (as tools; section 7.6):** current implementation covers Cartesia persona/number configuration and real Apify carrier/company lookups. CRS and Nessie are deferred; context.dev, other enrichment and email remain future adapters. Legacy scaffolds may use `MOCK_INTEGRATIONS=1`, but live agent tools never substitute these mocks for external evidence.
- **R8 Deliverables:** the pitch, the 3-minute video, Devpost, a README covering real vs simulated vs demo-routed data, the Cartesia features used, and the published Apify actor.

Done when: the counterparty panel shows real enriched data for every hero case. The demo console runs PHONE and EMAIL end to end. T9, T10 and T16 pass.

## 7. Interface contracts (frozen Sat 3:00 AM)

### 7.1 ASUS intake/routing boundary (W4 -> W1 -> W3)
The supported integration path is the existing Redis event contract (section 7.3) followed by the existing A2A task contract (section 7.2). No new shared task-envelope fields are introduced by this architecture correction.

1. W4 ingest publishes a normalized event to Redis.
2. W1's consumer on ASUS selects an allowed skill/owner, resolves its configured Agent Card URL, and constructs a `TaskEnvelope` using the existing fields.
3. W1 sends an A2A message to that service. Initial metadata uses `from_agent: "router"`, `delegation_depth: 0`, `visited: []`, a stable event-based `idempotency_key`, and the existing case/correlation fields.
4. W3 durably creates or returns the existing task for that idempotency key. W1 stores the acceptance mapping and acknowledges the Redis event. Acceptance means the task is durably queued/created, not that the financial work has completed.
5. The receiving agent executes using its own model provider and scoped tools. All child delegations go directly to other A2A services. W1 does not wait for the whole case, relay child tasks, or proxy provider calls.

`harness/server.py` currently exposes mock `/v1/chat/completions`, `/v1/models` and `/v1/sandbox/exec` endpoints from the earlier design. These are implementation migration work, not the v1.3 integration contract; agents must not depend on them for inference or tools. Any internal classifier endpoint on ASUS is local to W1 intake. W1 owns the deployment of the initial event consumer currently scaffolded in `router/main.py`; W3 supplies the A2A runtime helpers.

Mock boundary: a seeded event reaches a stub A2A agent and produces a recorded acceptance. W1 and W3 must exercise duplicate delivery, lost acceptance responses and restart recovery against the same unchanged envelope. Additional routing telemetry or configuration APIs require their owners' agreement before adding schemas.

### 7.2 A2A envelope (W3 internal, also shown in the UI)
- Case → `context_id`. A delegated work item → `Task.id`. Payload → `Part.data` (use `new_data_part` / `new_data_message`). Result → a named `Artifact` with a data part.
- `Message.metadata`: `case_id, from_agent, parent_task_id, correlation_id, causation_id, delegation_depth, visited[], idempotency_key, priority`.
```python
TaskEnvelope = {task_type, case_id, display_id, subject:{load_id, invoice_id, carrier_id, amounts…},
                evidence_refs:[…], requested_output, priority:"P0..P3", deadline_s}
ResultArtifact = {outcome:"clear|suspicious|verified|unverified|hold|approve|reject|partial",
                  confidence, findings:{…}, evidence_refs:[…], policy_ids:[…], model_used, recommended_next:[…]}
```
- Business status in status metadata: `queued, investigating, awaiting_other_agent, awaiting_external_verification, awaiting_call, awaiting_human_approval, resolved, rejected, failed`. All `awaiting_*` statuses map to A2A `INPUT_REQUIRED`.
- Agent Card URLs: `http://<configured-agent-host>:91xx/.well-known/agent-card.json` (or the deployed HTTPS equivalent). Configure each endpoint independently; ASUS is not the default host for the mesh.

### 7.3 Events: Redis Streams (W4 produces, W1 ASUS router consumes)
Stream `events` (single stream, `type` field), consumer group `router`.
```json
{"event_id":"EVT-…","type":"carrier_invoice.received","ts":"2026-06-14T15:02:11Z","sim_ts":"…",
 "case_hint":"LOAD-4821","priority":"P2","payload":{…},"source":"sim|demo|judge|voice"}
```
Types: `load.tendered, load.dispatched, gps.ping (not routed), load.delivered, pod.received, carrier_invoice.received, shipper_payment.received, bank.event, bank_change.requested, email.received, voice.inbound.dispute, scenario.injected`.
Routing: carrier_invoice/pod → AP; shipper_payment/bank.event → AR; bank_change → Risk; voice.inbound.dispute → AP; email → by classifier; scenario → by its generated type.

### 7.4 SpacetimeDB schema and reducers (W2 provides; W3, W1 and W4 write; UI reads)
Database name `freightops`. Agents call reducers over HTTP: `POST /v1/database/freightops/call/<reducer>` with a JSON array body and a bearer token (**W2 confirms this path for 2.x by C0**; Python agents have no official SDK, so HTTP it is). The dashboard uses the generated TypeScript client bindings over WebSocket.

Tables:
- `case(case_id pk, load_id, opened_at, status, priority, title, exposure_usd, resolved_at)`
- `task(task_id pk, case_id, parent_task_id, agent, skill, display_id, a2a_state, business_status, outcome, confidence, model_used, started_at, updated_at)`
- `agent(name pk, status, active, queued, done, errors, p50_ms, card_url, last_seen)`
- `agent_config(agent pk, model:"auto"|id, enabled)`: each W3 service resolves `auto` using its own provider configuration and validates supported model IDs; ASUS only uses enabled/availability information for dispatch.
- `ledger_entry(id pk, case_id, period, lines:[{account, debit, credit}], memo, requested_by, approved_by, posted_at)`
- `hold(id pk, case_id, invoice_id, amount, reason_code, status, placed_by, released_by)`
- `payment_proposal(id pk, case_id, carrier_id, amount, status, proposed_by)`
- `approval(id pk, case_id, task_id, action, amount, requested_by, approver, decision, code_ok, decided_at)`
- `audit_event(id pk, ts, case_id, task_id, agent, kind, reason_code, summary, evidence_refs, policy_ids, model_used, prev_hash, hash)`
- `call(id pk, case_id, task_id, persona, direction, to_masked, status, started_at, ended_at, findings_json, transcript_redacted)`
- `precedent(id pk, carrier_id, claim_type, lane, pattern_json, outcome, as_of, source_case)`
- `llm_call(id pk, ts, agent, model, latency_ms, tokens, route_reason, ensemble_agreement)`
- `metric(name pk, value, updated_at)`
- `change_set(id pk (short hash), parent_id, case_id, task_id, agent, action, summary, policy_ids, instr_version, memory_ids, ts, hash)`
- `cell_change(id pk, change_set_id, sheet, row_key, column, before, after)`: sheets are `ap_subledger, ar_subledger, gl, holds, cash_forecast, payments`
- `doc_read(id pk, change_set_id|task_id, agent, doc_ref, page, span, extracted_json)`
- `review(id pk, target_task_id, case_id, verdict:"pass|issue", checks_json, friction_json, ts)`
- `agent_memory(id pk, agent, scope:{carrier_id?, lane?, claim_type?, skill?}, text, source:"validator|human", source_ref, version, active, created_at)`
- `instruction_version(agent, version, diff, reason, author:"validator|human", source_ref, created_at, active)`
- `flag(id pk, target_ref, kind:"wrong|unclear|policy|praise|other", note, raised_by, status:"open|reviewing|resolved|rejected", resolution_ref, ts)`
- `company(id pk, kind, name, usdot, mc, phone, website, source_url)` and `contact(id pk, company_id, name, title, phone, email, linkedin_url, source, source_url, confidence, alternates_json)`: UI copies of W4's directory, shown unmasked.
- `delegation_edge(id pk, ts, case_id, from_agent, to_agent, skill, task_id)` (drives the edge animation)

Reducers (with the caller role that may call each):
`open_case (ingest)`, `upsert_task (any agent)`, `agent_heartbeat (any agent)`, `record_delegation (any agent)`, `append_audit (any agent; computes the hash server-side)`, `propose_payment (carrier_ap)`, `place_hold (controller)`, `release_hold (controller; >$5k needs approval)`, `post_journal_entry (controller; balanced, period open, SoD)`, `request_approval (controller)`, `decide_approval (human)`, `upsert_call (voice_desk; stores intended_to and routed_to)`, `add_precedent (controller)`, `log_llm_call (harness)`, `set_metric (evaluator)`, `set_agent_model (human)`, `commit_change_set (any writer; must be called in the same reducer transaction as the data change)`, `record_doc_read (any agent)`, `add_review (validator)`, `upsert_memory (validator, human)`, `publish_instruction_version (validator, human)`, `revert_instruction_version (human)`, `raise_flag (human)`, `resolve_flag (validator)`. Every ledger, hold, payment or forecast reducer emits its `change_set` and `cell_change` rows **atomically**.

**Telemetry ownership:** the existing `harness` writer role for `log_llm_call` is a telemetry permission, not a requirement to run inference on ASUS. A W3-side telemetry collector may use a dedicated credential with that role to publish normalized agent usage; it does not proxy inference. Preserve current reducer permissions until W2/W3 agree any role changes. W1 intake dispatch telemetry is separate from agent inference telemetry.

Mock by Sat 3 AM: the tables exist, the reducers insert rows without validation, and the dashboard shows raw tables.

### 7.5 Voice Desk ↔ Cartesia (W3 code, W4 config)

Implementation note (v1.9): local polling is the default completion transport. The configured
hosted personas currently have no gateway tools attached. `CARTESIA_WEBHOOK_TOOLS_ENABLED=1`
requires a genuine public HTTPS URL and webhook secret before those optional capabilities are used.
These setup choices do not change the task, call-result, or reducer payload contracts below.
- An agent delegates `place_call` in park mode with `{purpose: verification|collections|approvals|cash_brief, counterparty_id, questions[], facts{}}`. Allowed purposes per caller: Carrier Trust & Verification and POD & Document Chase → verification, Shipper Billing & Collections → collections, Settlement Approvals → approvals. The existing `cash_brief` purpose remains reserved; CFO → Voice Desk is not an allowed delegation edge.
- The number comes from the verified directory only: the `contact` or `company` record with a source (FMCSA, our contact-data key, context.dev or website). Policy checks: P-008, P-011, quiet hours, ≤ 1 attempt per counterparty per hour.
- **Demo routing:** the Voice Desk sets `intended_to` = the real number and `routed_to` = the demo allowlist number, and dials only `routed_to`. Both are written to `call` and shown in the UI.
- `POST https://api.cartesia.ai/agents/calls`, headers `X-API-Key`, `Cartesia-Version: 2026-08-14`, body `{agent_id, from_number_id, ringing_timeout_seconds:25, outbound_calls:[{to_number, dynamic_variables:{case_id, task_id, call_token, …facts}}]}`.
- In-call webhook tools hit `https://<tunnel>/voice/tools/<name>`. They are read-only: `get_load_facts, get_payment_status, get_invoice_status, get_case_summary`. The only writes allowed append evidence or events: `record_promise_to_pay, open_dispute`.
- System tools: `end_call`, `voicemail_detection`, `transfer_to_number`.
- Call end: a webhook (check `x-webhook-secret`), or poll `GET /agents/calls/{id}`. Then transcript → redact PII → Voice Desk provider-adapter extraction against the persona's schema → `call_result` artifact → the parent resumes. No answer returns `{reached:false}`.
- Every persona opens with AI disclosure plus the reason for the call. Custom metrics: `disclosure_given`, `claim_confirmed`.

### 7.6 Integration tools (W4 implements as Python functions in `integrations/`, W3 calls them)

Implementation note (v1.9): `integrations/apify.py` supplies asynchronous, bounded adapters to the
agent tool registry for public carrier records and company websites. The signatures below remain
the broader workstream contract; CRS/Nessie are deferred and other unfinished adapters remain
explicitly unavailable. Scraped contacts require independent verification before directory promotion.
```python
fmcsa_lookup(usdot:str|None, mc:str|None) -> {legal_name, authority_status, phone, insurance_on_file, oos_rate, …}  # Apify, cached 24h
crs_business_credit(name, state) -> {score, liens, judgments, filings}   # CRS sandbox
crs_ofac(name) -> {hit:bool, matches:[…]};  crs_idv(person) -> {verified:bool, method}
nessie_transfer(from_acct, to_acct, amount, memo) -> {transfer_id, status}
nessie_list_deposits(acct, since) -> [...]
send_email(to, subject, body, reply_token) -> {message_id};  # inbound replies → event email.received with reply_token
score_anomaly(invoice) -> {score, top_features}
enrich_contact(company_id, role) -> {name, title, phone, email, source:"fmcsa|own_key|context_dev|website", confidence, alternates}  # waterfall; W4
scrape_company(url|name) -> {website, address, phones, about}                 # Apify; W4
```
Each tool returns `{"error": …}` instead of raising, and is mockable with `MOCK_INTEGRATIONS=1`.

### 7.7 Validator loop (W3)
Trigger: W3's review-outbox delivery worker (or the agent's own runtime) sends `review_output` to the Validator for every terminal task from AP, AR, Controller and CFO, and `review_flag` for every new `flag`. Delivery is park mode, so it never blocks the case.
```python
ReviewInput  = {target_task_id, case_id, agent, artifact, change_set_ids, a2a_history_ref, tool_log_ref, flag?}
ReviewOutput = {verdict:"pass|issue", checks:[{name, ok, expected, actual}],
                friction:[{kind:"retry|reject|policy_deny|timeout|missing_doc|redelegation|human_override|flag", detail, cost_s}],
                lessons:[{agent, type:"memory|instruction", scope, text_or_diff, confidence, evidence_refs}],
                actions:["reopen_case"|"rerun_task"|"none"]}
```
Rules:
- Deterministic checks run first. The LLM review uses the Validator's configured provider adapter; any high-stakes ensemble runs in W3, not on the ASUS intake path.
- A memory lesson is written to `agent_memory` straight away.
- An instruction lesson becomes a new `instruction_version` with a diff. It goes live on that agent's next task, and a human can revert it.
- Lessons can **never** change policy code, limits, tool permissions or delegation allowlists (P-012).
- An issue verdict on a financial output can reopen the case or re-run the task (once), with a new change set.

### 7.8 Audit model: Git for the books (W2 storage, W3 writes)
- One state-changing action = one `change_set`, like a commit: a parent hash, the author agent, the task, the policy ids, the instruction version and memories used, the source documents read, and before/after cell changes.
- **Blame**(sheet, row, column) gives the latest change set touching that cell. **History** lists all change sets for the cell. **Diff**(a, b) gives the cell changes between them. **Log**(case) lists the change sets in order.
- **Revert** is a new compensating change set, never an edit. It needs human approval.
- The `change_set.hash` chain verifies on demand (T2).

### 7.9 Human input (W2 UI, W3 routing)
- **Flag** anything: a cell, row, task, call, decision or memory entry. A flag raises a `flag` row and a `review_flag` A2A task to the Validator. The resolution links back, with the memory or instruction diff and any re-run.
- **Approve or reject:** the approvals inbox, with a one-time code.
- **Take over:** a human can mark a case `human_owned`. Agents then stop acting on it and only assist.
- Every human action is a change set with author `human:<name>`.

## 8. Agent roster

Six frontline specialists and three supporting specialists; **nine independently running agents total**. Display names identify brokerage jobs, while service IDs preserve existing integrations.

| Agent (service ID) | Port | Skills (Agent Card) | Work it owns | May delegate to | Hard limits |
|---|---|---|---|---|---|
| Carrier Settlement (`carrier_ap`) | 9101 | process_carrier_invoice, propose_payment, answer_payment_status | Invoice/rate/POD matching, approved accessorials, duplicate detection, payment readiness | risk, controller, verification, documents | Proposes only; never pays; held invoices remain blocked |
| Shipper Billing & Collections (`shipper_ar`) | 9102 | reconcile_customer_payment, chase_overdue, set_credit_limit | Billing documentation, deposit matching, short-pay disputes and collections | controller, verification, voice_desk, cfo, documents | No write-offs; credit limit is a recommendation until a persistence contract is agreed |
| Load Profitability (`cfo`) | 9103 | assess_financial_impact, cash_forecast | Margin per load, unrecovered charges, collections timing and cash exposure | shipper_ar, carrier_ap, controller | Read-only on the ledger; writes forecasts only |
| Accessorial Audit (`risk`) | 9104 | investigate_charge, screen_bank_change, screen_counterparty | Detention, lumper, layover and other disputed charges; duplicate/fraud signals and escalation | verification, controller, documents | Recommends holds; cannot place them |
| Carrier Trust & Verification (`verification`) | 9106 | verify_claim, verify_bank_change, verify_dispute | Carrier identity, authority/insurance evidence, verified channels and payment-detail changes; legacy claim skills remain supported | voice_desk | No financial or bank-detail mutation; verified contact channels only |
| POD & Document Chase (`documents`) | 9109 | collect_documents, check_document_packet | Missing signed PODs/BOLs, rate confirmations and receipts; packet completeness, follow-up and resuming dependent work | voice_desk | Evidence collection only; cannot approve charges, payment or carrier identity |
| Settlement Approvals (`controller`) | 9105 | review_hold, check_already_paid, reconcile, approve_entry | Holds, reconciliations, exact-action approvals and balanced journal posting | cfo, verification, voice_desk, documents | Only ledger writer; requester cannot approve; threshold rules remain enforced |
| Dispatcher Outreach (`voice_desk`) | 9107 | place_call, handle_inbound_call | Calls for documents, verification, collections and approvals; transcript-to-findings | none; inbound disputes become intake events | Directory numbers and demo allowlist only; AI disclosure; no payment approvals |
| Settlement Quality Review (`validator`) | 9108 | review_output, review_flag | Financial checks, friction review, scoped lessons, instruction revisions and bounded corrections | none; corrective requests use review outbox | Cannot change policy, limits, permissions or allowlists; cannot touch ledger |

The new `documents` role is added to service provisioning and W2 task/audit/heartbeat permissions, with the same delegation graph as Python. Financial reducer permissions are unchanged. Both document recovery and carrier verification may place verification-purpose calls, but neither may delegate to the other to evade the depth limit.
Every agent folder contains `card.py`, `instructions.md`, `knowledge/`, `tools.py` and `main.py`.

## 9. Controls (deterministic, in code and again in reducers)
P-001 invoice > contract + 2% → hold the delta · P-002 no payment on a held invoice · P-003 payment > $5,000 → human approval · P-004 bank change → out-of-band verification on the directory or FMCSA phone, plus IDV for owner-operators · P-005 requester ≠ approver · P-006 journal debits = credits, period open · P-007 duplicate (carrier, load, amount within 14 days) → reject · P-008 external contact only to verified channels · P-009 ensemble disagreement on a high-stakes call → human approval · P-010 untrusted text can't authorize an action · P-011 at the event, outreach goes only to `DEMO_ALLOWLIST` targets · P-012 the Validator and memory system can't change policy, limits, permissions or allowlists; instruction changes are versioned and revertible.

## 10. Hero case data (regression fixture; W4 seeds it, everyone tests with it)

These fixed values are one regression fixture, not production defaults. Agents must process arbitrary supported case IDs, carriers, invoice amounts, contract terms and evidence through the same code. No handler may branch on these IDs or return the answer key. Missing-document, clean, disputed-charge, duplicate, bank-change and short-pay cases must be covered independently.
- `CASE-4821` / `LOAD-4821`, lane Stockton CA → Reno NV, carrier `C-311` "Sierra Haul LLC" (USDOT 3141592, owner-operator), shipper `S-07` "Bay Produce Co".
- Contract and rate con **$1,850**. Invoice **$2,150** = $1,850 + **$300 detention (2 h)**, no receipt.
- GPS geofence at the receiver: arrived 14:02, departed 14:49 (**47 min**). Free time 2 h, so no detention is owed.
- Second-chance replay: `LOAD-4877`, the same carrier and pattern, 2 sim-weeks later.
- Bank-change demo: carrier `C-77` "Rapid Ridge Transport" emails new bank details the day before a **$9,400** payment.

## 11. Acceptance tests (shared definition of done)
- T1: All 9 Agent Cards resolve and validate.
- T2: The hero case produces ≥ 5 A2A tasks under one context_id, each with an artifact, and the audit hash chain verifies.
- T3: Trigger to ring ≤ 15 s (measure 5 times).
- T4: 20 concurrent cases plus 1 waiting on a call: all finish.
- T5: `kill -9` Risk mid-case, restart: the case finishes, with no duplicate tasks.
- T6: An A→B→A delegation is refused and logged.
- T7: A $9.4k release waits for a human; the approval with its code goes through; the requester can't approve.
- T8: The precedent replay resolves with no call and cites the precedent.
- T9: A payload with a different phone number still dials the directory number; the mismatch is a risk signal.
- T10: The scoreboard shows precision and recall after 30 min of simulation.
- T11: Pin Risk to a supported provider/model configuration in the UI; the next call goes directly from Risk to that provider, with the selected model recorded. ASUS receives no inference request.
- T12: An agent sandbox tool trying to reach the network is blocked and logged on its execution host; intake preprocessing isolation is tested separately on ASUS.
- T13: Every AP, AR, Controller and CFO output gets a `review` row within 30 s.
- T14: A seeded friction case (missing rate con, then re-request) produces an AP memory entry, and the next similar case uses it (the memory id appears on the task).
- T15: Clicking the hold cell shows blame back to the source PDF and page, the reading agent, the change set and the policy. A diff between two change sets is correct.
- T16: With `OUTREACH_MODE` forced to anything, a non-allowlisted number is refused, and the call record shows both `intended_to` and `routed_to`.
- T17: A human flag on a CFO forecast row is resolved by the Validator, with a visible diff and a re-run.
- T18: Stop ASUS after AP accepts a task: downstream AP -> Risk -> Verification and case completion continue. New events remain in Redis and dispatch after ASUS restarts.
- T19: Re-deliver the same intake event, including after a lost acceptance response: one initial A2A task is created and one event-to-task mapping is retained.
- T20: Dispatch to at least two independently hosted agents; demonstrate direct provider calls and direct A2A delegation. The initial setup may use OpenAI for both with separate operational/reviewer models. Repeat with different providers when optional Anthropic credentials are available; report that cross-provider extension separately.
- T21: POD & Document Chase receives a delegated missing-document task, records the required evidence, and resumes the waiting settlement task without duplicate outreach. Its service identity cannot perform financial mutations.
- T22: Run varied amounts, counterparties, line items and case IDs through clean, document-missing, disputed-charge, duplicate, bank-change and short-pay scenarios. Outcomes must follow the records and policy, not the hero fixture.

## 12. Timeline (PDT; deadline **Sun 9:00 AM**; judging 10:30 AM–12:30 PM)

| Time | Checkpoint | W1 Harness | W2 SpacetimeDB/UI | W3 A2A | W4 World/integrations |
|---|---|---|---|---|---|
| Fri 11:45 PM – Sat 3 AM | **C0: contracts frozen, mocks up** | Redis intake + mock first-hop dispatch | Module with tables; raw UI | 9 services boot with cards; provider adapters; stub decisions; AP→Risk→Verification over A2A | Seeded world; 10 fixed events; Cartesia number provisioned; API keys |
| Sat 3 AM – 9 AM | **C1: spine end to end** | Typed routing, validated classification, P0 lane | Reducers with invariants; network + feed views | Park/resume, joins, reconciler, policy, audit to SpacetimeDB | Continuous generator, ingest → Redis, hero triggers, Apify carrier/company tools; Nessie deferred |
| Sat 9 AM – 3 PM | **C2: hero case live with a real call** | Dispatch deduplication/recovery, intake isolation, routing telemetry | Case drill-down, approvals inbox, ledger | Voice Desk + local completion poller, optional webhook tools, provider/tool isolation, precedents, "ask why" | Cartesia personas and team-call acceptance, anomaly model, answer key; CRS deferred; email future |
| Sat 3 PM – 9 PM | **C3: feature freeze** | Classification fallback, backpressure, injection quarantine | Scoreboard, harness panel, injector UI, polish | Provider pinning, crash recovery, T1–T9 and T18–T20 green, inbound line | Evaluator, judge injector, history replay, Apify actor published |
| Sat 9 PM – Sun 3 AM | **C4: hardening** | Load test at 2 events/s | Performance, backup polling | Bug fixes only | Record backup video; write README and Devpost |
| Sun 3 AM – 9 AM | **C5: rehearse and submit** | All | All | All | Submit by **8:00 AM** (1 h buffer) |

Extra items added in v1.1: by C1, W4 has 50 real carriers and 10 shippers scraped and the enrichment waterfall working for the hero contacts, and W3 has the change-set writer. By C2: the demo console call loop, the Validator v1 (deterministic checks and memory), and the audit grid with blame. By C3: instruction versioning and revert, flags end to end, and the counterparty panel.

Integration checkpoints are hard: at each one, everyone stops for 15 minutes and runs the hero case together.

## 13. Risks and fallbacks
| Risk | Fallback |
|---|---|
| ASUS intake classification is too slow or unavailable | Deterministic routes for known events, bounded classification time, triage ambiguous input, retain events in Redis |
| Agent model provider is rate-limited or unavailable | Agent-owned retry/backoff and configured provider fallback; park affected tasks, preserve direct delegation for unaffected work |
| Venue Wi-Fi or tunnel drops (Cartesia webhooks) | Phone hotspot, poll `GET /agents/calls/{id}`, pre-recorded backup video |
| SpacetimeDB is new to us (2.x API differs from older tutorials) | W2 keeps the mock schema working by C0; Postgres mirror for the audit as a backup |
| Phone number or Cartesia setup delay | Provision in the first hour; email path as the backup moment |
| An accidental real call or email | Allowlist enforced in code (T16), no real-recipient code path, test before every rehearsal |
| Enrichment credits or rate limits (Apify, context.dev, our own key) | Cache everything; pre-enrich the hero counterparties by C1; FMCSA phone as the fallback |
| The Validator writes bad memory | Memory is scoped and versioned, a human can revert it, and it never touches policy |
| Scope creep | Section 2 non-goals; feature freeze at C3 |

## 14. Repo and conventions
- Monorepo `freightops/`: `harness/ + router/main.py (W1 intake)`, `spacetime/ + web/ (W2)`, `agents/ common/ gateway/ + internal review delivery (W3)`, `sim/ ingest/ integrations/ eval/ (W4)`, `docs/PRD.md` (this file), `docker-compose.yml`.
- Python 3.12, `a2a-sdk==1.2.3`, Pydantic v2. Use `.env.example` and never commit keys (judges get the repo).
- Branch per workstream; merge to `main` at each checkpoint; `main` must always run `make demo`.
- Shared IDs: `CASE-####`, `LOAD-####`, `TASK-###` (display), carriers `C-###`, shippers `S-##`.

## 15. Open questions (owner to resolve, then update this doc)
Resolved in v1.2: the home company is fictional "Golden Gate Freight" with real counterparties; contacts are shown unmasked on the live site; enrichment is sponsor-first (Apify, context.dev) plus our own key.
1. ASUS hardware specs and intake classifier implementation; benchmark against deterministic routing before selecting a local model. — W1
2. Intake isolation on ASUS and separate agent tool-sandbox technology on W3 execution hosts. — W1 + W3
3. Per-agent provider/model assignments, deployment URLs, credentials and fallback limits. Agent-owned tool loops and direct A2A delegation are decided. — W3
4. Routing-policy RL experiment: allowed equivalent actions, reward weights, feedback attribution and held-out baseline; proposed until implemented and evaluated. — W1 + W4
5. Email provider for the team inbox and reply resume. — W4
6. Verify broader FMCSA coverage with the selected `humble-echidna/fmcsa-carrier-check` actor and validate website contact candidates before directory promotion. context.dev remains a future adapter. — W4
7. Other sponsor credits worth using for data (check the live site and Slack). — W4

## Change log
- v1.10 (Sat Oct 24): added the local Demo Console phone control, durable authenticated admission, exact-task demo authorization, idempotent retries and visible call status. Coordinated Voice/W2 changes let explicit team demos bypass hours and hourly limits while enforcing one active call; ordinary calls retain both guards. Live phone acceptance remains pending the operator's click.
- v1.0 (Fri 11:45 PM): initial ground truth.
- v1.1 (Sat 12:30 AM): dropped Fetch.ai and Coinbase. Person 4 = real-world data, enrichment and demo outreach. Demo-only outreach with intended_to and routed_to. Added the Validator agent (:9108) with memory and instruction learning, the Git-style audit with spreadsheet blame, diff and history, and human flags. Renamed the external-check agent from Validation to Verification.
- v1.2 (Sat 1:00 AM): fictional "Golden Gate Freight" with real counterparties; enrichment waterfall (FMCSA via Apify, our own key, context.dev, Apify website scrape); contacts unmasked on the live site.
- v1.3 (Sat Oct 24): corrected ASUS to intake and first-hop routing only. Independent A2A services own provider calls (including OpenAI/Claude), tools and downstream delegation. Updated deployment diagram, W1/W3 ownership, integration boundary, telemetry, recovery tests and timeline. Existing task envelopes and reducer permissions remain unchanged. Routing RL is explicitly proposed and separate from Validator instruction/memory updates; earlier local-model harness endpoints require implementation migration.

- v1.4 (Sat Oct 24): adopted nine brokerage-specific agents: six frontline specialists plus Settlement Approvals, Dispatcher Outreach and Settlement Quality Review. Added `documents` (:9109) for POD/document recovery; retained existing service IDs and envelope shapes. Updated Python/W2 delegation and role registries together, separated document collection from carrier trust checks, preserved financial controls and depth ≤3, corrected the demo’s held-invoice payment wording, and required varied-case tests. This is the implementation target; live provider/voice/data acceptance is reported separately from fixture coverage.

- v1.5 (Sat Oct 24): implemented the Agent inbox as the default dashboard view, with nine specialists, team/per-agent activity threads, case/search filters, local read markers and evidence inspection. Added a reproducible capture of actual nine-service fixture A2A execution, explicitly separated from live activity and actions. Added 15-second runtime heartbeats. The observation inbox is implemented; arbitrary operator chat and live provider/W2/Cartesia acceptance remain pending. No shared envelope or financial authorization contract changed.
- v1.6 (Sat Oct 24): made OpenAI the default provider for all nine services, selected Luna for operational assessments and Sol for the separate reviewer, and made Anthropic an optional explicit override. Updated provider setup and T20 to distinguish initial OpenAI-only acceptance from the later cross-provider extension. Live acceptance remains pending.
- v1.7 (Sat Oct 24): provisioned isolated local PostgreSQL and SpacetimeDB plus authenticated nine-agent services. Verified concurrent live OpenAI inference, two A2A handoffs and parked-parent continuation on explicitly synthetic data. Added repeatable live workload/acceptance reports, live inbox counts and source labels, task-state normalization at SQL/WebSocket boundaries, and schema-constrained evidence citations. Financial execution, real source data, telephony and cross-host acceptance remain separate.
- v1.8 (Sat Oct 24): added role-specific tool registries and durable model-selected tool execution for all nine services, with policy-guarded workflow actions and authenticated A2A delegation. Added sanitized tool-call lifecycle records and inbox inspection. Expanded live acceptance to require completed tool calls from every specialist. Existing envelopes and reducer contracts are unchanged; external integration credentials do not substitute for missing adapters.
- v1.9 (Sat Oct 24): narrowed sponsor implementation to Cartesia and Apify, with CRS/Nessie deferred. Added real source-bound Apify carrier and website tools, durable run tracking, bounded spend/time, cache and external-evidence outcome checks. Provisioned three Cartesia personas and one outbound number, added shared readiness checks and a local polling gateway, and verified real Apify observations through A2A in a synthetic business scenario. No live phone call has been placed; calling-window and demo-routing controls remain active. The catalog now has 22 tools across up to 68 role grants. Existing shared payload contracts are unchanged.
