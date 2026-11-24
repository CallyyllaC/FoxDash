# Ignition detection and graceful shutdown

FoxDash watches the SEENGREAT supercapacitor UPS and shuts the Pi down cleanly
after the ignition has been off for a configurable time (default 60 s). It all
runs inside the dashboard process (`foxdash_lite/power_manager.py`). There is
no separate watcher script, daemon or root process.

## Hardware

| Item | Detail |
|---|---|
| Signal | UPS `DetcVIN` → GPIO BCM 10 (physical pin 19), common ground |
| Logic | HIGH = external power present, LOW = running on supercapacitors |
| Pull | Internal pull-down, requested by FoxDash through libgpiod |
| Display | HyperPixel 4.0 with `disable-touch`. GPIO 10 is physically isolated from the HyperPixel header |

Power management itself changes the launcher only by adding the FoxDash flags
below. The boot screen and kiosk session are separate; see
[boot-experience.md](boot-experience.md).
No other GPIO is used.

## Configuration

| Flag | Default | Meaning |
|---|---|---|
| `--enable-power-management` | off | Turn the feature on |
| `--power-gpio N` | `10` | BCM GPIO wired to `DetcVIN` |
| `--power-gpio-chip PATH` | `/dev/gpiochip0` | GPIO character device |
| `--power-shutdown-delay S` | `60` | Seconds on capacitors before shutdown is committed (5–3600) |
| `--power-debounce S` | `0.25` | How long a signal change must persist before it counts (0–5) |
| `--power-dry-run` | off | Run the countdown and the shutdown decision, but never stop FoxDash or power off |

The Pi launcher `deploy/raspberry-pi/run_dashboard_visible.sh` currently passes
`--enable-power-management --power-dry-run`. To arm real shutdowns, remove
`--power-dry-run` from that line, but only after the hardware checklist below.

One-time permission setup on the Pi (see [Permissions](#permissions)):

```bash
./scripts/linux/setup_power_control.sh
```

## Behaviour

### States

```
NORMAL --(LOW for 0.25 s)--> PENDING --(deadline passed AND a fresh read is LOW)--> COMMITTED
   ^                            |
   +----(HIGH for 0.25 s)-------+
```

* **NORMAL.** The dashboard runs as usual.
* **PENDING.** The dashboard keeps running as usual. Telemetry, logging, LEDs and
  ambient brightness are untouched. The only effect is the countdown banner.
  PENDING is just a deadline held by a pure state machine. It has no timers,
  callbacks, tasks or render loops attached, so cancelling clears one value
  and the next loss starts a completely fresh countdown.
* **COMMITTED.** This state is terminal and its work runs exactly once (see below). In dry-run
  mode it only logs the decision, and restoring power returns to NORMAL so the
  test can be repeated.

### Timing

* The clock is `time.monotonic()`.
* The countdown is measured from the first LOW sample, not from when the
  debounce confirmed it.
* GPIO is polled at 10 Hz on a dedicated thread (`foxdash-power`). One poll is
  a single ~30 µs ioctl.
* Restoration is detected within about 0.25–0.35 s (debounce plus one poll).

**Why both directions are debounced.** Live captures show that unplugging the
supply makes `DetcVIN` chatter for 0.44–0.73 s (35–44k edges) before it settles
LOW. While on capacitors there are also sporadic HIGH spikes shorter than
50 ms. Without a restore-side debounce those spikes could keep resetting the
countdown until the capacitors ran flat.

### GPIO failures

* A failed open or read is never treated as power loss. It is logged once per
  distinct error and on recovery, and retried every 2 s.
* While the GPIO is unreadable the status bar shows an amber `PWR ?`. The `d`
  debug panel has a `Power` line.
* A countdown that is already running keeps running, because the car may
  really be off. Shutdown is still committed only after a *successful* fresh
  read that says LOW.

### Startup

Startup without external power is handled. The first stable LOW starts a
normal countdown.

### Display

The countdown takes over the driving-cue strip under the status bar. It is the
same size and uses the same edge-lit treatment, so nothing shifts and the
telemetry cards stay visible.

* Line 1 reads "EXTERNAL POWER LOST · shutdown in Ns" in a slow amber pulse.
* Line 2 is a draining rail drawn like the score rails.
* In the final 10 s the edge, rail and text turn red and pulse faster.
* In dry-run mode the banner is prefixed with `DRY RUN`.

The banner is chosen on the dashboard's existing 10 Hz refresh tick, and only
CSS classes switch it (no inline styles). When power returns the next tick
renders the normal cue again. Regression tests cycle this 40 times and check
that the classes, text, inline styles and timer count are unchanged.

### Shutdown sequence (COMMITTED, live mode)

These steps run once, on the power worker thread:

1. The commit already required a fresh LOW read at the deadline. If that read
   is HIGH or unreadable, the commit is withheld and logged.
2. The backlight writer is stopped, then `runtime.stop()` runs. That stops the
   OBD reader and the BH1750 worker, makes the LED worker blank the BlinkStick
   on close, and closes all session logs (`psa_final_values_*`, unknown report
   and session text).
3. The HyperPixel backlight is dimmed to its safe minimum (PWM 6).
4. External power is rechecked: 3 reads, 0.1 s apart.
   * If all three read HIGH, power came back during tidy-up, so FoxDash
     requests `reboot` rather than halting.
   * Otherwise it requests `poweroff`.
5. The power-off or reboot request is sent (see Permissions).

The dashboard stays visible throughout and shows "POWERING OFF" with progress
text until systemd stops the session.

### Permissions

FoxDash runs as `awo` and never as root. The request path:

1. `systemctl --no-ask-password poweroff` goes through logind/polkit. Raspberry
   Pi OS's policy allows `org.freedesktop.login1.power-off` for the **active**
   session, and the autologin desktop session is active. When autostart
   launches FoxDash this works with no extra configuration.
2. Fallback: `sudo -n /usr/bin/systemctl poweroff|reboot`, allowed only by
   `/etc/sudoers.d/foxdash-power` (created by
   `scripts/linux/setup_power_control.sh`). It is needed when FoxDash runs
   outside the active session, for example after a restart over SSH, where
   polkit answers `challenge`.

At startup FoxDash logs both, for example
`shutdown permission: logind CanPowerOff=challenge, sudo fallback=available`.
The probe only lists permissions (`busctl … CanPowerOff`, `sudo -n -l`) and
never runs anything.

GPIO access needs only the `gpio` group (`/dev/gpiochip0`). libgpiod v2 comes
from Raspberry Pi OS's `python3-libgpiod`. The venv imports it from
`dist-packages`, the same fallback the BH1750 path uses for `smbus`. No pip
install is needed.

### Logging

Power transitions, cancellations, GPIO errors and shutdown steps are written to
three places. Individual polls are never logged.

* **`~/CarOBD/logs/power_events.log`**: the durable record. It is append-only,
  every line carries a millisecond timestamp and the session ID, and each line
  is fsynced. It is the only power record that survives a real power-off: this
  Pi's journal is RAM-only, and the session log is closed before power-off.
  Its name falls outside the `psa_*` journey-archive pattern, so archiving
  leaves it in place.
* The session text log (`psa_session_*.txt`), with timestamps.
* The system journal under the `foxdash-power` tag, for live tailing:
  `journalctl -t foxdash-power -f -o short-precise`.

## What happens if power returns late

| Power returns… | Result |
|---|---|
| During the countdown | It cancels within about 0.3 s, and the dashboard continues uninterrupted. Verified live. |
| At the deadline, before the debounce confirms it | The commit is withheld because the fresh read is HIGH, and the countdown cancels about 0.25 s later. Covered by tests. |
| While FoxDash is tidying up (step 2–4, about 1–5 s) | FoxDash requests **reboot** instead of power-off, so the dashboard comes back. Covered by tests, not yet validated on hardware. |
| After `systemctl poweroff` has been accepted | systemd finishes the shutdown and the Pi halts. Linux cannot cancel this. |
| After Linux has halted, while the UPS still supplies 5 V | **Unresolved hardware risk.** A halted Pi Zero 2 W restarts only on a 5 V power cycle or GPIO3 pulled low. GPIO3 is taken by the HyperPixel and may not be repurposed. If the UPS output never drops (ignition back on, capacitors topped up), the Pi may stay dark until the next ignition-off is long enough to drain the capacitors. This must be validated on hardware. Countdown cancellation working says nothing about this case. |

Options to evaluate if hardware testing confirms the stuck-halt case:

* Check whether the SEENGREAT board can cut its output after the Pi halts
  (some UPS boards do this with a timer or a "Pi alive" input).
* Use a `systemd-shutdown` hook that reads GPIO 10 just before the final halt
  and reboots instead if external power is present. This narrows the window
  but cannot close it.
* Add a hardware watchdog or a power-path change, such as a relay that drops
  the UPS output on ignition-on after a halt.

## Rollback

The pre-deployment installation and autostart entry are archived on the Pi:

```bash
ls ~/foxdash-backups/
# restore:
tar -C ~ -xzf ~/foxdash-backups/foxdash_lite_textual-pre-power-<timestamp>.tar.gz
```

To disable the feature without a rollback, remove the two power flags from
the launcher. To remove the sudo fallback:
`sudo rm /etc/sudoers.d/foxdash-power`.

## Validation so far (2026-11-24, dry-run, 5 V bench supply)

### Automated

Run with `python -m unittest tests.test_power_manager tests.test_power_ui`.
The tests use a mocked GPIO, clock, `subprocess` and system interface. They cover:

* normal operation, power loss, countdown progression, cancellation
* 50 repeated interruptions, each starting a fresh 60 s countdown
* chatter while on capacitors and while powered
* expiry committing exactly once
* restoration 0.4 s before expiry, and a HIGH read at the deadline withholding the commit
* startup without power
* GPIO open and read failures, including a failure during the countdown, which blocks the commit
* dry-run reset, configurable delays (5, 15 and 90 s), config validation
* the shutdown sequence: order, run-once under 8 threads, reboot only on confirmed restoration, preparation failures
* logind-then-sudo permission fallback, and a probe that never executes anything
* the persistent log
* memory stability over 200 cycles

The headless Textual tests cycle the banner 40 times and assert that the
classes, text and inline styles are restored and that no timers are added.

### Live on awocore (Pi Zero 2 W)

| Test | Result |
|---|---|
| Power loss → countdown | Pass. Detected about 0.3 s after unplug, countdown backdated to the first LOW sample. |
| Cancel after ~21 s | Pass. Banner removed, normal cue restored, telemetry, LEDs and brightness unaffected. |
| Repeated and rapid cycles (11.4 s, 3.5 s, 0.85 s, 1.5 s on capacitors) | Pass. Countdowns #2–#5 each started fresh and cancelled. |
| Run to expiry (dry run) | Pass. Committed exactly 60.0 s after the first LOW, "SHUTDOWN WOULD START NOW" shown, reset on restore. |
| Restore just before expiry | Pass. Restored 5.8 s before the deadline (red phase) and cancelled. A further attempt (#7) also cancelled. |
| Startup without external power | Pass. FoxDash restarted while on capacitors detected the loss at once and started a countdown. |
| GPIO pull-down | Pass. Kernel reports `bias=pull-down consumer="foxdash-power"`. |
| Permission probe | After a real boot, autostart in the active session gives `logind CanPowerOff=yes`. After an SSH restart it gives `challenge`, and the sudo fallback is then available. |
| CPU | FoxDash python 102.3% of one core against 102.0% before, which is noise. The power thread is about 0.5% of one core, and a GPIO read takes about 30 µs. lxterminal 26.6% against 26.7%. |
| Memory and threads | RSS 32.0 MB against 30.1 MB (+1.9 MB, libgpiod bindings), +1 thread. |
| Boot | Userspace 17.8 s against 17.9 s. FoxDash is not a systemd unit, and the GPIO opens on its own thread. |

**Signal observations.**

* `DetcVIN` chatters for 0.44–0.73 s (35–44k edges) on every unplug.
* It shows HIGH spikes under 50 ms while on capacitors.
* Re-plugging is clean (under 1 ms).

All of this was absorbed by the 0.25 s debounce.

**Brown-out observed.** After repeated drain tests with short recharges, the
Pi lost power seconds into a capacitor-powered restart. The countdown was then
under 15 s old. The capacitors were nowhere near the 144 s measured from full
charge. The filesystem recovered through ext4 orphan cleanup. This is direct
evidence that **a 60 s countdown is not safe after short ignition cycles**.
See checklist items 4 and 5.

## Outstanding hardware validation checklist

Real shutdowns are still disabled (`--power-dry-run`). Complete these before
removing that flag. Before each run, record the ambient temperature, the
supply, how long it charged, the display brightness and the LED state.

1. **Fully charged, cold (around 0–2 °C).** Charge for at least 10 minutes, unplug, and
   time until display instability. Repeat 3 times.
2. **Worst-case load.** Maximum backlight (PWM 56), BlinkStick at full test pattern
   (`scripts/linux/run_led_test.sh`), OBD connected. Measure the usable runtime.
3. **Actual graceful-shutdown duration.** Remove `--power-dry-run` and use
   `--power-shutdown-delay 10`. Unplug, then time from "shutdown committed" in
   `power_events.log` to the green ACT LED going out. Repeat 3 times. Verify
   the BlinkStick goes dark and the backlight dims. Verify the next boot's
   `power_events.log` shows the `requesting systemctl poweroff` line and that
   `dmesg` shows no ext4 orphan cleanup.
4. **Short ignition cycles and incomplete charge.** Use 10 s, 30 s and 60 s of external
   power between unplugs. Measure capacitor runtime after each. This decides
   whether 60 s is safe, or whether the delay must be shorter or adapt to time
   since power returned.
5. **Required margin.** The delay plus the measured shutdown duration plus a safety margin must
   stay below the *worst* runtime from items 1, 2 and 4.
6. **Restore during the countdown.** Already validated in dry run. Repeat once
   with live mode at a short delay.
7. **Restore during FoxDash tidy-up.** Plug back in within about 1 s of "shutdown
   committed". Expect "requesting reboot" in the log and the Pi to restart.
8. **Restore during OS shutdown.** Plug back in after the `requesting systemctl
   poweroff` line. Expect the Pi to halt. Record whether it ever restarts by
   itself.
9. **Restore after halt, UPS still powered.** Plug back in after the ACT LED stops.
   Record whether the Pi stays dark indefinitely and how long the capacitors
   take to drain enough for it to boot. This is the unresolved stuck-halt risk
   described above.
10. **Automatic restart after complete power loss.** Leave it unplugged until the UPS output
    collapses, then reconnect. Confirm the Pi boots and FoxDash starts.
11. **12 V input.** Repeat 1, 3 and 9 on the intended 12 V supply. With a multimeter or
    `pinctrl get 10`, verify that `DetcVIN` is a 3.3 V logic level (never above
    3.3 V) both with external power present and absent.
