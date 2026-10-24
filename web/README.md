# Golden Gate Freight dashboard (W2)

A Next.js 15 / React 19 dashboard backed by real SpacetimeDB 2.10.2 subscriptions. It has no client-side mock dataset and never invents live activity. Empty, disconnected, fallback and gateway-stub states are explicit.

## Run

From this directory:

```sh
npm ci
cp .env.example .env.local
npm run dev
```

Open `http://localhost:3001`. SpacetimeDB uses port `3000`; the web app uses `3001` by default. First publish the W2 module and generate bindings as described in [`../spacetime/README.md`](../spacetime/README.md). Bindings are checked in at `module_bindings/` and must be regenerated after schema changes.

```sh
npm run lint
npm test
npm run build
npm start
```

`spacetimedb` and `react-data-grid` are pinned to the tested versions. A PostCSS override patches the existing Next.js dependency without requiring a framework major upgrade.

## Identity and human access

The browser connects directly to SpacetimeDB, receives its own identity and private token, and stores that token under `freightops.identity.v1:<server>:<database>` in local storage. No service/admin token is included in the client bundle or used by the fallback route. Only use the dashboard on trusted devices; clearing local storage creates a new identity.

Click the connection indicator or the observer profile to copy the **public identity**. Have the database publisher grant it `human` through the W2 administration CLI / `grant_role` reducer:

```json
{
  "write_id": "<new UUID>",
  "identity": "<64-character browser identity>",
  "role": "human",
  "actor": "human:Alan",
  "active": true
}
```

The module derives caller identity from the signed connection. Frontend state cannot bypass authorization. Approval codes are issued privately by the administrator with `issue_approval_code`, expire, and lock after five failures. A wrong code deliberately commits a failed attempt: the UI watches `approval.decision`, `failed_attempts` and `last_error`, and only reports approval after the requested decision appears in the subscription.

## Live data and fallback

- One WebSocket subscription covers the fixed set of public operational tables. SDK camelCase rows are normalized at the boundary to the snake_case contract.
- Table events coalesce into a snapshot within 40 ms. No network polling runs while the live subscription is healthy.
- Connection loss starts a read-only `/api/snapshot` fallback every five seconds. The server queries only an explicit public-table allowlist, with no service token. 2.10.2 accepts one SQL statement per request, so table requests run in parallel.
- The pinned 2.10.2 SDK has no automatic-reconnect builder method. The provider rebuilds the connection with the saved token using bounded exponential backoff (2–30 seconds) and resubscribes. The reconnect control also works immediately.
- Writes are disabled while disconnected or on fallback; reducers remain authoritative. Stale agent heartbeats are visibly marked after 60 seconds.
- Operational money fields use USD, **not cents**. Remaining holds subtract `released_amount` from their original amount.

## Workspaces

- **Overview:** eight-agent React Flow graph with actual delegation edges and recent animation, live workload counts, case/hold summaries, and evaluator metrics.
- **Cases & tasks:** case and agent filters, search, parent task depth, A2A and business states, artifacts/evidence, policy trail, models, document reads, human takeover, redacted call transcripts, recording links, intended/routed destinations.
- **Human inbox:** one-time-code decisions; feedback and durable Validator delivery status; resolutions and approval history.
- **Ledger & holds:** original and remaining holds, journal lines, payment proposals and cash metrics.
- **Audit:** keyboard-navigable React Data Grid; AP/AR/GL/holds/forecast/payments; cell blame, all versions, ancestor-based state diffs and per-case commit log; CSV export. Sequence breaks timestamp ties. CSV values neutralize spreadsheet formula injection.
- **Counterparties:** unmasked source-backed company and contact data, confidence and alternate values, persistent demo-outreach banner.
- **Validator:** checks and friction, versioned memory, historical restore/deactivation, instruction diffs and compensating restores.
- **Harness:** auto/pinned routing per agent, explicitly entered W1 model IDs, enable switches, model telemetry and disagreements.
- **Demo:** PHONE, bank-change EMAIL, SHORT-PAY and judge scenarios through W4, with allowlisted team destination selection.

Audit source URLs are only linked for HTTP(S). Non-URL document references remain visible for the source-system owner to resolve. The audit views reconstruct the actual recorded history; they do not manufacture missing provenance. Human flags create durable router work; a queued item is not reported as a completed Validator review.

## W4 integration

Set `GATEWAY_URL`, `DEMO_GATEWAY_ENABLED=true`, and the same `DEMO_ALLOWLIST_PHONES` / `DEMO_ALLOWLIST_EMAILS` used by W4. These variables are read server-side. Never enable a real-recipient path: only team-controlled destinations belong in the allowlist. The W4 gateway remains responsible for authentication, policy checks and final outreach routing.

The same-origin Next route forwards `POST /demo/{phone|email|bank_change|short_pay|scenario}` with:

```json
{
  "case_id": "optional existing case",
  "scenario": "custom scenario text",
  "channel": "phone|email",
  "demo_target": "allowlisted team destination"
}
```

The repository's original handler responds only with `{"queued":"<kind>"}`. That response is explicitly displayed as a **stub acknowledgment**, never a confirmed case, call or email. Real confirmation comes from the gateway result and subscribed case/call records. No W3/W4 implementation is duplicated in this frontend.

## Verification

Unit tests cover same-timestamp audit ordering, ancestor isolation, forward/reverse diffs, malformed ancestry termination, USD normalization, and safe document URLs. Root W2 integration tests exercise reducers and measured real subscriptions against the published module. Browser checks should include mobile navigation, live identity provisioning, wrong/correct OTP, flag delivery, model pinning and audit cell provenance. Live behavior depends on running W1/W3/W4 services for their respective work; UI empty states identify missing records.
