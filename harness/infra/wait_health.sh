#!/usr/bin/env bash
# Usage: wait_health.sh <port> [timeout_s]. Polls /health until 200.
port=$1; timeout=${2:-1200}; t=0
until curl -sf "http://127.0.0.1:$port/health" >/dev/null; do
  sleep 5; t=$((t+5))
  if [ "$t" -ge "$timeout" ]; then echo "port $port not healthy after ${timeout}s" >&2; exit 1; fi
done
echo "port $port healthy after ${t}s"
