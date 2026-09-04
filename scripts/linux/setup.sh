#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOOTSTRAP_PYTHON="${FOXDASH_BOOTSTRAP_PYTHON:-/usr/bin/python3}"
if [[ ! -x "$BOOTSTRAP_PYTHON" ]]; then
  BOOTSTRAP_PYTHON="$(PATH='/usr/local/bin:/usr/bin:/bin' command -v python3 || true)"
fi
if [[ -z "$BOOTSTRAP_PYTHON" || ! -x "$BOOTSTRAP_PYTHON" ]]; then
  echo "[FoxDash] Could not find a system Python 3 interpreter. Set FOXDASH_BOOTSTRAP_PYTHON." >&2
  exit 1
fi
exec "$BOOTSTRAP_PYTHON" "$ROOT/install.py" "$@"
