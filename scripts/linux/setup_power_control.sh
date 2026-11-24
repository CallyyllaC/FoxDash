#!/usr/bin/env bash
# Permit FoxDash's ignition monitor to request a clean power-off (or a reboot
# when external power returns mid-shutdown) without a password prompt.
#
# FoxDash first asks logind, which polkit already allows when the dashboard
# runs in the active autologin desktop session. This rule is the fallback for
# FoxDash running outside that session (for example restarted over SSH).
# It allows exactly two fixed commands and nothing else.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  exec sudo "$0" "$@"
fi

ALLOWED_USER="${SUDO_USER:-${USER:-}}"
SYSTEMCTL="/usr/bin/systemctl"
SUDOERS_FILE="/etc/sudoers.d/foxdash-power"

if [[ -z "$ALLOWED_USER" || "$ALLOWED_USER" == "root" ]]; then
  echo "ERROR: Could not determine the non-root user running FoxDash." >&2
  exit 1
fi
if [[ ! -x "$SYSTEMCTL" ]]; then
  echo "ERROR: $SYSTEMCTL not found." >&2
  exit 1
fi

TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT
cat > "$TMP_FILE" <<EOF
# Created by FoxDash setup_power_control.sh
# Ignition shutdown only: these two exact commands, no arguments beyond them.
${ALLOWED_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL} poweroff, ${SYSTEMCTL} reboot
EOF
visudo -cf "$TMP_FILE"
install -m 0440 -o root -g root "$TMP_FILE" "$SUDOERS_FILE"
echo "Installed limited power-off permission for ${ALLOWED_USER} in ${SUDOERS_FILE}."
