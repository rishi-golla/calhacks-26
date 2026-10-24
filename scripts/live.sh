#!/usr/bin/env bash
# Full live stack: Redis, real-counterparty world, 8 agents, router, gateway and the real-time simulator.
# Calls/emails park for a human playing the counterparty (no scripted transcripts). Ctrl-C stops everything.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
[ -f .env ] && { set -a; source .env; set +a; }
export VOICE_AUTOCOMPLETE=0 MOCK_INTEGRATIONS=${MOCK_INTEGRATIONS:-0}
docker compose up -d redis >/dev/null
$PY -m sim.world
pids=()
./scripts/dev_agents.sh & pids+=($!)
sleep 4
$PY -m router.main & pids+=($!)
$PY -m gateway.app & pids+=($!)
sleep 2
$PY -m sim.generator & pids+=($!)
trap 'kill ${pids[@]} 2>/dev/null' INT TERM EXIT
echo "live: console at http://localhost:${GATEWAY_PORT:-9000}/console"
wait
