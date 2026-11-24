#!/usr/bin/env bash
# Install (or remove) the FoxDash boot experience on the Raspberry Pi:
#
#   * the "foxdash" Plymouth boot screen (the Ember Deck boot animation,
#     rotated for the HyperPixel 4.0), shown from the initramfs onwards;
#   * a quiet kernel command line (no Raspberry Pi logos, console text or
#     blinking console cursor) and no firmware rainbow splash;
#   * the "FoxDash Kiosk" LightDM session: labwc with only FoxDash, the last
#     boot frame as background, no desktop, panel, decorations or cursor;
#   * a blank cursor theme, so no cursor is drawn at any time.
#
# Usage (on the Pi, from the FoxDash folder):
#   sudo deploy/raspberry-pi/install_boot_experience.sh            # install
#   sudo deploy/raspberry-pi/install_boot_experience.sh --uninstall
#
# Every system file it changes is copied to /var/backups/foxdash-boot/<time>/
# first. SSH, Wi-Fi, VNC and the power-management sudoers rule are not touched.
# Changes take effect at the next boot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
FOXDASH_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
THEME_SRC="$SCRIPT_DIR/boot/plymouth/foxdash"
THEME_DST=/usr/share/plymouth/themes/foxdash
CURSOR_DST=/usr/share/icons/foxdash-blank
SESSION_FILE=/usr/share/wayland-sessions/foxdash-kiosk.desktop
SESSION_BIN=/usr/local/bin/foxdash-session
LIGHTDM_CONF=/etc/lightdm/lightdm.conf
BOOT_DIR=/boot/firmware
[ -f "$BOOT_DIR/cmdline.txt" ] || BOOT_DIR=/boot
CMDLINE="$BOOT_DIR/cmdline.txt"
CONFIG="$BOOT_DIR/config.txt"
BACKUP_ROOT=/var/backups/foxdash-boot
QUIET_ARGS=(logo.nologo vt.global_cursor_default=0 loglevel=3 systemd.show_status=false rd.udev.log_level=3 udev.log_level=3)

die() { echo "ERROR: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run with sudo"
TARGET_USER="${SUDO_USER:-}"
[ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ] || die "run with sudo from the FoxDash user's account"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
as_user() { sudo -u "$TARGET_USER" -H "$@"; }

backup() {
    BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BACKUP_DIR"
    for f in "$CMDLINE" "$CONFIG" "$LIGHTDM_CONF" /etc/plymouth/plymouthd.conf; do
        [ -f "$f" ] && cp -a "$f" "$BACKUP_DIR/"
    done
    echo "Backed up system files to $BACKUP_DIR"
}

set_lightdm_session() {
    sed -i -E "s/^autologin-session=.*/autologin-session=$1/" "$LIGHTDM_CONF"
    grep -q "^autologin-session=$1\$" "$LIGHTDM_CONF" || die "could not set autologin-session in $LIGHTDM_CONF"
}

# Note: never name a function "install" here: it would shadow install(1).
do_uninstall() {
    backup
    set_lightdm_session rpd-labwc
    if [ "$(plymouth-set-default-theme)" = foxdash ]; then
        echo "Restoring the pix boot theme (rebuilds the initramfs, takes a few minutes)..."
        plymouth-set-default-theme -R pix
    fi
    local args
    args="$(cat "$CMDLINE")"
    for a in "${QUIET_ARGS[@]}"; do args="$(echo "$args" | sed -E "s/(^| )$a( |\$)/\1\2/g")"; done
    args="$(echo "$args" | sed -E 's/console=tty3/console=tty1/; s/  +/ /g; s/ +$//')"
    printf '%s\n' "$args" > "$CMDLINE"
    sed -i '/^# FoxDash boot experience$/,+1d' "$CONFIG"
    rm -rf "$THEME_DST" "$CURSOR_DST" "$SESSION_FILE" "$SESSION_BIN"
    echo "Removed. The Raspberry Pi desktop session returns at the next boot."
    echo "(swaybg and ~/.config/foxdash-kiosk are left in place; both are harmless.)"
}

make_blank_cursor_theme() {
    mkdir -p "$CURSOR_DST/cursors"
    # One fully transparent 1x1 Xcursor image, nominal size 24.
    python3 -I - "$CURSOR_DST/cursors/blank" <<'PY'
import struct, sys
header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
toc = struct.pack("<III", 0xFFFD0002, 24, 28)
image = struct.pack("<IIIIIIIII", 36, 0xFFFD0002, 24, 1, 1, 1, 0, 0, 0) + struct.pack("<I", 0)
open(sys.argv[1], "wb").write(header + toc + image)
PY
    # Every cursor name any installed theme knows resolves to the blank image.
    local name
    for dir in /usr/share/icons/*/cursors; do
        [ "$dir" = "$CURSOR_DST/cursors" ] && continue
        for f in "$dir"/*; do
            name="$(basename "$f")"
            [ "$name" = blank ] || ln -sfn blank "$CURSOR_DST/cursors/$name"
        done
    done
    for name in default left_ptr text xterm pointer hand2 wait watch progress grab grabbing; do
        [ -e "$CURSOR_DST/cursors/$name" ] || ln -sfn blank "$CURSOR_DST/cursors/$name"
    done
    printf '[Icon Theme]\nName=FoxDash blank\nComment=Invisible cursor for the FoxDash kiosk\n' > "$CURSOR_DST/index.theme"
}

theme_up_to_date() {
    [ "$(plymouth-set-default-theme)" = foxdash ] || return 1
    local f
    for f in "$THEME_SRC"/foxdash.plymouth "$THEME_SRC"/foxdash.script "$THEME_SRC"/*.png; do
        cmp -s "$f" "$THEME_DST/$(basename "$f")" || return 1
    done
}

do_install() {
    for f in "$THEME_SRC/foxdash.plymouth" "$THEME_SRC/foxdash.script" "$SCRIPT_DIR/boot/foxdash-boot-wallpaper.png" \
             "$SCRIPT_DIR/kiosk/labwc/rc.xml" "$SCRIPT_DIR/kiosk/foxdash-session"; do
        [ -f "$f" ] || die "missing $f"
    done
    grep -q "^autologin-session=" "$LIGHTDM_CONF" || die "$LIGHTDM_CONF has no autologin-session (autologin not set up?)"
    backup

    if ! command -v swaybg >/dev/null 2>&1; then
        echo "Installing swaybg (draws the boot background behind FoxDash)..."
        apt-get install -y --no-install-recommends swaybg
    fi

    if theme_up_to_date; then
        echo "Plymouth theme already installed and current; initramfs not rebuilt."
    else
        echo "Installing the Plymouth theme..."
        rm -rf "$THEME_DST"
        mkdir -p "$THEME_DST"
        cp "$THEME_SRC"/foxdash.plymouth "$THEME_SRC"/foxdash.script "$THEME_SRC"/*.png "$THEME_DST/"
        echo "Setting it as the boot theme (rebuilds the initramfs for every installed kernel; can take 10+ minutes on a Zero 2 W)..."
        plymouth-set-default-theme -R foxdash
    fi

    echo "Quietening the kernel command line..."
    local args
    args="$(cat "$CMDLINE")"
    args="$(echo "$args" | sed -E 's/console=tty1/console=tty3/')"
    for a in quiet splash "${QUIET_ARGS[@]}"; do
        echo " $args " | grep -q " $a " || args="$args $a"
    done
    printf '%s\n' "$args" > "$CMDLINE"
    grep -q '^disable_splash=1' "$CONFIG" || printf '# FoxDash boot experience\ndisable_splash=1\n' >> "$CONFIG"

    echo "Installing the blank cursor theme..."
    make_blank_cursor_theme

    echo "Installing the FoxDash Kiosk session..."
    install -m 0755 "$SCRIPT_DIR/kiosk/foxdash-session" "$SESSION_BIN"
    install -m 0644 "$SCRIPT_DIR/kiosk/foxdash-kiosk.desktop" "$SESSION_FILE"

    local kiosk_dir="$TARGET_HOME/.config/foxdash-kiosk"
    as_user mkdir -p "$kiosk_dir" "$TARGET_HOME/.local/share/foxdash" "$TARGET_HOME/.config/lxterminal"
    as_user cp "$SCRIPT_DIR/kiosk/labwc/rc.xml" "$SCRIPT_DIR/kiosk/labwc/environment" "$kiosk_dir/"
    sed "s|@FOXDASH_DIR@|$FOXDASH_DIR|g" "$SCRIPT_DIR/kiosk/labwc/autostart" | as_user tee "$kiosk_dir/autostart" >/dev/null
    as_user cp "$SCRIPT_DIR/boot/foxdash-boot-wallpaper.png" "$TARGET_HOME/.local/share/foxdash/boot-wallpaper.png"
    local profile="$TARGET_HOME/.config/lxterminal/lxterminal-foxdash.conf"
    [ -f "$profile" ] && as_user cp -a "$profile" "$profile.before-boot-experience"
    as_user cp "$SCRIPT_DIR/kiosk/lxterminal-foxdash.conf" "$profile"
    [ -f "$TARGET_HOME/.config/kanshi/config" ] || echo "WARNING: no ~/.config/kanshi/config; the display will not be rotated."

    set_lightdm_session foxdash-kiosk
    echo
    echo "Installed. It takes effect at the next boot."
    echo "Undo with: sudo $SCRIPT_DIR/install_boot_experience.sh --uninstall"
    echo "Backups: $BACKUP_DIR"
}

case "${1:-}" in
    --uninstall) do_uninstall ;;
    "") do_install ;;
    *) die "usage: $0 [--uninstall]" ;;
esac
