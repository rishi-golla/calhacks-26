# Maincloud deployment

The W2 module is deployed under Alan's `at350` account:

- Dashboard: <https://spacetimedb.com/@at350/at350-freightops>
- Server: `https://maincloud.spacetimedb.com`
- Database: `at350-freightops`
- Database identity: `c200318d8fbe0086104c5b4182240bec3c6d600c56d7783f71e01a6f20028bdd`
- Publisher identity: `c2005175965c7ab35b8367adc2064a7bc96cacb0b0b796ddf81377fc02b50f1f`

The signed-in account page shows the database as Running. Its management dashboard
has Private visibility, so the signed-out public profile does not list it. This
visibility setting does not make the module's public operational tables private:
the hackathon dashboard reads those tables anonymously and writes require roles.
Approval secrets, idempotency receipts, and chain bookkeeping remain private tables.

## Update the deployed module

Use the publisher's existing CLI session. Confirm that `spacetime login show`
matches the publisher above before running:

```bash
spacetime publish at350-freightops --server maincloud \
  --module-path spacetime/module/spacetimedb --no-config \
  --delete-data=never --yes=remote --yes=skip-login
```

There is no automatic deletion or local-to-cloud data copy. Local development
continues to use the separate `freightops` database on port 3000.

## Provision cloud identities and fixtures

The cloud helper is deliberately restricted to the server, database, and publisher
listed above. It verifies the existing CLI login and database owner, issues fresh
Maincloud service identities, and grants roles using the CLI. It never extracts
or stores the publisher's token and does not reuse the local server's tokens.

```bash
.venv/bin/python -m scripts.spacetime_cloud provision
.venv/bin/python -m scripts.spacetime_cloud demo
```

The `demo` command writes explicitly labeled W2 fixtures through real reducers.
It refuses to seed over non-fixture cases, a locked October period, or active
non-fixture Carrier AP instructions. Stable operation IDs make a repeated seed
idempotent. It does not run agents or place calls.

Service credentials are stored with owner-only permissions in ignored
`data/w2/maincloud/at350-freightops/services.env`. To run a service against the
shared backend:

```bash
set -a
source data/w2/maincloud/at350-freightops/services.env
set +a
```

Select its actor explicitly with `SpacetimeClient(actor="controller")`, for
example. Each teammate needs only the token for the service they own.

## Connect the dashboard

Set all four coordinates in `web/.env.local`, then rebuild/restart Next.js:

```dotenv
NEXT_PUBLIC_SPACETIME_URL=https://maincloud.spacetimedb.com
NEXT_PUBLIC_SPACETIME_DB=at350-freightops
SPACETIME_URL=https://maincloud.spacetimedb.com
SPACETIME_DB=at350-freightops
```

The two browser variables are embedded at build time. The two server variables
configure the read-only snapshot fallback. Never place service credentials in
`NEXT_PUBLIC_*` variables. A hosted frontend uses these same coordinates; the W4
gateway must also be reachable from that deployment before enabling the demo console.

Cloud and local browser identities are stored separately. Grant the identity
shown in the cloud-connected dashboard rather than reusing a local browser identity.
Grant it with:

```bash
.venv/bin/python -m scripts.spacetime_cloud grant-human <identity> --name alan
```

Cloud approval codes can be issued with `issue_approval_code` through the
publisher's authenticated database dashboard. The loopback admin helper's
`issue-code` command targets only the local development database.

Keep credentials on trusted devices and share service credentials privately with
the owner of that service. See [INTEGRATION.md](INTEGRATION.md) for the API contract
and cross-owner responsibilities.

## Demo presentation

Use one selected case for a 75–90 second presentation: introduce the disputed
$300 charge, show the agent handoffs, click the held amount for its provenance,
then submit human feedback and show the subscribed result. Current `CASE-W2-*`
records are explicitly labeled synthetic fixtures. Their persisted task graph
does not demonstrate live agent execution, and queued Validator work does not
mean a review has completed.

The recommended next frontend change is a presentation view with four steps:
Case, Handoffs, Evidence, Human control. Carry the selected case and audit cell
between steps, use large type, and keep full operational navigation available
for questions. Add the phone-ring opening once W3/W4 are executing the real flow;
keep recovery, full metrics, and model routing as optional follow-ups.
