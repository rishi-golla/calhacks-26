#!/usr/bin/env bash
# Start A, B, C one at a time, waiting for /health between each. Run via: sg docker -c "bash serve_all.sh"
set -euo pipefail
cd "$(dirname "$0")"
bash serve_nano.sh   && bash wait_health.sh 8001
bash serve_qwen4b.sh && bash wait_health.sh 8002
bash serve_gptoss.sh && bash wait_health.sh 8003
