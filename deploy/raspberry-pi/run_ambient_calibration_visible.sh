#!/usr/bin/env bash
set -u

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
PROJECT_DIR="${FOXDASH_DIR:-$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)}"
DATA_DIR="${FOXDASH_DATA_DIR:-${HOME}/CarOBD}"
RUNNER_LOG="${DATA_DIR}/logs/ambient_calibration_runner.log"

mkdir -p "$(dirname "$RUNNER_LOG")"

run_calibration() {
    cd "$PROJECT_DIR" || return 1
    echo "$(date -Is) Ambient calibration starting" | tee -a "$RUNNER_LOG"
    bash "$PROJECT_DIR/scripts/linux/run_ambient_calibration.sh"
    rc=$?
    echo "$(date -Is) Ambient calibration exited with code ${rc}" | tee -a "$RUNNER_LOG"
    echo
    read -r -p "Calibration stopped. Press Enter to close... " _
    return "$rc"
}

if [ "${AMBIENT_CAL_TERMINAL:-}" = "1" ]; then
    run_calibration
    exit $?
fi

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-${HOME}/.Xauthority}"
export FOXDASH_AMBIENT_RUNNER="$SCRIPT_PATH"

TERMINAL_BIN="$(command -v lxterminal || command -v x-terminal-emulator || true)"
if [ -z "$TERMINAL_BIN" ]; then
    echo "$(date -Is) ERROR: No terminal emulator found." >> "$RUNNER_LOG"
    exit 1
fi

"$TERMINAL_BIN" --profile=foxdash --no-remote -t "FoxDash Ambient Calibration" -e bash -lc \
    'AMBIENT_CAL_TERMINAL=1 exec bash "$FOXDASH_AMBIENT_RUNNER"' &
terminal_pid=$!

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    WAYLAND_SOCKET="$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -type s -name 'wayland-*' | head -n 1)"
    [ -n "$WAYLAND_SOCKET" ] && export WAYLAND_DISPLAY="$(basename "$WAYLAND_SOCKET")"
fi

if command -v wlrctl >/dev/null 2>&1; then
    for _ in $(seq 1 20); do
        sleep 0.25
        wlrctl toplevel fullscreen app_id:lxterminal 'title:FoxDash Ambient Calibration' && break
    done
fi

wait "$terminal_pid"
