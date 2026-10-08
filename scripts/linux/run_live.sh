#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
bash "$ROOT/scripts/linux/ensure_env.sh"
exec "$ROOT/.venv/bin/python" -m foxdash_lite run --source live --refresh-hz 10 --enable-leds "$@"
