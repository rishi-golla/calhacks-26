# PRD: Autonomous Back Office for Freight Brokerages
**Cal Hacks 13.0 · Ground truth v1.2 · Sat Oct 24, 1:00 AM PDT**

> This is the one source of truth. If code, a chat message or a slide disagrees with this doc, the doc wins until someone changes it. To change it, edit this file, bump the version in the change log at the bottom, and post one line in the team chat. Section 7 (interface contracts) is frozen at **Sat 3:00 AM**. After that, contract changes need agreement from both owners on that interface.

---

## 1. Summary

**One line:** an autonomous finance back office for a mid-size freight brokerage. Specialized AI agents, running on our own sandboxed multi-model harness on an ASUS AI supercomputer, pick up work as it arrives, delegate to each other over the A2A protocol, phone carriers and shippers with voice agents, and act on the books only through deterministic controls, recording every decision in an audit trail.

**Seats it fills (the line the Hyphenate judges ask for):** *"The carrier-payables clerk, payables fraud analyst and collections specialist at a 40-person freight brokerage, reporting to the controller."*

**Problem:** freight brokers lose money on carrier invoice padding (detention and lumper charges without proof), double-brokering, bank-change fraud and slow collections. The evidence is spread across the TMS, rate cons, PODs, GPS/ELD, email and the bank, and the sources disagree. Today humans reconcile all of it by hand.

**What makes it real, not a demo script:**
- Eight independently running agent services that talk over the real A2A protocol: Agent Cards, task IDs, artifacts, and tasks that park and resume.
- A continuous synthetic company with planted errors and an answer key, so we can prove accuracy.
- Our own harness that routes each LLM call to the best open-source model and runs every tool in a sandbox.
- Real counterparties: real carriers and shippers found by scraping (Apify: FMCSA, company sites), with real contacts surfaced through an enrichment waterfall (FMCSA, our own contact-data key, context.dev, website scrape), shown unmasked. Realistic synthetic invoices are written against them.
- Cartesia voice calls run for real, but **outreach is demo-only**: the UI shows the real person it would call, and the call actually rings a teammate playing that person. Real sandbox APIs: Nessie (bank), CRS (credit).
- A Validator agent reviews every output and every bit of friction, then updates each agent's instructions and persistent memory.
- A Git-style audit trail shown as spreadsheets: source document, which agent read it, what it did, and which cells changed.
- Live shared state in SpacetimeDB: every agent, task, hold and audit row is visible as it happens.

## 2. Goals and non-goals

**Goals (must ship):**
- G1: The hero case runs live end to end. A suspicious invoice reaches AP, then Risk, then Verification, then a Voice Desk call (demo mode), then the Controller places a hold, then the CFO impact updates. The Validator reviews it, and all of it lands in the audit trail.
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

| Tier | Track | What their judges need to see | Owner who makes it land |
|---|---|---|---|
| 1 | **Hyphenate** ($8k/$5k/$3k) | Real enough, would you hire it, something new, "the moment". A video that opens with the moment, a live demo (they may bring a scenario), a seats line, proof of what's real vs simulated | All; pitch by W4 |
| 1 | **Microsoft: Built for the Real World** | Customer understanding, measurable usefulness, path to production, responsible design, end to end | All; metrics by W4 |
| 1 | **SpacetimeDB** ($2,048/$1,024/$512) | SpacetimeDB as the *core* real-time backend for agent coordination and live dashboards | W2 |
| 1 | **Cartesia** | Voice core to the product: originality, use of Cartesia, execution and edge cases, responsibility (25% each). 2-min demo-first video, README with features used | W3 (Voice Desk) + W4 (personas) |
| 1 | **Apify** ($5k pool + $400 Best Actor) | Meaningful use: real company and carrier scraping (FMCSA, websites), plus our own published "freight counterparty" actor | W4 |
| 1 | **CRS** ($1k / $500 agent / fraud interview) | CRS central, live against the sandbox | W4 |
| 1 | **Capital One Nessie** ($300/person) | Completeness, creativity, complexity | W4 |
| 1 | **Armadin security** (criteria TBD) | Sandboxed harness, prompt-injection quarantine, SoD, tamper-evident audit | W1 + W3 |
| 2 | **Notability** | Real process notes from sketch to build | Everyone; start now |
| auto | **Cal Hacks: Most Technically Complex** | Comes from the main build | n/a |
| stretch | Bright Data: live public web data on the counterparties (site, news, reviews). It must be essential and public only; personal contacts come from the enrichment waterfall (section 6, W4 R2). Factory Droid also stretch. | Only if Tier 1 is done | W4 |
| out | Fetch.ai, Coinbase | Not doing them for now | n/a |

Check Devpost for a cap on how many tracks we can enter.

## 4. The demo (3 minutes, also the video)
1. **0:00, the moment (cold open):** the phone rings on stage. The Cartesia agent: "Hi, this is an AI assistant calling for Golden Gate Freight about load 4821. You billed two hours of detention, but our GPS shows your truck on site for 47 minutes. Can you send the receipt?" A teammate plays the carrier's dispatcher and hedges.
2. **0:30, real-world data:** the case panel shows a *real* carrier: FMCSA authority, USDOT/MC, the registered phone, the scraped website, and the dispatcher contact from the enrichment waterfall (name, title, phone, email, source, confidence), all unmasked. A banner reads: **"Outreach: DEMO MODE. Would dial +1 775 555 0112 (FMCSA-registered, verified). Routed to the team line."**
3. **0:50, the network (SpacetimeDB live):** delegations animate AP to Risk to Verification to Voice Desk, while 30 other cases keep flowing.
4. **1:05, resolution and Validator:** the call ends and the transcript becomes findings. The Controller holds $300 and pays the $1,850. The Validator re-checks the entry and the CFO projection, notices friction (AP had to re-request the rate con), and writes an AP memory note. The UI shows the memory or instruction diff.
5. **1:30, audit spreadsheet:** open the AP subledger grid and click the $300 hold cell. Blame shows the source invoice PDF (page 1, line 3), read by AP in task TASK-002, the change made by the Controller in change set `a1f3c9`, and policy P-001. The history tab shows every version of that cell.
6. **1:55, a human flag:** a judge flags a CFO forecast line as wrong. The Validator reviews the flag, updates CFO memory and re-runs the projection. The new change set appears with a diff.
7. **2:15, recovery:** `kill -9` Risk on a new case, restart it, and the case finishes.
8. **2:30, "never asks twice" and proof:** the same pattern resolves with no call and cites the precedent, then the scoreboard. "Want to try one? Type a scenario into the demo console."

## 5. System architecture

```
 ┌──────────── W4: World & integrations ────────────┐
 │ Simulator (TMS/ERP/invoices/PODs/GPS/email/bank) │
 │ Demo triggers · Judge scenario injector          │──► Ingest ──► Redis Streams ──► Router (first hop only)
 │ Answer key / evaluator                           │       │                              │ A2A SendMessage
 └──────────────────────────────────────────────────┘       ▼                              ▼
                                                     Postgres (company records,   ┌──────── W3: A2A mesh ────────┐
                                                     A2A task store)              │ Carrier AP 9101  Shipper AR 9102│
                                                                                  │ CFO 9103   Risk 9104            │
 ┌──────── W1: ASUS harness (secure env) ────────┐   LLM calls + tool exec        │ Controller 9105 Verification 9106 │
 │ Model router ("auto" or pinned per agent)     │◄──────────────────────────────│ Voice Desk 9107                 │
 │ Model pool (open-source, vLLM/SGLang/Ollama)  │                               │ Policy engine · Audit writer    │
 │ Ensemble ("fusion") for high-stakes calls     │                               └──────────────┬─────────────────┘
 │ Tool sandbox (files, code, spreadsheets)      │                     reducers (HTTP) │        │ Cartesia / Apify /
 │ Secrets vault · telemetry                     │                                     ▼        │ CRS / Nessie / email
 └───────────────────────────────────────────────┘                  ┌──── W2: SpacetimeDB ────┐  ▼
                                                                    │ cases, tasks, agents,  │  External world
                                                                    │ ledger, holds, approvals│  (phones, APIs)
                                                                    │ audit, calls, metrics   │
                                                                    │ → Dashboard (live subs) │
                                                                    └─────────────────────────┘
```

**Where state lives (decided):**
| Data | System of record | Why |
|---|---|---|
| Simulated source systems (loads, contracts, rate cons, invoices, PODs, GPS pings, emails, carrier directory) | Postgres | Plays the role of the "existing systems" the agents read |
| A2A task store (task history, status, artifacts) | Postgres via the SDK's `DatabaseTaskStore` | The SDK supports it; survives restarts |
| Operational truth: cases, task graph, agent status, **ledger, holds, approvals, payments-proposed**, audit events, call status, precedents, metrics | **SpacetimeDB** | Transactional reducers enforce invariants next to the data, and the live UI subscribes. This makes SpacetimeDB core (for its track) |
| Event stream | Redis Streams | Ingest buffer, consumer groups, replay |

**v1.1 additions to the diagram:** a **Validator :9108** in the A2A mesh (it reviews every output, owns memory and instruction updates, and receives human flags), and a **W4 enrichment pipeline** (Apify scrape → enrichment waterfall: FMCSA, own key, context.dev → `company` and `contact` directory) that feeds Verification, the Voice Desk and the UI.

**Everything runs on the ASUS box** except Cartesia (cloud), external APIs, and optionally the dashboard. A tunnel (cloudflared or ngrok) exposes `gateway:9000` over public HTTPS for Cartesia webhooks.

## 6. Workstreams and owners

| ID | Workstream | Owner | Owns |
|---|---|---|---|
| W1 | **ASUS harness** | _name_ | Model serving, model router, ensemble, tool sandbox, secrets, telemetry, harness UI panel data |
| W2 | **SpacetimeDB + dashboard** | _name_ | Module schema and reducers (ledger invariants), subscriptions, the Next.js dashboard (network graph, case tree, feeds, approvals inbox, scoreboard) |
| W3 | **A2A mesh** | _name_ | 8 agent services (including the Validator), AgentRuntime, delegation and park/resume, router, policy engine, audit/change-set writer, memory store, Voice Desk logic, webhook gateway |
| W4 | **Real-world data, enrichment & demo outreach** | _person 4_ | Scraping real counterparties, the contact enrichment waterfall (Apify, context.dev, own key), realistic synthetic documents, simulator and answer key, the demo console and the demo-mode call and email loop, Cartesia personas, CRS/Nessie connectors, pitch, video, Devpost |

Rule: **each owner can build against mocks of the other three from hour 0.** The contracts in section 7 are what make that possible. Each owner ships a mock of their side by **Sat 3:00 AM** (a stub harness that returns canned JSON, a SpacetimeDB module with tables and no-op reducers, an agent that returns canned artifacts, a simulator that emits 10 fixed events).

### W1: ASUS harness (sandboxed, multi-model, Fusion-style routing)
**Purpose:** our own inference and execution environment. Every agent's LLM call and tool execution goes through it. It decides which open-source model handles each call (or the user pins one), and it isolates tool execution.

Requirements:
- **H1 Model pool:** serve 3+ open-source models side by side behind one OpenAI-compatible endpoint, for example a small fast model (classification, extraction), a strong reasoning model (investigation, planning), and a coder/tool-calling model (spreadsheet and code tools). Choose by benchmarking on the ASUS box (candidates: Qwen3 family, gpt-oss, Llama, Mistral, Gemma). Serving: vLLM or SGLang preferred, with Ollama or llama.cpp as fallback. Confirm the hardware specs and what fits in memory first.
- **H2 Router ("auto" mode):** pick a model per call from `route_hints` (agent, task_type, priority, needs_tools, json_schema, max_latency_ms, stakes) plus live load. The rules start deterministic (a table); a learned or LLM classifier is a stretch. Every response reports which model was used and why.
- **H3 Pinning:** the dashboard can set a model per agent (`auto` or a specific model). The harness reads this from the SpacetimeDB `agent_config` table.
- **H4 Ensemble ("fusion"):** for `stakes: "high"` calls (risk verdicts, hold or release recommendations), run 2–3 models and merge. If they disagree beyond a threshold, return `agreement: false`. The agent must then escalate to a human. This is the "knows when to ask" story.
- **H5 Priority and fairness:** P0 (demo and voice path) has a reserved slot. Per-agent concurrency caps. Queue-depth metrics.
- **H6 Structured output:** JSON-schema-constrained decoding (guided decoding in vLLM or SGLang), with a validate-and-repair retry.
- **H7 Tool sandbox:** tools that touch files, run code or edit spreadsheets execute in an isolated sandbox per agent per task: a container (or nsjail or firejail), no network by default, a per-agent allowlist, a read-only mount of the case files plus a scratch dir, CPU, memory and time limits. Results are returned as JSON plus file refs.
- **H8 Secrets:** API keys live only in the harness or gateway env. Agents get capability tokens, never raw keys. The sandbox gets nothing.
- **H9 Injection quarantine:** untrusted text (email bodies, PDF text, call transcripts) is wrapped and marked `untrusted` before any prompt. Tools invoked in a turn that contains untrusted text must pass policy as usual. Log any detected instruction-like content as a risk signal.
- **H10 Telemetry:** per call: model, latency, tokens, queue wait, route reason. Written to SpacetimeDB `llm_calls` (sampled) for the dashboard.
- **H11 Fallback:** if a model is down or times out, route to the next model. If everything is down, return `{"error":"unavailable"}` so the agent takes its deterministic path.

Done when: the hero case runs on real models with p50 < 2 s per small call; the dashboard shows model per call; switching an agent's model in the UI takes effect on the next call; the sandbox blocks network access from a test tool.

### W2: SpacetimeDB + dashboard
**Purpose:** the live, transactional operational state of the company and the agents, and the UI that shows it.

Requirements:
- **S1 Module:** tables and reducers as in section 7.4, built on **SpacetimeDB 2.x**. Modules can be written in TypeScript (runs on V8), Rust, C# or C++. TypeScript is recommended so the module and the dashboard share types. The 2.0 API changed substantially, so trust the current docs over older examples and confirm the HTTP reducer-call endpoint in section 7.4 against them by C0. 2.x also has HTTP handlers and procedures, which the gateway could use.
- **S2 Ledger invariants in reducers:** `post_journal_entry` rejects unbalanced entries, entries in locked periods, and entries where `approved_by == requested_by`. `place_hold` and `release_hold` check amounts against the invoice. `release_hold` over $5,000 requires an `approvals` row with a human approver.
- **S3 Auth:** agents call reducers over HTTP with a service identity/token per agent. Humans use the dashboard identity. Reducers check the caller's role (for example, only `controller` may post journal entries).
- **S4 Dashboard (Next.js + React Flow + a spreadsheet grid component) views:**
  1. **Agent network:** 7 nodes with live status, active, queued and done counts, and an edge animation on every A2A delegation.
  2. **Live task feed:** newest first, filterable by agent or case.
  3. **Case drill-down:** the task tree, each task's artifacts, evidence, policy ids, model used, and call transcript and recording link.
  4. **Approvals inbox:** approve or reject with a one-time code; the decision goes to a reducer.
  5. **Ledger and holds:** journal entries, holds, cash position.
  6. **Scoreboard:** precision, recall, $ caught, false holds, median time to resolution, calls placed.
  7. **Harness panel:** model per agent (dropdown: auto or pinned), recent LLM calls, ensemble disagreements.
  8. **Demo console:** W4 backend; see W4 R6.
  9. **Counterparty panel:** the real company, the FMCSA data, the enriched contacts with source and confidence, and the outreach-mode banner.
  10. **Audit spreadsheets (Git-style):** grid views of the AP and AR subledgers, the GL, holds and the CFO forecast. Click a cell for **blame** (change set, agent, task, source document and page, policy). The **history** tab lists every version of the cell. **Diff** compares any two change sets. **Log** is the per-case change-set list, like `git log`. Export to XLSX or CSV.
  11. **Validator and learning:** reviews (pass or issue), friction items, and memory and instruction diffs per agent, with version, author (Validator or human) and a revert button.
  12. **Flags and human inbox:** a flag button on every row, cell, task, call and decision. Shows flag status and what changed because of it. Approvals live here too.
- **S5 Performance:** UI updates < 500 ms after a reducer call; handles 2 events/s for 30 min.

Done when: all 8 views run off live subscriptions, with no polling except a fallback.

### W3: A2A mesh
**Purpose:** the agents themselves and how they collaborate.

Requirements:
- **A1 Seven services**, each its own process with an Agent Card at `/.well-known/agent-card.json` and a JSON-RPC endpoint (a2a-sdk **1.2.3**). See the roster in section 8.
- **A2 Shared `AgentRuntime`:** the LLM tool loop goes through the harness. Each agent has instructions, knowledge and an allowlist of tools. It provides `delegate(skill, envelope, mode)` with modes `await | park | fanout`, park/resume on `INPUT_REQUIRED`, and resume via follow-up messages on the same task id.
- **A3 Guards:** delegation depth ≤ 3, no cycles, fan-out ≤ 3, per-agent delegation allowlist, idempotency key per delegation, per-skill timeouts, retries with backoff, a reconciler loop every 5 s.
- **A4 Router:** a Redis consumer group that maps event type to the owning agent and makes the first SendMessage only.
- **A5 Policy engine:** a pure-Python function `check(action, actor, payload) -> allow | deny | needs_approval(rule_id)`. It is called before every state-changing tool, and the reducers enforce the same rules again (defense in depth).
- **A6 Audit writer:** every step becomes a SpacetimeDB `audit_events` row (hash-chained per case). It records structured rationale, never chain-of-thought.
- **A7 Voice Desk:** the `place_call` and `handle_inbound_call` skills. Cartesia API, webhook gateway, transcript → findings through the harness, `call_result` artifacts (section 7.5).
- **A8 Precedents:** after a case resolves, write `precedents`. Before investigating, agents query the precedents for the same carrier and claim type. A high-confidence match resolves the case automatically and cites the precedent.
- **A9 "Ask why":** `GET /cases/{id}/why?decision=` composes an answer from audit rows (data, rule, precedent, approver) using the harness. It never makes up facts that aren't in the audit trail.

- **A10 Validator (:9108), the review and learning loop:** see section 7.7. It reviews every completed output from AP, AR, Controller and CFO. Deterministic re-checks: recompute totals, tie the subledger to the GL, check debits = credits, forecast rows sum. An LLM review of the case's A2A message history and tool log looks for friction: retries, rejections, policy denials, timeouts, missing documents, re-delegations, human overrides. Output: a `review` row plus `lessons`. Lessons become versioned **memory entries** (applied automatically) or **instruction patches** (applied automatically as versioned diffs; reverting is one click). The Validator also handles `review_flag` tasks from humans.
- **A11 Memory:** each agent's runtime loads its `instructions.md@version` and its top-k relevant `agent_memory` entries (by carrier, lane, claim type and skill) into every task. The memory and instruction version used is recorded on every task and audit row.
- **A12 Change sets:** every state-changing tool call writes one `change_set` (hash-chained, with a parent) plus `cell_change` rows (before/after) and `doc_read` rows (which document and page each agent read). This is the Git-style audit.

Done when: acceptance tests T1–T9 and T13–T17 (section 11) pass.

### W4: Real-world data, enrichment & demo outreach (person 4)
**Purpose:** make the world real. Real counterparties, real contacts, realistic documents, and a safe demo path that shows we *can* call and email them.

- **R1 Real counterparties:** choose a real lane cluster (for example Northern California ↔ Nevada produce). Pull about 300 real carriers from FMCSA via the Apify SAFER actor: legal name, USDOT/MC, authority status, registered phone and address, fleet size. Scrape about 40 real shippers (public company sites, maps listings) via Apify: name, address, website, the AP or logistics page. Store them in Postgres `company` with `source_url` and `scraped_at`. **Decided:** the home brokerage is fictional. We simulate a brokerage's internals under the name **Golden Gate Freight** (used in materials, the voice persona and the UI). Every counterparty is real.
- **R2 Contact enrichment waterfall (sponsor-first).** Every result stores `source`, `source_url`, `confidence` and `fetched_at`. Everything is cached, and the hero counterparties are pre-enriched by C1.
  1. **FMCSA via Apify** (`scrapesage/fmcsa-trucking-scraper`, `makework36/fmcsa-trucking-api` or similar; pick the one that returns phone and email): the carrier's registered phone, email, address and officers. These are public records and our highest-trust source for verification calls (P-008).
  2. **Apify Contact Details Scraper** (`vdrmota/contact-info-scraper`, about 65k users): crawl each counterparty's website for public emails, phones and social profiles. Use the Google Maps lead actors for shippers' main lines.
  3. **context.dev** (credits from the event): company data (description, address, socials, industry), **people enrichment** from identity clues (name plus company leads to the contact's details), and company news for Risk. It shares one key with scraping (`/v1/web/scrape`, with JSON-schema extraction for AP or contact pages).
  4. **Our own contact-data API key** (provided by the team) fills the gaps: direct dials and named people at shippers' AP departments. Apollo is optional if this covers it.
  - Merge rule: prefer FMCSA, then our key, then context.dev, then website scrape. Conflicting values are kept as alternates, and the disagreement is shown (Risk uses it as a signal).
  - **Display: unmasked on the live site.** Showing that we find real contacts and can operate in a real business is the point. Outreach stays demo-routed (R5).
- **R3 Realistic synthetic documents:** generate invoices, rate confirmations, BOLs and PODs as PDFs, plus email threads, all against the real counterparties (real names, MC numbers, addresses and lanes) with synthetic loads, amounts and line items. Mark them with a small "SIMULATED" footer. Planted errors (padded detention, lumper without a receipt, duplicates, rate above contract, bank-change fraud, double-brokering, short-pay, misapplied payment) go in an `answer_key`, with clean decoys mixed in.
- **R4 Simulator and ingest:** run a continuous event stream with a 3-month fast-forward history at boot. Ingest normalizes, timestamps and dedupes events, writes Postgres, publishes to Redis and opens SpacetimeDB cases. Train an anomaly model (IsolationForest) on clean history as the `score_anomaly` tool. The evaluator joins outcomes to the answer key and writes `metric` rows.
- **R5 Outreach safety (hard rule):** `OUTREACH_MODE=demo` is the only mode at the event. Real contacts are resolved and shown as `intended_to`. The actual dial and send target is replaced with `DEMO_ALLOWLIST` (team phones and team inbox). Code refuses any target not on the allowlist, even if someone flips the flag. Every call and email record stores both `intended_to` and `routed_to`.
- **R6 Demo console (UI by W2, backend by W4):**
  1. Pick a case, or type a scenario that the harness turns into a case.
  2. Choose the channel (call or email) and the teammate playing the counterparty.
  3. **Start**. The Voice Desk places a real Cartesia call to the teammate, using the real counterparty's context (company, load, claim) as dynamic variables.
  4. The audience hears the back-and-forth on speaker. The transcript appears in the case (live if Cartesia exposes it, otherwise right after the call). The findings resume the A2A task.
  5. For email, the message lands in the team inbox, and replying resumes the task.
  6. Buttons for the preset scripts: PHONE (detention), EMAIL (bank change), SHORT-PAY (collections).
- **R7 Integrations (as tools; section 7.6):** Cartesia persona configs and phone numbers, Apify, context.dev, our contact-data key, CRS, Nessie and email. Each tool has `MOCK_INTEGRATIONS=1` so other workstreams aren't blocked.
- **R8 Deliverables:** the pitch, the 3-minute video, Devpost, a README covering real vs simulated vs demo-routed data, the Cartesia features used, and the published Apify actor.

Done when: the counterparty panel shows real enriched data for every hero case. The demo console runs PHONE and EMAIL end to end. T9, T10 and T16 pass.

## 7. Interface contracts (frozen Sat 3:00 AM)

### 7.1 Harness API (W1 provides, W3 and W4 consume)
Base `http://asus:8000`. Auth: `Authorization: Bearer <agent capability token>`.

```http
POST /v1/chat/completions          # OpenAI-compatible
{
  "model": "auto",                  # or a pinned model id; "auto" honours dashboard pinning
  "messages": [...],
  "tools": [...],                   # optional, OpenAI tool schema
  "response_format": {"type":"json_schema","json_schema":{...}},   # optional
  "max_tokens": 512,
  "metadata": {
    "agent": "risk", "case_id": "CASE-4821", "task_id": "…",
    "task_type": "investigate_charge", "priority": "P0",
    "stakes": "high",               # high → ensemble
    "max_latency_ms": 4000, "untrusted_spans": [[120, 560]]
  }
}
→ 200 OpenAI-shaped response plus
  "harness": {"model_used":"…","route_reason":"…","ensemble":{"models":[…],"agreement":true,"votes":[…]},
              "latency_ms":812,"queue_ms":40}
→ 503 {"error":"unavailable"}       # agent uses its deterministic fallback

POST /v1/sandbox/exec
{ "agent":"controller","task_id":"…","tool":"spreadsheet.apply"|"python"|"pdf.extract",
  "input":{…}, "files":["case://CASE-4821/invoice.pdf"], "timeout_s":20, "network":false }
→ {"ok":true,"output":{…},"files_out":["case://CASE-4821/out/recon.xlsx"],"stdout":"…","limits_hit":null}

GET /v1/models                      # pool + health + current load
```
Mock by Sat 3 AM: return canned JSON matching the schema; `model_used: "mock"`.

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
- Agent Card URLs: `http://asus:91xx/.well-known/agent-card.json`.

### 7.3 Events: Redis Streams (W4 produces, W3 router consumes)
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
- `agent_config(agent pk, model:"auto"|id, enabled)`
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

Mock by Sat 3 AM: the tables exist, the reducers insert rows without validation, and the dashboard shows raw tables.

### 7.5 Voice Desk ↔ Cartesia (W3 code, W4 config)
- An agent delegates `place_call` in park mode with `{purpose: verification|collections|approvals|cash_brief, counterparty_id, questions[], facts{}}`. Allowed purposes per caller: Verification → verification, AR → collections, Controller → approvals, CFO → cash_brief.
- The number comes from the verified directory only: the `contact` or `company` record with a source (FMCSA, our contact-data key, context.dev or website). Policy checks: P-008, P-011, quiet hours, ≤ 1 attempt per counterparty per hour.
- **Demo routing:** the Voice Desk sets `intended_to` = the real number and `routed_to` = the demo allowlist number, and dials only `routed_to`. Both are written to `call` and shown in the UI.
- `POST https://api.cartesia.ai/agents/calls`, headers `X-API-Key`, `Cartesia-Version: 2026-08-14`, body `{agent_id, from_number_id, ringing_timeout_seconds:25, outbound_calls:[{to_number, dynamic_variables:{case_id, task_id, call_token, …facts}}]}`.
- In-call webhook tools hit `https://<tunnel>/voice/tools/<name>`. They are read-only: `get_load_facts, get_payment_status, get_invoice_status, get_case_summary`. The only writes allowed append evidence or events: `record_promise_to_pay, open_dispute`.
- System tools: `end_call`, `voicemail_detection`, `transfer_to_number`.
- Call end: a webhook (check `x-webhook-secret`), or poll `GET /agents/calls/{id}`. Then transcript → redact PII → harness extraction against the persona's schema → `call_result` artifact → the parent resumes. No answer returns `{reached:false}`.
- Every persona opens with AI disclosure plus the reason for the call. Custom metrics: `disclosure_given`, `claim_confirmed`.

### 7.6 Integration tools (W4 implements as Python functions in `integrations/`, W3 calls them)
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
Trigger: the router (or the agent's own runtime) sends `review_output` to the Validator for every terminal task from AP, AR, Controller and CFO, and `review_flag` for every new `flag`. Delivery is park mode, so it never blocks the case.
```python
ReviewInput  = {target_task_id, case_id, agent, artifact, change_set_ids, a2a_history_ref, tool_log_ref, flag?}
ReviewOutput = {verdict:"pass|issue", checks:[{name, ok, expected, actual}],
                friction:[{kind:"retry|reject|policy_deny|timeout|missing_doc|redelegation|human_override|flag", detail, cost_s}],
                lessons:[{agent, type:"memory|instruction", scope, text_or_diff, confidence, evidence_refs}],
                actions:["reopen_case"|"rerun_task"|"none"]}
```
Rules:
- Deterministic checks run first. The LLM review uses the high-stakes ensemble.
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

| Agent | Port | Skills (Agent Card) | Key tools | May delegate to | Hard limits |
|---|---|---|---|---|---|
| Carrier AP | 9101 | process_carrier_invoice, propose_payment, answer_payment_status | three_way_match, get_rate_con, get_pod, propose_payment | Risk, Controller, Verification | Proposes only; never pays |
| Shipper AR | 9102 | reconcile_customer_payment, chase_overdue, set_credit_limit | match_payment (Nessie), crs_business_credit | Controller, Verification, Voice Desk (collections), CFO | No write-offs |
| CFO | 9103 | assess_financial_impact, cash_forecast | forecast_cash, load_margin | AR, AP, Controller | Read-only on the ledger |
| Risk & Compliance | 9104 | investigate_charge, screen_bank_change, screen_counterparty | score_anomaly, detect_duplicates, fmcsa_lookup, crs_ofac, gps_dwell | Verification, Controller | Recommends holds; can't place them |
| Controller | 9105 | review_hold, check_already_paid, reconcile, approve_entry | place_hold, release_hold, post_journal_entry, request_approval | CFO, Verification, Voice Desk (approvals) | Only ledger writer; >$5k release needs a human |
| Verification (external checks) | 9106 | verify_claim, verify_bank_change, verify_dispute | compare_documents, send_email, crs_idv | Voice Desk (verification) | Contacts only verified channels |
| Validator | 9108 | review_output, review_flag | recompute/tie-out checks, history and tool-log reader, upsert_memory, publish_instruction_version | none (writes memory and instructions only) | Can't change policy, limits, permissions or allowlists; can't touch the ledger |
| Voice Desk | 9107 | place_call, handle_inbound_call | Cartesia, transcript extraction | Router (inbound dispute → event) | Directory numbers only; demo allowlist dialing only; AI disclosure |

Every agent folder contains `card.py`, `instructions.md`, `knowledge/`, `tools.py` and `main.py`.

## 9. Controls (deterministic, in code and again in reducers)
P-001 invoice > contract + 2% → hold the delta · P-002 no payment on a held invoice · P-003 payment > $5,000 → human approval · P-004 bank change → out-of-band verification on the directory or FMCSA phone, plus IDV for owner-operators · P-005 requester ≠ approver · P-006 journal debits = credits, period open · P-007 duplicate (carrier, load, amount within 14 days) → reject · P-008 external contact only to verified channels · P-009 ensemble disagreement on a high-stakes call → human approval · P-010 untrusted text can't authorize an action · P-011 at the event, outreach goes only to `DEMO_ALLOWLIST` targets · P-012 the Validator and memory system can't change policy, limits, permissions or allowlists; instruction changes are versioned and revertible.

## 10. Hero case data (fixed IDs; W4 seeds them, everyone tests with them)
- `CASE-4821` / `LOAD-4821`, lane Stockton CA → Reno NV, carrier `C-311` "Sierra Haul LLC" (USDOT 3141592, owner-operator), shipper `S-07` "Bay Produce Co".
- Contract and rate con **$1,850**. Invoice **$2,150** = $1,850 + **$300 detention (2 h)**, no receipt.
- GPS geofence at the receiver: arrived 14:02, departed 14:49 (**47 min**). Free time 2 h, so no detention is owed.
- Second-chance replay: `LOAD-4877`, the same carrier and pattern, 2 sim-weeks later.
- Bank-change demo: carrier `C-77` "Rapid Ridge Transport" emails new bank details the day before a **$9,400** payment.

## 11. Acceptance tests (shared definition of done)
- T1: All 7 Agent Cards resolve and validate.
- T2: The hero case produces ≥ 5 A2A tasks under one context_id, each with an artifact, and the audit hash chain verifies.
- T3: Trigger to ring ≤ 15 s (measure 5 times).
- T4: 20 concurrent cases plus 1 waiting on a call: all finish.
- T5: `kill -9` Risk mid-case, restart: the case finishes, with no duplicate tasks.
- T6: An A→B→A delegation is refused and logged.
- T7: A $9.4k release waits for a human; the approval with its code goes through; the requester can't approve.
- T8: The precedent replay resolves with no call and cites the precedent.
- T9: A payload with a different phone number still dials the directory number; the mismatch is a risk signal.
- T10: The scoreboard shows precision and recall after 30 min of simulation.
- T11: Pin Risk to a specific model in the UI; the next call uses it.
- T12: A sandbox tool trying to reach the network is blocked and logged.
- T13: Every AP, AR, Controller and CFO output gets a `review` row within 30 s.
- T14: A seeded friction case (missing rate con, then re-request) produces an AP memory entry, and the next similar case uses it (the memory id appears on the task).
- T15: Clicking the hold cell shows blame back to the source PDF and page, the reading agent, the change set and the policy. A diff between two change sets is correct.
- T16: With `OUTREACH_MODE` forced to anything, a non-allowlisted number is refused, and the call record shows both `intended_to` and `routed_to`.
- T17: A human flag on a CFO forecast row is resolved by the Validator, with a visible diff and a re-run.

## 12. Timeline (PDT; deadline **Sun 9:00 AM**; judging 10:30 AM–12:30 PM)

| Time | Checkpoint | W1 Harness | W2 SpacetimeDB/UI | W3 A2A | W4 World/integrations |
|---|---|---|---|---|---|
| Fri 11:45 PM – Sat 3 AM | **C0: contracts frozen, mocks up** | Model serving up; mock API | Module with tables; raw UI | 7 services boot with cards; stub decisions; AP→Risk→Verification over A2A | Seeded world; 10 fixed events; Cartesia number provisioned; API keys |
| Sat 3 AM – 9 AM | **C1: spine end to end** | Router v1, structured output, P0 lane | Reducers with invariants; network + feed views | Park/resume, joins, reconciler, policy, audit to SpacetimeDB | Continuous generator, ingest → Redis, hero triggers, Apify + Nessie tools |
| Sat 9 AM – 3 PM | **C2: hero case live with a real call** | Ensemble, sandbox, telemetry to SpacetimeDB | Case drill-down, approvals inbox, ledger | Voice Desk + webhook gateway, precedents, "ask why" | Personas + webhook tools, CRS, email, anomaly model, answer key |
| Sat 3 PM – 9 PM | **C3: feature freeze** | Pinning from UI, fallback, injection quarantine | Scoreboard, harness panel, injector UI, polish | Crash recovery, T1–T9 green, inbound line | Evaluator, judge injector, history replay, Apify actor published |
| Sat 9 PM – Sun 3 AM | **C4: hardening** | Load test at 2 events/s | Performance, backup polling | Bug fixes only | Record backup video; write README and Devpost |
| Sun 3 AM – 9 AM | **C5: rehearse and submit** | All | All | All | Submit by **8:00 AM** (1 h buffer) |

Extra items added in v1.1: by C1, W4 has 50 real carriers and 10 shippers scraped and the enrichment waterfall working for the hero contacts, and W3 has the change-set writer. By C2: the demo console call loop, the Validator v1 (deterministic checks and memory), and the audit grid with blame. By C3: instruction versioning and revert, flags end to end, and the counterparty panel.

Integration checkpoints are hard: at each one, everyone stops for 15 minutes and runs the hero case together.

## 13. Risks and fallbacks
| Risk | Fallback |
|---|---|
| Local models are too slow under load | P0 lane, small model for the hot path, deterministic fast path, reduce background rate |
| Venue Wi-Fi or tunnel drops (Cartesia webhooks) | Phone hotspot, poll `GET /agents/calls/{id}`, pre-recorded backup video |
| SpacetimeDB is new to us (2.x API differs from older tutorials) | W2 keeps the mock schema working by C0; Postgres mirror for the audit as a backup |
| Phone number or Cartesia setup delay | Provision in the first hour; email path as the backup moment |
| An accidental real call or email | Allowlist enforced in code (T16), no real-recipient code path, test before every rehearsal |
| Enrichment credits or rate limits (Apify, context.dev, our own key) | Cache everything; pre-enrich the hero counterparties by C1; FMCSA phone as the fallback |
| The Validator writes bad memory | Memory is scoped and versioned, a human can revert it, and it never touches policy |
| Scope creep | Section 2 non-goals; feature freeze at C3 |

## 14. Repo and conventions
- Monorepo `freightops/`: `harness/ (W1)`, `spacetime/ + web/ (W2)`, `agents/ common/ router/ gateway/ (W3)`, `sim/ ingest/ integrations/ eval/ (W4)`, `docs/PRD.md` (this file), `docker-compose.yml`.
- Python 3.11+, `a2a-sdk==1.2.3`, Pydantic v2. Use `.env.example` and never commit keys (judges get the repo).
- Branch per workstream; merge to `main` at each checkpoint; `main` must always run `make demo`.
- Shared IDs: `CASE-####`, `LOAD-####`, `TASK-###` (display), carriers `C-###`, shippers `S-##`.

## 15. Open questions (owner to resolve, then update this doc)
Resolved in v1.2: the home company is fictional "Golden Gate Freight" with real counterparties; contacts are shown unmasked on the live site; enrichment is sponsor-first (Apify, context.dev) plus our own key.
1. ASUS hardware specs and which models fit; final model pool. — W1
2. Sandbox technology (Docker vs nsjail vs firejail) on the ASUS OS. — W1
3. Does the A2A runtime run the tool loop (current decision), or does the harness own the agent loop? — W1 + W3
4. Email provider for the team inbox and reply resume. — W4
5. Which FMCSA actor returns phone and email reliably; context.dev credit amount. — W4
6. Other sponsor credits worth using for data (check the live site and Slack). — W4

## Change log
- v1.0 (Fri 11:45 PM): initial ground truth.
- v1.1 (Sat 12:30 AM): dropped Fetch.ai and Coinbase. Person 4 = real-world data, enrichment and demo outreach. Demo-only outreach with intended_to and routed_to. Added the Validator agent (:9108) with memory and instruction learning, the Git-style audit with spreadsheet blame, diff and history, and human flags. Renamed the external-check agent from Validation to Verification.
- v1.2 (Sat 1:00 AM): fictional "Golden Gate Freight" with real counterparties; enrichment waterfall (FMCSA via Apify, our own key, context.dev, Apify website scrape); contacts unmasked on the live site.
