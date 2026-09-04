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

if ! "$BOOTSTRAP_PYTHON" "$ROOT/install.py" --check-venv >/dev/null 2>&1; then
  echo "[FoxDash] repairing local Linux virtual environment..."
  "$BOOTSTRAP_PYTHON" "$ROOT/install.py"
fi

PYTHON_BIN="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[FoxDash] installer completed but Linux venv interpreter is missing: $PYTHON_BIN" >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import textual, serial, blinkstick, usb, smbus2' >/dev/null 2>&1; then
  echo "[FoxDash] installing missing project dependencies..."
  "$BOOTSTRAP_PYTHON" "$ROOT/install.py"
fi
