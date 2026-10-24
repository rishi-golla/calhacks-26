# W2 integration boundary

The module is the operational system of record. W1 owns inference; W3 owns agent execution,
policy preflight and A2A dispatch; W4 owns source data, external integrations and evaluation.
Nothing in W2 simulates a successful phone call, inference result or payment transfer.

For the shared Maincloud deployment, use database `at350-freightops` at
`https://maincloud.spacetimedb.com`; see [CLOUD.md](CLOUD.md). Local examples below
use `freightops`. Credentials are specific to the issuing server, so local service
tokens must not be sent to Maincloud.

## Wire protocol

Pinned and verified against SpacetimeDB **2.10.2**:

```text
POST /v1/database/freightops/call/<snake_case_reducer>
Authorization: Bearer <the calling service's token>
Content-Type: application/json

["{\"write_id\":\"a-stable-event-or-operation-id\", ... }"]
```

Every business reducer has one `payload: string` argument containing a JSON object. Keep
`write_id` stable when recovering from an uncertain HTTP outcome. An identical replay is a no-op;
reusing an ID with different contents is rejected. A timeout is **not** evidence of rollback.
Public column names use snake_case; generated TypeScript properties use camelCase.

Money fields named `amount`, `exposure_usd`, `debit`, and `credit` are **USD**. For example,
`9400` means $9,400. Reducers validate exact cents and use integer arithmetic for controls.
Public `*_json` fields contain JSON strings. Server timestamps and cryptographic audit hashes
are produced in the reducer transaction, never trusted from a client.

The authoritative payload reference lives in [the module contract](module/CONTRACT.md). Generate
TypeScript bindings again after schema/reducer changes with `make spacetime-generate`.

## Service identities

`make spacetime-publish` creates local service identities and grants each its own role. The
publisher is an administrator, not a universal financial-agent impersonation token. Credentials
are owner-readable files under ignored `data/w2/freightops/`; do not commit them.

```bash
set -a
source data/w2/freightops/services.env
set +a
```

Each service uses `SPACETIME_TOKEN_<UPPERCASE_ACTOR>` (for example,
`SPACETIME_TOKEN_CONTROLLER`). The browser obtains its own identity over WebSocket. Grant a
human role to the identity shown in the dashboard:

```bash
.venv/bin/python -m scripts.spacetime_admin grant-human <browser-identity> --name alan
```

The local provisioning script never modifies the user's global Spacetime CLI login. For a
remote deployment, use your own OIDC tokens and deliberate role provisioning; the helper
intentionally operates only on loopback servers.

## Python calls

Use the strict `SpacetimeClient` for actions whose success matters. It runs
`common.policy.check()` before writes and reports transport/reducer failures explicitly.
The legacy `call_reducer(name, *args)` remains available for best-effort telemetry.
`SPACETIME_DISABLED=1` preserves the original scaffold's offline behavior; set it to `0`
for live writes.

Select the actor explicitly; sourcing `services.env` supplies credentials but deliberately
does not choose one service for every process. Inside an async agent handler:

```python
from common.spacetime_client import SpacetimeClient

db = SpacetimeClient(actor="controller")
receipt = await db.mutate("place_hold", {
    "write_id": "hold-request-4821-v1",  # persist with the originating request
    "id": "HOLD-4821",
    "case_id": "CASE-4821",            # must already exist
    "invoice_id": "INV-4821",          # must already be mirrored by ingest
    "amount": 300,
    "reason_code": "UNSUPPORTED_DETENTION",
})
```

For legacy `await call_reducer(name, *args)`, set `SPACETIME_ACTOR` in that service's
environment and pass a **single serialized JSON string** containing `write_id`, rather
than an object or positional business fields. Prefer `mutate` for new integration work.

A reducer receipt confirms transport acceptance, not an approved financial decision: failed OTP
attempts deliberately commit their attempt count while the approval stays pending. Read the
approval row after a decision. After an uncertain high-value action whose authorization is
already consumed, the strict Python preflight refuses replay; reconcile the stored hold or
proposal. Ordinary identical writes with an unconsumed authorization remain idempotent.

To prevent splitting an obligation to avoid approval, any payment against an originally over
$5,000 invoice, or release from an originally over-$5,000 hold, requires human authorization for
the exact requested amount, including smaller partial actions.

`get_instruction_history(agent)` returns the ordered version history. Instruction rows are
patches: a revert creates a new version whose `reverts_version` selects the historical snapshot
to reconstruct. W3 must resolve that history; the active row alone is not a complete prompt.

W3 must wire calls into its runtime and financial tools. W4 must mirror invoices and directory
records before agents reference them. W1 writes LLM telemetry and reads `agent_config`; W4
writes computed scoreboard metrics. W2 does not edit these owners' implementations.

## Human approvals and work delivery

Approval decisions require a separate human identity and a private, expiring, one-time code.
Issue a code using the local admin helper:

```bash
.venv/bin/python -m scripts.spacetime_admin issue-code <approval-id>
```

Give that code only to the assigned approver. Codes are never in public subscription rows or
browser bundles. A client-supplied `code_ok` or arbitrary approval ID cannot authorize money.

Human flags and financial task completions create durable `routing_outbox` work. W3's router
must claim it, respect `target_agent`, dispatch `review_flag` / `review_output` over its A2A
runtime, and acknowledge delivery after successful handoff. Validator issues can also create
`review_followup` work for the original task's agent, with `target_task_id`, `actions`
(`reopen_case` / `rerun_task`) and `review_id` in the payload. Honor human ownership and use a
new task ID for revised output, since terminal tasks are immutable. Use the outbox ID as the
dispatch idempotency key. An outbox row means queued work, not a completed review.
Task retries should use stable task and operation IDs. Human-owned cases reject autonomous
financial writes; W3 should also stop scheduling new autonomous work for them.

## Official references

- [Reducer and SQL HTTP API](https://spacetimedb.com/docs/http/database/)
- [Identity issuance and bearer authorization](https://spacetimedb.com/docs/http/authorization/)
- [TypeScript modules and reconnecting clients](https://spacetimedb.com/docs/quickstarts/typescript/)
