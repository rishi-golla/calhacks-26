# Shared Maincloud setup

Use the team `main` branch. The cloud module is deployed by Alan using the publisher
CLI session; teammates do not need to publish or run `spacetime_cloud provision`.
The older `codex/a2a-brokerage` branch preserves a local implementation snapshot;
it is not the current team integration branch.

## Get the code

In an existing clean checkout on `main`:

```bash
git pull --ff-only origin main
make setup
```

If you have work in progress, keep it on your branch and merge `origin/main` after
`git fetch origin`. Do not reset your working tree to follow these instructions.

## Backend services

Alan supplies a private `<actor>.env` file per service. Save it as
`data/w2/maincloud/at350-freightops/<actor>.env` inside your checkout, then load it:

```bash
set -a
source data/w2/maincloud/at350-freightops/<actor>.env
set +a
```

Replace `<actor>` with your service ID, such as `ingest`, `harness`, `evaluator`,
`router`, `carrier_ap`, or `documents`. Each file contains:

- `SPACETIME_URL=https://maincloud.spacetimedb.com`
- `SPACETIME_DB=at350-freightops`
- `SPACETIME_DISABLED=0`
- `SPACETIME_ACTOR=<actor>` for legacy callers
- `SPACETIME_TOKEN_<UPPERCASE_ACTOR>` for authenticated writes

Load your application configuration first and this cloud file second so local
Spacetime defaults do not overwrite the cloud settings. Use
`SpacetimeClient(actor="<actor>")` in Python. Keep the file ignored and private;
never add tokens to `NEXT_PUBLIC_*` settings. Local-server tokens do not work on
Maincloud. All nine agent processes require all nine corresponding token files
(or the combined service file, supplied privately to the runtime owner).

These are SpacetimeDB credentials, not A2A authentication tokens or model-provider
API keys. Live agent execution additionally requires the configured Postgres,
Redis, source dataset, model credentials, A2A tokens and service URLs described in
[A2A-RUNBOOK.md](A2A-RUNBOOK.md). Use `make agents`, `make gateway`, `make router`
and `make review-worker` with the intended environment loaded. The `live-*`
launchers currently load the separate local profile under `data/w2/<database>`;
they should not be used as Maincloud launchers without configuring that profile.

## Dashboard

Put these four settings in `web/.env.local` (preserve any other settings):

```dotenv
NEXT_PUBLIC_SPACETIME_URL=https://maincloud.spacetimedb.com
NEXT_PUBLIC_SPACETIME_DB=at350-freightops
SPACETIME_URL=https://maincloud.spacetimedb.com
SPACETIME_DB=at350-freightops
```

Restart/rebuild the frontend; `make web` serves it at http://localhost:3001.
Read-only dashboard access needs no service token. For human actions, send Alan
the public identity shown by the cloud-connected dashboard. Alan grants it:

```bash
.venv/bin/python -m scripts.spacetime_cloud grant-human <identity> --name <teammate>
```

## Troubleshooting

- `Cloud actor ... already has another active identity`: you ran provisioning
  from a checkout without the original credentials. Use the supplied service file.
- Unauthorized writes: check the actor, cloud URL, database and matching token.
- Missing reducer/skill: report the deployed module mismatch to Alan; do not
  publish an older feature branch over the shared database.
- Dashboard shows no active agents: database publication does not launch the
  Python services; their runtime owner must start them with live configuration.
