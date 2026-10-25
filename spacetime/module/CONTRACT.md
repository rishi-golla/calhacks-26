# W2 reducer contract

The module is pinned to SpacetimeDB 2.10.2. Build with `cd spacetime/module/spacetimedb && npm ci && npm run check && npm run build`. All business reducers take one argument, `payload: string`, containing a JSON object. HTTP callers send `[JSON.stringify(payload)]` to `POST /v1/database/freightops/call/<reducer>` with their own Bearer token. The reducer exports use snake_case names. Bindings may camel-case these names.

Every payload requires a unique `write_id`. Retry the **same payload and write_id** after a timeout. Same identity + write_id + same canonical payload is a no-op; changed content is rejected. A request that already matches stored state (`upsert_source_record` with an unchanged digest, `ensure_agent_defaults` with nothing missing) commits only its receipt, with no change set or audit event. Errors roll back the entire reducer. Codes rejected during an approval decision are the deliberate exception: the failed attempt is committed and the public approval `last_error` explains why it remains pending.

All legacy money fields (`amount`, `exposure_usd`, `debit`, `credit`, `paid_amount`, `contract_amount`) are **USD**, accept at most two decimals, and use integer cents for control checks and sums. `$9,400` is `9400`. Timestamps are UTC ISO strings. Missing optional strings are `""`; JSON-valued columns such as `scope`, `artifact_json`, `findings_json`, and `checks_json` are canonical JSON strings.

Authentication comes only from the actual SpacetimeDB sender identity. The init lifecycle provisions the database publisher as admin. Admin is an infrastructure role; it does not bypass financial role checks. Provision distinct service identities and a human identity. Roles: `admin`, `human`, `ingest`, `harness`, `evaluator`, `router`, `carrier_ap`, `shipper_ar`, `cfo`, `risk`, `controller`, `verification`, `voice_desk`, `validator`, `documents`. The `documents` role owns POD/document collection tasks and may delegate verification-purpose outreach to `voice_desk`; it has no financial-write permissions. Every reducer requires a provisioned, active identity except `set_view_epoch`, which any connected identity may call.

| Reducer | Role | Payload besides `write_id` |
|---|---|---|
| `grant_role` | admin | `identity` (64 hex), `role`, `actor`, `active` (default true) |
| `open_case` | ingest | `case_id` (not `HIST-`, reserved for imported history), `load_id` (optional until known), `title`, `priority` (P0–P3, default P2), `exposure_usd` (default 0); calling it again on an existing case refreshes title, priority and exposure only. Status and timestamps are kept, a set `load_id` is immutable, and a `human_owned` case rejects the refresh |
| `update_case` | controller, validator, human | `case_id`, `status` (`open`, `in_progress`, `resolved`, `human_owned`), `reason` |
| `upsert_invoice` | ingest | `id`, `case_id`, `carrier_id`, `load_id`, `amount`, `contract_amount`, `issued_at` (optional ISO); `paid_amount` is zero at ingest and cannot be overwritten |
| `set_period` | admin, human | `period` (YYYY-MM), `status` (`open` or `locked`) |
| `place_hold` | controller | `id`, `case_id`, `invoice_id`, `amount`, `reason_code` |
| `release_hold` | controller | `id` (hold ID), `amount` (optional, remaining amount), `approval_id` (required for any release once the original amounts of all holds ever placed on the hold's invoice, released ones included, total more than $5,000; `human_approval_id` alias accepted) |
| `propose_payment` | carrier_ap | `id`, `case_id`, `invoice_id`, `amount`, `approval_id` (required for any proposed portion of an invoice whose total exceeds $5,000), optional matching `carrier_id` |
| `request_journal_entry` | carrier_ap, shipper_ar, cfo, controller | `id`, `case_id`, `period`, `lines:[{account,debit,credit}]`, `memo` |
| `post_journal_entry` | controller | `id`, `request_id`; any supplied `lines`, `period`, `requested_by`, `approved_by` must match the stored request and actual caller |
| `request_approval` | controller | `id`, `case_id`, `task_id` (optional), `action` (`release_hold` or `propose_payment`), `target_id` (hold ID or future proposal ID), `invoice_id` (required for propose_payment; derived from hold for release_hold), `amount`, `approver` (human identity), `expires_in_seconds` (default 900) |
| `issue_approval_code` | admin | `approval_id`, `code` (6–64 characters), `expires_in_seconds` (default 300, max 900) |
| `decide_approval` | designated human | `id`, `decision` (`approved` or `rejected`), `code`; inspect approval state after the reducer succeeds |
| `upsert_task` | task's agent | `task_id`, `case_id`, `agent` (optional matching caller), `skill`, `parent_task_id` (optional), `a2a_state`, `business_status`, optional `outcome`, `confidence`, `model_used`, `display_id`, `artifact_json`, `evidence_refs`, `policy_ids`, `instr_version`, `memory_ids` |
| `record_delegation` | source agent | `id`, `case_id`, `parent_task_id`, `task_id` (new child), `to_agent`, `skill`; inserts the reserved child task atomically |
| `agent_heartbeat` | agent | optional matching `name`, `status`, `p50_ms`, `card_url`; task counts are derived from task state |
| `append_audit` | agent | `case_id`, optional `task_id`, `kind`, `reason_code`, `summary`, `evidence_refs`, `policy_ids`, `model_used` |
| `record_doc_read` | agent | `id`, `task_id`, `doc_ref`, `page`, `span`, `extracted_json`, optional `change_set_id` belonging to same task |
| `upsert_call` | voice_desk | `id`, `case_id`, `task_id`, `company_id`, optional `contact_id`, `routed_to`, `persona`, `direction`, `status`, `findings_json`, `transcript_redacted`, `recording_url`, `disclosure_given`, `team_demo_authorized` (default false; restricted below); intended number is resolved from directory |
| `set_demo_target` | admin | `phone` (E.164), `label`, `enabled` |
| `add_precedent` | controller | `id`, `carrier_id`, `claim_type`, `lane`, `pattern_json`, `outcome`, `source_case` |
| `log_llm_call` | harness | `id`, `agent` (nine agent IDs, `router`, or `ingest`), `model`, `latency_ms`, `tokens`, `route_reason`, `ensemble_agreement` |
| `record_dispatch` | router | `event_id`, `case_id`, `skill`, `agent`, `task_id`, `status` (`queued`, `accepted`, `triage`, `not_routed`, `duplicate`), `reason`, `queue_ms`, `policy_version`; transitions below |
| `enqueue_first_hop` | router | `event_id`, `case_id` (must exist and not be `human_owned`), `agent`, `skill` (owned by `agent`), `envelope` (TaskEnvelope object whose `case_id` and `task_type` match), `queue_ms`, `policy_version`, `reason`; writes the `first_hop` outbox row and a `queued` dispatch atomically |
| `set_metric` | evaluator | `name`, `value` |
| `set_agent_model` | human | `agent`, `model`, `enabled` (default true) |
| `ensure_agent_defaults` | admin | none; inserts `{model: auto, enabled: true}` for every agent missing from `agent_config` and leaves existing rows alone |
| `set_view_epoch` | anyone (any active principal, or an unprovisioned identity acting as `visitor:<first 12 hex of identity>`) | `since` (optional ISO timestamp, default now) upserts the `view_epoch` row; or `clear: true` (without `since`) deletes it. Clearing when no row exists writes only the receipt |
| `settle_cases` | admin | none; runs the case-settlement rule below over every case (one-time sweep for cases written before the rule). Each changed case gets its own change set and audit event on its case chain, in one transaction; the sweep itself and a no-op sweep write only receipts |
| `import_history_batch` | admin | `source`, and any of `companies`, `cases`, `invoices`, `ledger_entries`, `source_records` dated in the past under `HIST-` ids; see Imported history below |
| `remove_imported_history` | admin | `limit` (default 50, max 200 cases per call); removes imported (`HIST-`) rows only; see Imported history below |
| `upsert_forecast` | cfo | `id`, `case_id`, `period`, `category`, `amount` (signed), `confidence`, `assumptions_json` |
| `upsert_company` | ingest | `id`, `kind`, `name`, optional `usdot`, `mc`, `phone`, `website`, `source_url` |
| `upsert_source_record` | ingest | `id` (`case:<case_id>`, `company:<id>`, `contacts:<company_id>` or `dataset:meta`), `kind` (`case`, `company`, `contacts`, `dataset`, matching the id prefix), `record_json` (object, or array for `contacts`), `digest` (SHA-256 hex of `record_json` exactly as sent) |
| `upsert_contact` | ingest | `id`, `company_id`, `name`, optional `title`, `phone`, `email`, `linkedin_url`, `source` (`fmcsa`, `sec_edgar`, `own_key`, `context_dev`, `website`, `fixture`), `source_url`, `confidence`, `alternates_json` |
| `add_review` | validator | `id`, `target_task_id`, `case_id`, `verdict` (`pass` or `issue`), `checks_json`, `friction_json`, `actions` |
| `upsert_memory` | validator, human | `id`, `agent`, `scope` (object with carrier_id/lane/claim_type/skill only), `text`, `source_ref`, `active` |
| `publish_instruction_version` | validator, human | `agent`, `diff`, `reason`, `source_ref`, `expected_version`; compare-and-swap against latest version (0 initially); validator patches restricted to approved append-only advisory template |
| `revert_instruction_version` | human | `agent`, `version` (snapshot to restore, 0 for checked-in baseline), `reason`, `expected_version`; compare-and-swap creates a new compensating version |
| `raise_flag` | human | `id`, `target_ref`, `kind` (`wrong`, `unclear`, `policy`, `praise`, `other`), `note`, optional `case_id` |
| `resolve_flag` | validator | `id`, `status` (`reviewing`, `resolved`, `rejected`), `resolution_ref` |
| `claim_outbox` | router | `id`, optional `lease_seconds` (max 300) |
| `ack_outbox` | router | `id`, `success`, optional `error`; claim must belong to caller |

`commit_change_set` is an internal transaction helper, intentionally **not** an externally callable reducer: it hashes actual before/after rows generated by the business operation. Each mutation emits one change set, its cell changes, and an audit event atomically. Every change set has `hash_body`, canonical JSON whose SHA-256 equals `hash`; it includes `parent_hash`, author identity, and the exact sorted cell contents. Audit events use the same algorithm with `prev_hash`. Chains are scoped by `case_id`, with an additional system chain for `case_id = ""`; each scope has its own sequential `sequence`. Verify and walk parents within each scope. This keeps case diffs free of unrelated case changes. No exposed reducer updates or deletes audit, cell-change, change-set, journal-entry, or review history.

Financial sheets use `holds`, `payments`, `gl`, `ap_subledger`, `ar_subledger`, and `cash_forecast`; other tables also receive cell history under their table name. Each posted journal line is projected into `gl` under row key `<entry ID>:<zero-based line index>`, with separate `account`, `debit`, `credit`, period, memo and author cells. Journal header/line-array history remains under `ledger_entry`. Receivable lines are also projected into `ar_subledger` for normalized account names `accounts_receivable`, `account_receivable`, `ar`, `a/r`, `receivables`, `trade_receivables`. Payable equivalents (`accounts_payable`, `account_payable`, `ap`, `a/p`, `payables`, `trade_payables`) also appear in `ap_subledger`. Normalization lowercases and replaces spaces/hyphens with underscores.

Approval codes, code hashes, receipts, and chain heads are in **private** tables. Codes are issued out of band by the trusted admin/gateway; no delivery side effect occurs inside a reducer. Codes expire, lock after five failed attempts, and are consumed once when the human decides. The approved authorization is then consumed once by the matching financial action, bound to target ID, invoice ID, amount, case and action. Invoice carrier/load/case are immutable, so an approval cannot be redirected to a different beneficiary. Caller-supplied `code_ok` has no effect.

P-003 for releases is measured per invoice: once the original amounts of every hold ever placed on an invoice (released holds included) total more than $5,000, **every release** on that invoice requires human approval, even a partial one below the threshold. Holds are never deleted and their amounts never shrink, so at most $5,000 of an invoice can be released without a human, whether the dispute is split across several holds, held and released in turn, or partially released. An invoice whose total exceeds $5,000 similarly requires human approval for **every proposed payment portion**. Each approval still binds the exact partial amount, preventing split releases or differently sized split proposals from bypassing the limit. Task `artifact_json` must contain a JSON object; W3 retains ownership of the fields and validates its shared `ResultArtifact` contract.

Agent-owned tasks cannot be reparented or reopened after terminal state. Terminal output, artifact, evidence and provenance are immutable, so revised output requires a new task and receives its own Validator request. Every supplied audit `task_id` must belong to the authenticated actor and action's case (delegations validate the owned parent and reserved child). Delegations require an existing owned parent, an allowlisted edge and skill, no repeated agent in the ancestor path, depth ≤3 and fanout ≤3. Completed financial tasks and human flags create deterministic `routing_outbox` rows for the W3 router. Claim/ack with leases supports crash recovery; the router must use the outbox ID as its A2A idempotency key. W2 persists delivery work; W3 must run the delivery worker. Rejected reducer transactions cannot persist audit rows; sanitized denials are written to server logs while their attempted mutations roll back.

Human takeover blocks new agent business mutations and delegation on that case. Review/audit evidence and task completion may still be recorded so in-flight work can report its outcome. Only a human can release human ownership.

New calls normally allow one attempt per company per hour, including failed attempts. An authenticated Voice service may send `team_demo_authorized: true` only after validating the server-owned, expiring operator grant for that exact task and destination. W2 accepts the exception only for an owned `place_call` task in case `SYN-CARTESIA-TEAM-CALL`, company `DEMO-TEAM`, outbound direction and verification persona. The company must retain `source_url = operator://explicit-team-demo-destination`, and its phone, the directory-resolved intended phone and the routed phone must all match an enabled demo target. A new authorized call must start `queued`; any existing `queued`, `ringing` or `in_progress` call for that company blocks it atomically regardless of age. Completed, no-answer, failed or cancelled calls allow another explicitly authorized demo immediately. Same-call updates remain idempotent and cannot restart terminal calls. New accepted demo calls record audit reason `operator_team_demo_call`. The flag is a trusted service assertion, never model or browser input; directory, ownership, disabled-agent and allowlist controls remain in force. Ordinary calls and calls without this authorization retain the hourly limit. No database schema or A2A envelope fields change.

Outbox kinds are `review_output` and `review_flag` for the Validator, `review_followup` for the original task's agent, and `first_hop` for the agent the router selected (see below). Dispatch using the row's `target_agent`. Followup payloads include `target_task_id`, `case_id`, `agent`, `actions` (`reopen_case` / `rerun_task`), and `review_id`; only one corrective request is queued per original task to bound retry loops. Human-owned cases do not receive automatic corrective followups. Acknowledge delivery after successful handoff; revised output and execution status belong to a new task. Delivery acknowledgement is not evidence that execution completed.

Instruction history is append-only except for the active marker. A normal row (`reverts_version = 0`) contains a diff relative to the preceding effective version. A revert row creates a new version whose effective content is the **fully reconstructed content of `reverts_version`**. Its copied `diff` is display context only; W3 must not apply that copied patch to the latest text. Reconstruct versions in ascending order, preserving the effective text for each version, and use the target snapshot for revert rows. Policy code, approval thresholds, tool permissions and delegation allowlists remain outside this version stream and are never mutable through lesson reducers.

### Instruction learning and rollback

Validator publishes code-generated evidence/cents reminders after deterministic review failures.
The patch and base version are durably checkpointed before publication. The reducer rejects a
stale or missing `expected_version`; it never rebases automatically over human edits. When
the version advanced between planning and publication, Validator re-plans once from the new
history: an already-present reminder is reported as `superseded`, and the review still resolves
its flag. Replays with the same write ID retain the original receipt. New tasks load the reconstructed latest
instructions; already-running tasks retain their checkpointed instruction version.

Automated patches can only append the approved advisory template. Model output, flag text and
source documents cannot delete base instructions or introduce new policy/permission text.
Human rollback restores a selected snapshot (including version 0) as a new immutable version,
with the same concurrency check. Financial policies and role/delegation permissions are unchanged.

Deploy the updated module before restarting Validator or using the updated rollback UI. Older
modules do not enforce the new precondition. This change adds no tables or A2A envelope fields.

### Intake dispatch telemetry

`dispatch` is a public table keyed by `event_id`, with the `record_dispatch` fields
above and a server-generated `ts` for the latest observation. Only the `router`
role can write it. Queued, accepted and duplicate records require a case and a
matching agent/skill; accepted records also require the accepted remote A2A task ID,
queued records must not carry one, and duplicates may leave it empty when the router
no longer knows it. These references need not already exist in SpacetimeDB; this
reducer records intake evidence and grants no task ownership. Triage and not-routed
records may leave unresolved references empty.

Exact `write_id` retries are no-ops. Once an event is routed (queued, accepted or
duplicate) its case/agent/skill are immutable, it cannot downgrade to triage, and
later observations retain the original queue time and policy version. `queued →
accepted` sets the task; `accepted → queued` is rejected. A duplicate observation of
a still-queued event leaves it `queued`, because its outbox row will still be
delivered. A new observation of an accepted event becomes `duplicate` and keeps its
task, so a duplicate sent without `task_id` inherits it; a supplied task must match.
A separate duplicate event (its own `event_id`) gets its own `duplicate` row, and its
task may be empty. A triage record can later be updated after resolution. `ts` changes on each new observation; this is one row
per event, not a retry-count table. Changes also enter the standard audit trail.

The repository router writes before Redis acknowledgment when `SPACETIME_DISABLED=0`,
using its `SPACETIME_TOKEN_ROUTER` credential (or the existing token fallback).
Failure leaves the event pending; A2A retries retain their event idempotency key.
`gps.ping` is acknowledged without a dispatch record (high-volume telemetry is not
audited); unknown or invalid events go to triage/dead letter. The router's write id is
`dispatch:<redis entry id>:<status>`, so an ambiguous retry is a reducer no-op rather
than a `duplicate`. `queue_ms` measures Redis entry creation to processing start. Model-call telemetry
remains separate. The dashboard's **Intake & models → Live intake** panel subscribes
to `dispatch` and supports status filtering, including events without a case.

Deployment order: the database owner republishes the additive module, then deploy
this dashboard and router together. From the repository root, with the owner's login:

```sh
spacetime publish --module-path spacetime/module/spacetimedb --server https://maincloud.spacetimedb.com --delete-data=never at350-freightops
```

The module change does not enable telemetry on a disabled router or deploy the UI.

### First hop through the outbox

`enqueue_first_hop` replaces the router's direct A2A push. In one transaction it checks
the case exists and is not `human_owned`, that `skill` belongs to `agent`, and that the
envelope describes the same case and skill. It then inserts `routing_outbox` row
`first_hop:<event_id>` (`kind = first_hop`, `target_agent = agent`, `task_id = ""`,
`payload_json = canonical({event_id, case_id, skill, agent, envelope, queue_ms,
policy_version})`) and upserts the event's `dispatch` row as `queued` with an empty task.
The change set and audit event chain under the case. Use `first_hop:<event_id>` as the
write ID and resend the identical payload on retry: an identical replay is a no-op and
changed contents are rejected. Under another write ID, identical contents re-observe the
queued event without a second outbox row; different contents, or an event already
accepted, are rejected.

The dispatch worker claims the row with `claim_outbox`, rebuilds the TaskEnvelope from
`payload_json.envelope`, sends it with idempotency key `event:<event_id>`, records
`record_dispatch` with `status = accepted` and the task ID, then calls `ack_outbox`.
`claim_outbox` and `ack_outbox` are unchanged and kind-agnostic.

### Source records

`source_record` is a public table, one row per record agents read in place of
`source.json`: `id` (as above), `kind`, `case_id` (derived from the id for `kind = case`,
otherwise `""`), `record_json`, `digest`, `updated_at`. `upsert_source_record` parses
`record_json` and requires an object, or an array for `contacts`. It also recomputes
SHA-256 over the exact UTF-8 string and rejects a mismatch. It does not re-canonicalize,
so the sender's serialization is authoritative. A supplied `case_id` must equal the
derived one. Case records chain under their case and the others under the system chain.
An unchanged digest is a no-op with no change set. Source records may be written before
`open_case` and are accepted for `human_owned` cases, since they are evidence, not agent
decisions. The data is synthetic and deliberately unredacted.

### Agent defaults

`init` seeds `agent_config` once. A database initialised before an agent joined `AGENTS`
lacks that row; `ensure_agent_defaults` (admin) inserts the missing defaults. It is
idempotent and never re-enables a disabled agent or resets a chosen model, so
provisioning can call it after every publish.

### Case settlement

Case status follows the work on the case. At the end of `upsert_task`, `record_delegation`,
`enqueue_first_hop`, `record_dispatch`, `ack_outbox`, `add_review`, `request_approval`,
`decide_approval`, `raise_flag` and `resolve_flag`, the module re-derives the status of the
affected case inside the same transaction:

- `human_owned` cases, and cases with no task yet (opened by ingest, first hop not delivered),
  are left alone, except a case the router dispatched only as a `duplicate` (below).
- **Duplicate only**: a case with no task, at least one `duplicate` dispatch row, and nothing
  delivered on it (no `queued` or `accepted` dispatch row, and no duplicate keeping a task that is
  not another case's) is finished work, for example a re-numbered invoice case (`CASE-<load>-R`)
  whose invoice P-007 blocked. It settles by the rules below as if its tasks were all complete,
  normally `resolved`; that change set's audit event has `reason_code = duplicate` and summary
  `duplicate: <the latest duplicate row's reason>` unless the caller supplied its own. A P-007
  duplicate of an accepted event carries the original case's task and still counts as nothing
  delivered here; the task is another case's when its mirror is, or, before the mirror arrives,
  when another case's `accepted` dispatch row carries it. A re-observed original turns its own
  row into a duplicate that keeps its task, so a duplicate row elsewhere does not count. If new
  work is later routed to such a case while it is resolved, it goes back to `open`, awaiting its
  first task: a first hop through the outbox, or a newly recorded `queued`/`accepted` dispatch row
  with direct A2A first hops. A re-observation of an existing row is not new work. A task reopens
  it as usual. When a duplicate cannot yet tell whose task it carries (no mirror and no accepted
  row elsewhere), its case stays as is; once that task's first `upsert_task` names its own case,
  every other case holding a `duplicate` row with that task is re-settled in its own change set.
- **Active** (`in_progress`): a task is non-terminal (`submitted`, `working`, `input-required`,
  `auth-required`), a `routing_outbox` row for the case is not `done`, an approval is `pending`
  and unexpired, or a flag is `open` or `reviewing`.
- Otherwise **`open`** when a task ended `failed`, `canceled` or `rejected`, or its latest review
  verdict is `issue`, and no later `completed` task for the same agent and skill (without an issue
  review) supersedes it. Such a case needs attention.
- Otherwise **`resolved`**, with `resolved_at` set. A proposed but unpaid payment still counts:
  the agent work is done and `payment_proposal.status` keeps its own state.

A resolved case changes only when the mutation creates new work: a new task, delegation, first
hop, approval request or flag, or an `issue` review. Leaving `resolved` clears `resolved_at`.
An explicit `update_case(resolved)` is therefore not undone by the caller's own task finishing,
or by the reviews it enqueues; a later new task (for example the Validator's review task) or an
issue does reopen it. A `record_dispatch` observation is never new work. A status change is
captured as `case` cells in the triggering mutation's change set, under the case chain; nothing is
written when the status is unchanged. `update_case` remains the explicit override. No tables or
indexes change; the duplicate rule reads `dispatch` through its existing `case_id` index.
An approval that expires undecided stops counting as active, but expiry alone triggers no reducer;
the requesting task normally settles the case when it finishes, and `settle_cases` can be re-run.

### Start fresh view boundary

`view_epoch` is a public singleton table: at most one row, `id = "current"`, with `since`
(the dashboard hides activity before it), `set_by` (the caller's actor, e.g. `human:operator` or
`visitor:0123456789ab`)
and `set_at`. Both timestamps use the module convention (`2026-10-24T09:30:00.000Z`); a
supplied `since` must parse as a date and is normalised to that form. No row means no boundary.
`set_view_epoch` is open to every dashboard viewer: it only moves a view boundary, so it does not
require a role. A deactivated principal is still refused, and the caller does not become
provisioned. It is accountable like any write: it emits a change set (sheet `view_epoch`, row
`current`) and an audit event on the system chain. It deletes nothing else; memory, lessons,
precedents, counterparties and the ledger are untouched. The evaluator reads the row and scores
only loads whose earliest case `opened_at` is at or after `since`, and counts only calls started
since then. Publish the module before deploying a dashboard that subscribes to `view_epoch`.

### Imported history

`import_history_batch` (admin) writes a brokerage's books from before FreightLine ran, so the dashboard opens on a
company that has been operating: resolved cases, their carrier invoices, posted journals, case files and the
counterparties they name, all dated in the past. It is the one path that stores caller-supplied past timestamps, and the one financial write an admin makes: a narrow, deliberate exception to "admin does not bypass financial role checks", bounded to `HIST-` rows.
`scripts/seed_history.py` generates the rows (sim/history.py) and sends them; nothing calls a model or an agent.

Payload: `write_id`, `source` (a short tag, recorded in each change set's summary), and any of `companies`,
`cases`, `invoices`, `ledger_entries`, `source_records` (1 to 2,000 rows in all, within the 256 KiB payload cap).

- **Admin only.** Any other role, or an unprovisioned identity, is refused.
- **Marked.** Case ids and journal ids must start with `HIST-`; invoices, journals and source records must belong to
  a `HIST-` case that exists or arrives earlier in the same batch (journals may also have no case, for the opening
  balance). Source record ids are `case:HIST-...` or the import's manifest `dataset:history`. `open_case` refuses
  `HIST-` ids, so live intake can never write into the namespace.
- **History, not work.** Cases are always `resolved`, with `opened_at <= resolved_at`. Nothing writes task,
  dispatch, delegation, review, outbox, hold, payment or approval rows, so settlement never touches an imported case.
- **Never the future.** Every `opened_at`, `resolved_at`, `issued_at`, `posted_at` and `updated_at` must be at or
  before the reducer's own time. A journal's `period` must be the month of its `posted_at`, and a `locked` period
  refuses imported journals (P-006). Journals go through the same line validation as every posting: 2 to 200 lines,
  each exactly one positive debit or credit, debits equal to credits in cents.
- **Idempotent.** The batch's write id makes an exact replay a no-op, as everywhere. A row that already exists with
  the same caller-supplied contents is skipped, so a replay under new write ids (another batch size) writes nothing;
  different contents are refused. An existing company is left alone: the live directory wins.
- **Audited.** Each case's rows land in one change set and audit event on that case's own chain (sheets `case`,
  `ap_subledger`, `ledger_entry`, `gl`, `ar_subledger`, `source_record`), and the opening balance and new companies on
  the system chain; the change set's action and audit kind are `import_history_batch`, reason `imported_history`.
  Journals carry `requested_by = import:history` and the admin as `approved_by`, with no journal request.

`remove_imported_history` (admin; `limit`, default 50, at most 200) is the documented way to take it back out. Each
call removes up to `limit` imported cases with their invoices, journals and source record, in a change set on each
case's chain that keeps every deleted value; once none is left it also removes the `HIST-` journals with no case and
the manifest. It never touches a row without the prefix, refuses a case that has any task, dispatch, hold, payment,
approval or flag on it, and leaves companies alone. This is the only reducer that deletes journal rows, and only
imported ones; audit, cell-change and change-set history is never deleted.

The dashboard treats imported rows as the company's past: "Start fresh" keeps them in view (Finance shows the
imported books plus live activity since the epoch), agent metrics leave them out, and the case page labels them
"Imported history" with no trace graph. Publish the module (additive: two reducers, no schema change) before running
the seed; the Maincloud runbook is in the pull request and `scripts/seed_history.py --help`.
