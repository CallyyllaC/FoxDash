#!/usr/bin/env bash
# Permit FoxDash to change only the HyperPixel backlight brightness entry
# without prompting for sudo on every adjustment.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  exec sudo "$0" "$@"
fi

ALLOWED_USER="${SUDO_USER:-${USER:-}}"
BACKLIGHT_PATH="/sys/class/backlight/backlight/brightness"
SUDOERS_FILE="/etc/sudoers.d/foxdash-hyperpixel-backlight"

if [[ -z "$ALLOWED_USER" || "$ALLOWED_USER" == "root" ]]; then
  echo "ERROR: Could not determine the non-root user running FoxDash." >&2
  exit 1
fi
if [[ ! -e "$BACKLIGHT_PATH" ]]; then
  echo "ERROR: $BACKLIGHT_PATH does not exist. Boot the HyperPixel PWM overlay first." >&2
  exit 1
fi

cat > "$SUDOERS_FILE" <<EOF
# Created by FoxDash setup_backlight_control.sh
# Limited to the one HyperPixel backlight path, nothing else.
${ALLOWED_USER} ALL=(root) NOPASSWD: /usr/bin/tee ${BACKLIGHT_PATH}
EOF
chmod 0440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE"
echo "Installed limited backlight permission for ${ALLOWED_USER}."
