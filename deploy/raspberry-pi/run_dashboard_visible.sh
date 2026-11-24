#!/usr/bin/env bash
set -u

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
PROJECT_DIR="${FOXDASH_DIR:-$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)}"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
DATA_DIR="${FOXDASH_DATA_DIR:-${HOME}/CarOBD}"
RUNNER_LOG="${DATA_DIR}/logs/obd_visible_runner.log"

mkdir -p "$(dirname "$RUNNER_LOG")"

start_foxdash() {
    "$PYTHON_BIN" -u -m foxdash_lite run --source live --refresh-hz 10 --enable-leds --enable-ambient-brightness \
        --enable-power-management --power-dry-run
}

run_dashboard_kiosk() {
    # Kiosk session (FOXDASH_KIOSK=1 from ~/.config/foxdash-kiosk/environment):
    # nothing but FoxDash may appear in the terminal. Hide the text cursor,
    # keep runner messages in the log only, and restart FoxDash in place if it
    # exits, so the fullscreen window (and the boot background behind it)
    # never goes away. Stop it for good with: pkill -f run_dashboard_visible.sh
    printf '\033[?25l\033[2J\033[H'
    stty -echo 2>/dev/null || true
    trap 'exit 0' TERM HUP INT

    if [ ! -x "$PYTHON_BIN" ]; then
        echo "$(date -Is) ERROR: FoxDash venv missing: $PYTHON_BIN" >> "$RUNNER_LOG"
        while :; do sleep 3600; done
    fi
    cd "$PROJECT_DIR" || exit 1

    while :; do
        started=$(date +%s)
        echo "$(date -Is) FoxDash visible runtime starting (kiosk)" >> "$RUNNER_LOG"
        start_foxdash
        rc=$?
        echo "$(date -Is) FoxDash exited with code ${rc}; restarting" >> "$RUNNER_LOG"
        printf '\033[0m\033[?25l\033[2J\033[H'
        # Back off if it is failing straight away, so a crash loop cannot
        # starve the ignition monitor of CPU.
        if [ $(( $(date +%s) - started )) -lt 10 ]; then sleep 5; else sleep 1; fi
    done
}

run_dashboard() {
    if [ "${FOXDASH_KIOSK:-}" = "1" ]; then
        run_dashboard_kiosk
        return
    fi

    if [ ! -x "$PYTHON_BIN" ]; then
        echo "$(date -Is) ERROR: FoxDash venv missing: $PYTHON_BIN" | tee -a "$RUNNER_LOG"
        read -r -p "Press Enter to close... " _
        return 1
    fi

    cd "$PROJECT_DIR" || return 1

    echo "$(date -Is) FoxDash visible runtime starting" | tee -a "$RUNNER_LOG"
    start_foxdash
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

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    WAYLAND_SOCKET="$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -type s -name 'wayland-*' | head -n 1)"
    [ -n "$WAYLAND_SOCKET" ] && export WAYLAND_DISPLAY="$(basename "$WAYLAND_SOCKET")"
fi

launch_terminal() {
    "$TERMINAL_BIN" --profile=foxdash --no-remote -t "FoxDash" -e bash -lc \
        'FOXDASH_TERMINAL=1 exec bash "$FOXDASH_RUNNER"' &
    terminal_pid=$!

    # In the kiosk session the labwc window rule makes the terminal fullscreen
    # as it maps; this loop is the fallback for the desktop session.
    if command -v wlrctl >/dev/null 2>&1; then
        for _ in $(seq 1 20); do
            sleep 0.25
            wlrctl toplevel fullscreen app_id:lxterminal 'title:FoxDash' && break
        done
    fi

    wait "$terminal_pid"
}

if [ "${FOXDASH_KIOSK:-}" = "1" ]; then
    # Reopen the terminal if it ever closes; the boot background stays behind it.
    trap 'kill "${terminal_pid:-}" 2>/dev/null; exit 0' TERM HUP INT
    while :; do
        launch_terminal
        echo "$(date -Is) FoxDash terminal closed; reopening" >> "$RUNNER_LOG"
        sleep 2
    done
fi

launch_terminal
