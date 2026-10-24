#!/usr/bin/env bash
# Start all nine A2A agents. Ctrl-C stops them all.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
pids=()
for a in carrier_ap shipper_ar cfo risk controller verification voice_desk validator documents; do
  $PY -m agents.$a.main & pids+=($!)
done
trap 'kill ${pids[@]} 2>/dev/null' INT TERM EXIT
wait
