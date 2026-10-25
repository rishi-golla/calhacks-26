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

## Who runs what

The demo stack spans two machines that both reach Maincloud outbound; neither needs to reach
the other. Each machine runs **one checkout, on `main`, and one copy of each process**.

| Machine | Processes | Cloud settings, loaded after the app `.env` |
|---|---|---|
| ASUS | Redis, `make sim`, `make ingest` (`ingest.main`), `make router` (`router.main`), and the vLLM containers behind `harness/server.py` | `handoff/ingest.env` for ingest, `handoff/router.env` for the router (plus `harness.env` if harness LLM telemetry is wired) |
| Mac, main checkout | PostgreSQL, the nine agents (`make live-agents`, :9101–9109), the dispatch worker (`common.review_worker`), the gateway (`make live-gateway`, :9000), the Elastic recall sync (`common.agent_recall --sync --loop 60`), the dashboard (`make web`) | the combined `services.env`, copied to `data/w2/at350-freightops/services.env`; the worker delivers outbox rows as the `router` identity |

The ASUS router enqueues each first hop into `routing_outbox` (`FIRST_HOP_TRANSPORT=spacetime`);
the Mac worker claims it every `DISPATCH_POLL_SECONDS` (1 s) and pushes it to the local agent over
A2A. The router and the worker share the `router` identity, and only the worker claims rows.
Agents read per-case source truth from the `source_record` table that ingest mirrors
(`SOURCE_DATA_BACKEND=spacetime`), so the Mac needs no `data/source.json`.

**Ingest and the router must use one database.** Both run on the ASUS from the same handoff set,
so their `SPACETIME_URL` and `SPACETIME_DB` match. `enqueue_first_hop` refuses a case that ingest
has not opened in that database (HTTP 530), so a split leaves every routed event refused and,
after repeated refusals, dead-lettered.

## Environment layering

The application `.env` always comes first and the cloud settings second, so the cloud wins.
Two loaders do this differently:

- **`make` service targets** (`make ingest`, `make router`, `make agents`, ...) reload `.env`
  underneath your shell; values already in the shell win. Source the handoff file in the shell
  first. A variable you only `unset` comes back from `.env`, so delete it from `.env` instead.
- **Live launchers** (`make live-agents`, `make live-gateway`, and anything started through
  `scripts.local_env.load_live_env`) apply `.env`, then `data/w2/<SPACETIME_DB>/services.env`,
  then `data/live/runtime.env`. Each file overrides the previous one **and the shell**.

ASUS, one terminal per process:

```bash
H=data/w2/maincloud/at350-freightops/handoff
set -a; . ./.env; . "$H/ingest.env"; set +a; make ingest     # terminal 1
set -a; . ./.env; . "$H/router.env"; set +a; make router     # terminal 2
make sim                                                     # terminal 3, once both machines are up
```

Mac, in the main checkout. One-time profile setup, after each regeneration:

```bash
cp data/w2/maincloud/at350-freightops/services.env data/w2/at350-freightops/services.env
# .env: SPACETIME_URL/SPACETIME_DB pointing at Maincloud, FREIGHTOPS_MODE=live, POSTGRES_URL,
#   OpenAI key, distinct A2A tokens. Delete SOURCE_DATA_PATH from .env and data/live/runtime.env;
#   scripts/live_sponsors.py writes it to both, so do not rerun it after the cutover.
#   data/live/runtime.env is applied last: it must not set SPACETIME_TIMEOUT_SECONDS or
#   SOURCE_DATA_BACKEND.
```

The copied `services.env` carries every token plus `SPACETIME_TIMEOUT_SECONDS=15`,
`SOURCE_DATA_BACKEND=spacetime` and `DISPATCH_POLL_SECONDS=1`, which override the `.env` values.
Then, one terminal each:

```bash
make live-agents                       # nine agents on :9101-9109
make live-gateway                      # :9000
.venv/bin/python -u -c "from scripts.local_env import load_live_env; load_live_env(); \
import runpy; runpy.run_module('common.review_worker', run_name='__main__')"   # dispatch worker
make web                               # with web/.env.local as below
```

The recall sync runs beside them as `common.agent_recall --sync --loop 60`, with the Elastic
writer key and the same `services.env` sourced.

An equivalent shell-layered launch (`make agents`, `make review-worker`, `make gateway` after
sourcing `.env` and the nine agent files plus `router.env` from `handoff/`) also works. Each
handoff file sets `SPACETIME_ACTOR`, so a layered shell keeps the last one; every writer names
its actor explicitly, and only anonymous source reads use it.

## Handoff files

Alan's checkout holds the private set under `data/w2/maincloud/at350-freightops/`:
`services.env` (all service tokens) and `handoff/<actor>.env` (one service each). Every file
carries `SPACETIME_URL=https://maincloud.spacetimedb.com`, `SPACETIME_DB=at350-freightops`,
`SPACETIME_DISABLED=0`, `SPACETIME_TIMEOUT_SECONDS=15` and the actor's
`SPACETIME_TOKEN_<UPPERCASE_ACTOR>`; per-actor files also set `SPACETIME_ACTOR`. Machine switches
ride with the actor:

| File | Adds |
|---|---|
| the nine agent files (`carrier_ap` … `documents`) | `SOURCE_DATA_BACKEND=spacetime` |
| `router.env` | `SOURCE_DATA_BACKEND=spacetime`, `FIRST_HOP_TRANSPORT=spacetime`, `DISPATCH_POLL_SECONDS=1` |
| `ingest.env`, `harness.env`, `evaluator.env` | nothing else |
| `services.env` (all tokens, no `SPACETIME_ACTOR`) | `SOURCE_DATA_BACKEND=spacetime`, `FIRST_HOP_TRANSPORT=spacetime`, `DISPATCH_POLL_SECONDS=1`, for the single-checkout live launchers |

The files are generated, never edited by hand. To pick up a change to the generator, run in the
checkout that holds `identities.json`:

```bash
.venv/bin/python -m scripts.spacetime_cloud handoff   # rewrites services.env and handoff/*.env; no cloud request
```

`provision` rewrites them too. Then copy `services.env` into the live profile as shown above,
and send each owner only their files, privately; the ASUS needs `ingest.env` and `router.env`.
Never add tokens to `NEXT_PUBLIC_*` settings. Local-server tokens do
not work on Maincloud. These are SpacetimeDB credentials, not A2A authentication tokens or
model-provider API keys; live execution additionally needs the PostgreSQL, Redis, model
credentials, A2A tokens and service URLs in [A2A-RUNBOOK.md](A2A-RUNBOOK.md).

Teammates running a single service can save their file as
`data/w2/maincloud/at350-freightops/<actor>.env` and load it the same way, second.
Use `SpacetimeClient(actor="<actor>")` in Python.

## Republish and provision before starting ingest

Alan, once per module change. A wrong database, a missing token or an unpublished reducer makes
ingest dead-letter each routed raw entry after three refusals (replay `<RAW_STREAM>:dead_letter`
to recover), and the router's `enqueue_first_hop` is refused the same way. So the module is
republished and provisioned first, and ingest starts last:

```bash
make w2-check                         # on main, with the module change merged
spacetime login show                  # must print the at350 publisher identity
spacetime publish at350-freightops --server maincloud \
  --module-path spacetime/module/spacetimedb --no-config \
  --delete-data=never --yes=remote --yes=skip-login
make spacetime-generate
.venv/bin/python -m scripts.spacetime_cloud provision
make cloud-verify VERIFY_ARGS="--heartbeat-wait 0"     # agent_config 9/9 and periods open
```

Then start the Mac, then the ASUS (`ingest`, `router`, and `sim` last). If a database is ever
recreated under the same name, delete `$SIM_DATA_DIR/source_mirror.json` on the ASUS so ingest
resends every source record.

## One stack per machine

On the evening of Oct 24, 2026 two full fleets wrote to `at350-freightops` from Alan's Mac:

| Checkout | Processes | Decision |
|---|---|---|
| main checkout (`~/Documents/Hackathons/CalHacks-2026`) | agents :9101–9109, `scripts.live_gateway` :9000, the review worker, the recall sync | **the Mac demo stack** (Alan's choice) |
| `traycer-witty-raven` worktree | agents :9201–9209, `router.main`, `ingest.main`, `gateway.app` :9010 | returns to a local database |

While both target the cloud they heartbeat the same nine `agent` rows, so `card_url` flips
between their ports and either fleet may answer a task. After the cutover only the main
checkout writes to `at350-freightops` from the Mac, with agents on :9101–9109, one worker, one
gateway and no `router.main` or `ingest.main` (those belong on the ASUS). Anything else running
on the Mac targets a local database. To verify:

```bash
# Every FreightLine process with the checkout it runs from: Maincloud writers only from the main checkout
pgrep -f 'agents\.[a-z_]+\.main|common\.review_worker|common\.agent_recall|router\.main|ingest\.main|gateway\.app|scripts\.live_gateway' |
  while read pid; do printf '%s %s ' "$pid" "$(lsof -a -p "$pid" -d cwd -Fn | sed -n 's/^n//p')"; ps -o command= -p "$pid"; done
# Launch-time database of processes that received it in their environment (not in-process loaders)
pgrep -f 'agents\.[a-z_]+\.main|router\.main|ingest\.main|gateway\.app' |
  while read pid; do printf '%s ' "$pid"; ps -Eww -o command= -p "$pid" | tr ' ' '\n' | grep -E '^SPACETIME_(URL|DB)=' | tr '\n' ' '; echo; done
# From the cloud: card ports 9101-9109 only, nine heartbeats advancing, no card moving between reads
make cloud-verify VERIFY_ARGS="--readyz"
```

Run the same `pgrep` on the ASUS: one `ingest.main`, one `router.main`, no agents.

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

Payments and hold releases over $5,000 wait for one named approver. On the Mac, set
`HUMAN_APPROVER_ID=<identity>` in `data/live/runtime.env`, using the identity of the dashboard
that will approve, after granting it as above. Then restart the agents. Settlement Approvals
(`controller`, :9105) reports `/readyz` 503 in live mode until this is set. Only that identity can
decide an approval in the dashboard, and the parked task resumes on its own afterwards.

## Troubleshooting

- `make cloud-verify` prints the cutover checklist from anonymous SQL: agent defaults, heartbeats,
  accounting periods, the newest case's source → case → dispatch → task chain, stale first hops,
  enqueue-to-accept latency and the outbox backlog with `last_error`. Add `--readyz` on the Mac.
- `Cloud actor ... already has another active identity`: you ran provisioning
  from a checkout without the original credentials. Use the supplied service file.
- Unauthorized writes: check the actor, cloud URL, database and matching token.
- Missing reducer/skill: report the deployed module mismatch to Alan; do not
  publish an older feature branch over the shared database.
- First hops stay `pending` with `last_error`: the worker is down, an agent is down
  (`ConnectError`), or its source record is missing (`SourceUnavailable`).
- Dashboard shows no active agents: database publication does not launch the
  Python services; their runtime owner must start them with live configuration.
