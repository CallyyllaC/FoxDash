#!/usr/bin/env bash
set -u

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
PROJECT_DIR="${FOXDASH_DIR:-$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)}"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
DATA_DIR="${FOXDASH_DATA_DIR:-${HOME}/CarOBD}"
RUNNER_LOG="${DATA_DIR}/logs/obd_visible_runner.log"

mkdir -p "$(dirname "$RUNNER_LOG")"

run_dashboard() {
    if [ ! -x "$PYTHON_BIN" ]; then
        echo "$(date -Is) ERROR: FoxDash venv missing: $PYTHON_BIN" | tee -a "$RUNNER_LOG"
        read -r -p "Press Enter to close... " _
        return 1
    fi

    cd "$PROJECT_DIR" || return 1

    if ! bash "$PROJECT_DIR/scripts/linux/ensure_env.sh"; then
        echo "$(date -Is) ERROR: FoxDash dependency check failed" | tee -a "$RUNNER_LOG"
        read -r -p "Press Enter to close... " _
        return 1
    fi

    echo "$(date -Is) FoxDash visible runtime starting" | tee -a "$RUNNER_LOG"
    "$PYTHON_BIN" -u -m foxdash_lite run --source live --refresh-hz 10 --enable-leds
    rc=$?

    echo "$(date -Is) FoxDash exited with code ${rc}" | tee -a "$RUNNER_LOG"
    echo
    read -r -p "FoxDash stopped. Press Enter to close... " _
    return "$rc"
}

if [ "${FOXDASH_TERMINAL:-}" = "1" ]; then
    run_dashboard
    exit $?
fi

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-${HOME}/.Xauthority}"
export FOXDASH_RUNNER="$SCRIPT_PATH"

TERMINAL_BIN="$(command -v lxterminal || command -v x-terminal-emulator || true)"
if [ -z "$TERMINAL_BIN" ]; then
    echo "$(date -Is) ERROR: No terminal emulator found." >> "$RUNNER_LOG"
    exit 1
fi

"$TERMINAL_BIN" --profile=foxdash --no-remote -t "FoxDash" -e bash -lc \
    'FOXDASH_TERMINAL=1 exec bash "$FOXDASH_RUNNER"' &
terminal_pid=$!

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    WAYLAND_SOCKET="$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -type s -name 'wayland-*' | head -n 1)"
    [ -n "$WAYLAND_SOCKET" ] && export WAYLAND_DISPLAY="$(basename "$WAYLAND_SOCKET")"
fi

# Fullscreen is deliberately matched by lxterminal app_id only. The terminal
# title can change while the shell/Python/Textual stack starts, which made the
# old exact title match a race. Reassert fullscreen several times after the
# first successful match so a late compositor/window-state update cannot undo
# the initial request. The loop is bounded and harmless if wlrctl never finds
# the window.
if command -v wlrctl >/dev/null 2>&1; then
    fullscreen_successes=0
    for _ in $(seq 1 40); do
        sleep 0.25
        if wlrctl toplevel fullscreen app_id:lxterminal >/dev/null 2>&1; then
            fullscreen_successes=$((fullscreen_successes + 1))
            if [ "$fullscreen_successes" -ge 8 ]; then
                break
            fi
        fi
    done

    if [ "$fullscreen_successes" -eq 0 ]; then
        echo "$(date -Is) WARN: wlrctl could not fullscreen lxterminal during startup" >> "$RUNNER_LOG"
    fi
fi

wait "$terminal_pid"
