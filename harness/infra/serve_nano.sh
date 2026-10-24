#!/usr/bin/env bash
# Model A: Nemotron-3-Nano-Omni-30B-A3B-Reasoning (FP8 mix) on 127.0.0.1:8001.
# Flags follow NVIDIA's DGX Spark section of the model card (~/models/nano/README.md),
# without `pip install vllm[audio]` (we only send text). Media limits must stay >0:
# zeroing them skips building the projector and weight loading fails (KeyError '0.weight').
set -euo pipefail
NAME=nano
WEIGHTS=${WEIGHTS:-$HOME/models/nano}
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --restart unless-stopped --name "$NAME" \
  --gpus all --ipc=host --shm-size=16g \
  -p 127.0.0.1:8001:8000 \
  -v "$WEIGHTS:/model:ro" \
  vllm/vllm-openai:v0.20.0 \
  --model /model \
  --served-model-name nano \
  --trust-remote-code \
  --gpu-memory-utilization 0.35 \
  --max-model-len 32768 \
  --max-num-seqs 8 \
  --max-num-batched-tokens 32768 \
  --enable-prefix-caching \
  --kv-cache-dtype fp8 \
  --limit-mm-per-prompt '{"video": 1, "image": 1, "audio": 1}' \
  --reasoning-parser nemotron_v3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder
