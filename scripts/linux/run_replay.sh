#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
bash "$ROOT/scripts/linux/ensure_env.sh"
exec "$ROOT/.venv/bin/python" -m foxdash_lite run \
  --source replay \
  --log "$ROOT/sample_data/replay_sample.csv" \
  --no-random-start \
  --refresh-hz 10 \
  "$@"
