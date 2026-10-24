#!/usr/bin/env bash
# Model C: gpt-oss-20b (MXFP4) on 127.0.0.1:8003.
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
  --max-num-seqs 8 \
  --async-scheduling \
  --enable-auto-tool-choice \
  --tool-call-parser openai
