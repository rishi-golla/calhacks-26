# Validator instruction publication deployment

Validator now publishes the existing code-generated evidence/exact-cent reminder after a deterministic review failure. This is bounded append-only learning, not unrestricted model-written instruction editing. Policies, approval limits, role permissions, and delegation rules remain outside instruction history.

## Review engine (October 24, 2026)

`agents/validator/tools.py::deterministic_review` tests each finance artifact in both directions against the same source case the agent read plus the case's reducer rows (`hold`, `payment_proposal`, `approval`, `ledger_entry`, `journal_request`, `cash_forecast`).

- **Wrong approvals.** An `approve` must be clear of every settlement blocker computed with the agents' own helpers (`three_way_match`, `audit_charges`, `check_packet`, `duplicate_invoices`): contract tolerance (P-001), line totals (P-010), unsupported accessorials, duplicates (P-007), missing or unsigned documents, carrier identity (P-002), bank change (P-004), active hold (P-002). It must leave a payment proposal for invoice minus paid, bound to the same invoice, and above $5,000 a decided human approval with separation of duties (P-003, P-005).
- **Wrong holds.** A `hold`, `reject` or `unverified` must name a blocker the source actually shows. Holding a clean or decoy invoice fails `decision_justified`. Controller holds must equal the disputed delta or unsupported amount, or the full invoice on a bank change, and must leave a matching hold row.
- **AR and CFO.** Cash application is recomputed per deposit and the outcome must follow the balance sign; matched deposits must belong to this shipper; receivable journals must balance at the deposit amount. CFO margins, collections, payables and net cash are recomputed and forecast rows must equal them.
- **Books.** Every review also checks that case ledger entries balance (P-006) and that no open proposal pays an invoice under an active hold (P-002). These are advisory because they belong to the case, not to the agent under review; rerunning an unrelated agent cannot fix them.
- **Severity.** Each check carries `severity` (`issue` or `advisory`) and `basis` (policy or principle). Only `issue` failures set the verdict, schedule `rerun_task`, and create a lesson. Advisories (`short_pay_explained`, `net_cash_nonnegative`, books-level invariants, unrecorded forecast receipts, unanchored model commentary) are stored in `checks_json` for the dashboard.
- **Model pass.** The reviewer model cannot change a verdict. Its issues are kept as an `issue` check only when they name a failed issue-severity check (adding explanation to an established failure); everything else, including evidence mentions and advisory restatements, is recorded as advisory. `common/providers.py` adds a review-specific instruction for `purpose="review"`.
- **Live row shapes.** Journal lines from the SQL API arrive positionally (`[account, debit, credit]`); the review normalizes them before summing, as `common/spacetime_client.py` does for preflight.
- **Lessons.** Memory text is code-generated from the failed checks (name, expected, actual, basis), bounded to 1,000 characters, never model or document text. The instruction patch is unchanged: the reducer accepts only the approved append-only reminder (`validateAutomatedInstruction`), so richer guidance lives in scoped memory.

`agents/validator/instructions.md` (the version-0 baseline) was reworded to describe the two-sided review. Instruction versions are diffs over that baseline, so if any `instruction_version` rows exist for `agent = validator`, roll them back to baseline before deploying this text or reconstruction will fail with a context mismatch. No validator versions had been published as of this change.

## Shown work (October 24, 2026)

Every finance agent attaches `findings.workings`, a list of re-runnable steps (`common/workings.py`): label, formula, named inputs, result, unit, the evidence refs the inputs came from, and the policy or principle applied. Carrier Settlement shows the three-way match, tolerance ceiling, line-item sum and open balance; Accessorial Audit shows GPS dwell minus free time, supportable hours and each unsupported charge; Settlement Approvals shows whether a hold is required and the amount held, or the journal debits and credits; Billing & Collections shows cash applied per deposit and the open balance; Load Profitability shows margins, collections, payables and net cash. The Validator attaches its own recomputation as `work` on each money check, so a reviewer sees the agent's arithmetic and the independent arithmetic side by side. Workings are evidence, not authorization: nothing reads them to make a decision.

The dashboard renders workings with substituted values and makes every `doc:` and `mail:` evidence ref open the actual PDF or email through `web/app/api/evidence/[ref]` (read-only, path-validated, served from `DOCS_DIR` and `MAIL_DIR`, defaulting to `SIM_DATA_DIR/docs` and `SIM_DATA_DIR/mail`). The dashboard host must see the same data directory the simulator wrote. The route answers only requests whose `Sec-Fetch-Site` is `same-origin` or `none` (the dashboard's own pages and a person's address bar), which stops scripted scraping and hotlinking but is not authentication: the dashboard has no login, so run it on a trusted network, and set `EVIDENCE_PREVIEWS=off` to disable the route entirely.

Evaluation: `python -m harness.tests.validator_eval` runs every simulator scenario offline, reviews the real agent artifact, then mutates it (flipped outcome, one-cent shift, dropped evidence, missing approval) and reports catch rate and false-alarm rate per scenario.

## Deployment order

1. Publish the updated SpacetimeDB module with `--delete-data=never`. Both instruction reducers now require `expected_version`; deploy the module before updated callers.
2. Restart the Python agents so Validator uses the publication workflow and instruction reconstruction supports rollback to baseline.
3. Rebuild/restart the dashboard for version-checked rollback controls.
4. Run `common.review_worker` with the existing router identity and the same runtime database/configuration as the agents. This delivers queued reviews and may resume existing work.

No schema or A2A envelope changes are required. New tasks read the latest effective instructions; in-flight tasks retain their checkpointed settings. Publication plans and write IDs are durable. Stale updates fail instead of rebasing over a newer edit.

## Deployment performed on October 24, 2026

- Published to Maincloud `at350-freightops`, owned by Alan's `at350` account, without deleting data.
- Published to local `freightops-a2a-live` at `http://127.0.0.1:3000`, without deleting data.
- Applied the runtime changes to the existing `codex/voice-load-context` worktree and restarted all nine agents, gateway, and production dashboard.
- All nine `/readyz` endpoints returned live/ready; gateway and dashboard returned HTTP 200.
- Started the missing review worker; 24 queued reviews were acknowledged as delivered. Delivery is not proof that all reviews completed.

The running local services use the local database. Cloud and local rows do not automatically synchronize. This PR records the deployed Validator changes; it does not deploy teammates' Python services.

## Rollback

In the dashboard instruction history, restore a prior snapshot or choose **Restore original instructions** for baseline version 0. Human authorization and a matching expected latest version are required. Rollback creates a new immutable compensating version and preserves intervening history. A subsequent publication starts from the restored effective text.

Keep this module's version checks deployed when rolling back instructions. Reverting module code independently could remove those checks while updated callers are still running.

## Validation

Tests cover effective publication, superseded publication after a version race, replay without duplicate writes, skipping an already-present reminder, missing/stale version rejection, blocked automated policy edits, human-only rollback, baseline reconstruction, and publishing after rollback. Real local SpacetimeDB acceptance checks also exercised publication and compensating rollback in an isolated test database.
