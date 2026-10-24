#!/usr/bin/env bash
# Generate the shared A2A service secrets (common/server.py SERVICE_ACTORS: 9 agents + router + gateway).
# Each value is a shared secret: the sender sends A2A_TOKEN_<SELF>; every agent checks callers against its copy
# of all eleven. Live mode requires all eleven set and distinct, so every machine running agents gets this whole
# file, and the router host needs at least A2A_TOKEN_ROUTER. Never commit the output.
# The FreightOps team already has a shared set (data/team-release/a2a.env in the private team bundle): use that.
# This script is only for a fresh, separate deployment; two different sets cannot talk to each other.
#   usage: harness/infra/a2a_tokens.sh [out_file]     (default ~/.config/freightops/a2a-tokens.env)
set -euo pipefail
OUT=${1:-$HOME/.config/freightops/a2a-tokens.env}
if [ -e "$OUT" ]; then
  echo "$OUT exists; not overwriting (tokens are shared secrets, rotating breaks every peer)" >&2
  exit 1
fi
mkdir -p "$(dirname "$OUT")"
umask 077
{
  echo "# Shared A2A service secrets. Same file on every agent host; never commit."
  for actor in carrier_ap shipper_ar cfo risk controller verification voice_desk validator documents router gateway; do
    printf 'A2A_TOKEN_%s=%s\n' "${actor^^}" "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  done
} > "$OUT"
chmod 600 "$OUT"
echo "wrote $OUT ($(grep -c '^A2A_TOKEN_' "$OUT") tokens)"
