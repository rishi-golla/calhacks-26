#!/usr/bin/env bash
# Serve one fully open model (open data + open training code + open weights) on 127.0.0.1:<port> with vLLM.
#   usage: serve_open.sh <name> <weights dir under ~/models> <port> <gpu-mem fraction> <max-model-len>
#   env:   QUANT=fp8   on-the-fly FP8 weights (about half the bytes read per token; GB10 is bandwidth-bound)
# Weights are safetensors with standard architectures only (no remote code), see download_open_models.sh.
set -euo pipefail
NAME=$1 DIR=$2 PORT=$3 MEM=$4 MAXLEN=$5
QUANT_ARGS=()
[ "${QUANT:-}" = "fp8" ] && QUANT_ARGS=(--quantization fp8)
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --restart unless-stopped --name "$NAME" \
  --gpus all --ipc=host --shm-size=8g \
  -p "127.0.0.1:$PORT:8000" \
  -v "$HOME/models/$DIR:/model:ro" \
  vllm/vllm-openai:v0.20.0 \
  --model /model \
  --served-model-name "$NAME" \
  --gpu-memory-utilization "$MEM" \
  --max-model-len "$MAXLEN" \
  --max-num-seqs 16 \
  --enable-prefix-caching \
  "${QUANT_ARGS[@]}"
