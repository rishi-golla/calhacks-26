#!/usr/bin/env bash
# Model C: gpt-oss-20b (MXFP4) on 127.0.0.1:8003.
# --max-num-seqs 1: on GB10 the MXFP4 Marlin MoE kernel (the only one vLLM 0.20 picks here; the
# VLLM_MXFP4_USE_MARLIN=0 override is ignored) emits corrupt Harmony output whenever requests are batched
# (500s, empty content). One at a time is correct and ~1 s per short verify call. Async scheduling was not
# the cause but is left off.
# Harmony fetches its tiktoken vocab at startup and that fails from the container, so mount a host copy:
#   mkdir -p ~/models/tiktoken && cd ~/models/tiktoken && for f in o200k_base cl100k_base; do
#     curl -sSfLO https://openaipublic.blob.core.windows.net/encodings/$f.tiktoken; done
set -euo pipefail
NAME=gptoss
WEIGHTS=${WEIGHTS:-$HOME/models/gptoss20b}
TIKTOKEN=${TIKTOKEN:-$HOME/models/tiktoken}
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --restart unless-stopped --name "$NAME" \
  --gpus all --ipc=host --shm-size=8g \
  -p 127.0.0.1:8003:8000 \
  -v "$WEIGHTS:/model:ro" \
  -v "$TIKTOKEN:/tiktoken:ro" -e TIKTOKEN_ENCODINGS_BASE=/tiktoken \
  vllm/vllm-openai:v0.20.0 \
  --model /model \
  --served-model-name gptoss \
  --gpu-memory-utilization 0.25 \
  --max-model-len 16384 \
  --max-num-seqs 1 \
  --enable-auto-tool-choice \
  --tool-call-parser openai \
  --reasoning-parser openai_gptoss   # keeps the JSON grammar off the analysis channel
