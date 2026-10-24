#!/usr/bin/env bash
# Start the fully open model set one at a time, gated on /health. Run via: sg docker -c "bash serve_all_open.sh"
#   slot B classify: olmoe :8004 | slot C cross-check: apertus :8005 | slot A extract: olmo3 :8006, olmo2 :8007
set -euo pipefail
cd "$(dirname "$0")"
bash serve_open.sh olmoe   olmoe-1b7b-instruct  8004 0.15 4096  && bash wait_health.sh 8004
bash serve_open.sh apertus apertus-8b-instruct  8005 0.18 16384 && bash wait_health.sh 8005
bash serve_open.sh olmo3   olmo3-7b-instruct    8006 0.17 16384 && bash wait_health.sh 8006
QUANT=${OLMO2_QUANT:-fp8} bash serve_open.sh olmo2 olmo2-13b-instruct 8007 0.22 4096 && bash wait_health.sh 8007
