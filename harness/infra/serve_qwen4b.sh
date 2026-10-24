#!/usr/bin/env bash
# Model B: Qwen3-4B-Instruct-2507 on 127.0.0.1:8002.
set -euo pipefail
NAME=qwen4b
WEIGHTS=${WEIGHTS:-$HOME/models/qwen4b-instruct}
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --restart unless-stopped --name "$NAME" \
  --gpus all --ipc=host --shm-size=8g \
  -p 127.0.0.1:8002:8000 \
  -v "$WEIGHTS:/model:ro" \
  vllm/vllm-openai:v0.20.0 \
  --model /model \
  --served-model-name qwen4b \
  --gpu-memory-utilization 0.15 \
  --max-model-len 16384 \
  --max-num-seqs 16 \
  --enable-prefix-caching \
  --enable-auto-tool-choice \
  --tool-call-parser hermes
