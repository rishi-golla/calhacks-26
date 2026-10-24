# Validator instruction publication deployment

Validator now publishes the existing code-generated evidence/exact-cent reminder after a deterministic review failure. This is bounded append-only learning, not unrestricted model-written instruction editing. Policies, approval limits, role permissions, and delegation rules remain outside instruction history.

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
