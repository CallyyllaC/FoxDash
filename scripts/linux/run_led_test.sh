#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
bash "$ROOT/scripts/linux/ensure_env.sh"
exec "$ROOT/.venv/bin/python" "$ROOT/tools/hardware/led_strip_test.py" "$@"
