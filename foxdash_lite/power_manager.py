from __future__ import annotations

"""Ignition (external power) detection and graceful shutdown.

The SEENGREAT supercapacitor UPS drives its DetcVIN signal into GPIO BCM 10:
HIGH while external (ignition) power is present, LOW while FoxDash is running
on the capacitors.  This module turns that one wire into three explicit states:

    NORMAL  --power lost-->  PENDING  --countdown expired + LOW recheck-->  COMMITTED
       ^                        |
       +-----power restored-----+

PENDING is fully reversible: it is nothing more than a deadline held by the
state machine.  There are no timers, callbacks or render loops attached to it,
so cancelling simply clears the deadline and the next power loss starts a
completely fresh countdown.  Only COMMITTED performs irreversible work, and it
does so exactly once.

Design notes
- The pure ``PowerStateMachine`` owns all decisions and takes an explicit
  monotonic ``now``, so tests drive it with a fake clock.
- ``PowerManager`` is a small polling worker (default 10 Hz, one ioctl per poll)
  that publishes an immutable ``PowerStatus``.  The dashboard reads that
  status on its existing refresh tick; it never blocks rendering, telemetry or
  LEDs.
- GPIO read failures are never interpreted as power loss.  A countdown already
  running keeps running (the car may genuinely be off), but shutdown is only
  committed after a *successful* fresh read that still says LOW.
- Both transitions are debounced (default 0.25 s).  Without that, a noisy
  signal while on capacitors could repeatedly reset the countdown until the
  capacitors ran flat.
"""

import datetime as dt
import enum
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol


DEFAULT_GPIO_CHIP = "/dev/gpiochip0"
DEFAULT_POWER_GPIO = 10
DEFAULT_SHUTDOWN_DELAY_S = 60.0
DEFAULT_DEBOUNCE_S = 0.25
DEFAULT_POLL_INTERVAL_S = 0.1
GPIO_REOPEN_INTERVAL_S = 2.0
FINAL_RECHECK_SAMPLES = 3
FINAL_RECHECK_GAP_S = 0.1


class PowerState(str, enum.Enum):
    NORMAL = "normal"
    PENDING = "shutdown_pending"
    COMMITTED = "shutdown_committed"


class PowerEvent(str, enum.Enum):
    POWER_PRESENT = "external power present"
    POWER_LOST = "external power lost"
    POWER_RESTORED = "external power restored"
    COUNTDOWN_STARTED = "shutdown countdown started"
    COUNTDOWN_CANCELLED = "shutdown countdown cancelled"
    EXPIRY_AWAITING_READ = "countdown expired but GPIO unreadable; shutdown withheld"
    EXPIRY_AWAITING_LOW = "countdown expired but GPIO reads HIGH; shutdown withheld"
    COMMITTED = "shutdown committed"
    DRY_RUN_RESET = "dry-run shutdown reset by power restoration"


@dataclass(frozen=True)
class PowerConfig:
    enabled: bool = False
    gpio_pin: int = DEFAULT_POWER_GPIO
    gpio_chip: str = DEFAULT_GPIO_CHIP
    shutdown_delay_s: float = DEFAULT_SHUTDOWN_DELAY_S
    dry_run: bool = False
    debounce_s: float = DEFAULT_DEBOUNCE_S
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S

    def __post_init__(self) -> None:
        if self.gpio_pin < 0:
            raise ValueError("Power GPIO must be a non-negative BCM number")
        if not 5.0 <= self.shutdown_delay_s <= 3600.0:
            raise ValueError("Shutdown delay must be between 5 and 3600 seconds")
        if not 0.0 <= self.debounce_s <= 5.0:
            raise ValueError("Power debounce must be between 0 and 5 seconds")
        if not 0.02 <= self.poll_interval_s <= 1.0:
            raise ValueError("Power poll interval must be between 0.02 and 1 second")


@dataclass(frozen=True)
class PowerStatus:
    """Immutable snapshot read by the UI on its own refresh tick."""

    enabled: bool = False
    state: PowerState = PowerState.NORMAL
    external_power: bool | None = None
    remaining_s: float | None = None
    shutdown_delay_s: float = DEFAULT_SHUTDOWN_DELAY_S
    dry_run: bool = False
    gpio_ok: bool = False
    gpio_error: str = ""
    countdown_id: int = 0
    detail: str = ""

    @property
    def countdown_active(self) -> bool:
        return self.state is PowerState.PENDING

    @property
    def progress(self) -> float:
        """Fraction of the countdown still remaining, 1.0 -> 0.0."""
        if self.remaining_s is None or self.shutdown_delay_s <= 0:
            return 0.0
        return max(0.0, min(1.0, self.remaining_s / self.shutdown_delay_s))


DISABLED_STATUS = PowerStatus()


class PowerStateMachine:
    """Pure decision logic. Holds no threads, timers or hardware handles."""

    def __init__(self, *, shutdown_delay_s: float, debounce_s: float, dry_run: bool) -> None:
        self.shutdown_delay_s = float(shutdown_delay_s)
        self.debounce_s = float(debounce_s)
        self.dry_run = bool(dry_run)
        self.state = PowerState.NORMAL
        self.external_power: bool | None = None  # debounced
        self.countdown_id = 0
        self._raw: bool | None = None
        self._raw_since: float | None = None
        self._deadline: float | None = None
        self._expiry_block: PowerEvent | None = None

    def remaining(self, now: float) -> float | None:
        if self.state is not PowerState.PENDING or self._deadline is None:
            return None
        return max(0.0, self._deadline - now)

    def observe(self, reading: bool | None, now: float) -> list[PowerEvent]:
        """Feed one GPIO sample (``None`` = read failed) at monotonic ``now``."""
        events: list[PowerEvent] = []
        if reading is not None:
            if reading != self._raw:
                self._raw = reading
                self._raw_since = now
            assert self._raw_since is not None
            if reading != self.external_power and (now - self._raw_since) >= self.debounce_s:
                events.extend(self._debounced_change(reading))

        if self.state is PowerState.PENDING and self._deadline is not None and now >= self._deadline:
            events.extend(self._expiry(reading))
        return events

    def _debounced_change(self, present: bool) -> list[PowerEvent]:
        previous = self.external_power
        self.external_power = present
        if present:
            events = [PowerEvent.POWER_PRESENT if previous is None else PowerEvent.POWER_RESTORED]
            if self.state is PowerState.PENDING:
                self._reset_countdown()
                events.append(PowerEvent.COUNTDOWN_CANCELLED)
            elif self.state is PowerState.COMMITTED and self.dry_run:
                self._reset_countdown()
                events.append(PowerEvent.DRY_RUN_RESET)
            # A real COMMITTED state is terminal: shutdown work has started.
            return events

        events = [PowerEvent.POWER_LOST]
        if self.state is PowerState.NORMAL:
            # Count from the first LOW sample, not from debounce confirmation:
            # the capacitors started draining then.
            assert self._raw_since is not None
            self.countdown_id += 1
            self._deadline = self._raw_since + self.shutdown_delay_s
            self._expiry_block = None
            self.state = PowerState.PENDING
            events.append(PowerEvent.COUNTDOWN_STARTED)
        return events

    def _expiry(self, reading: bool | None) -> list[PowerEvent]:
        # Commit only on a successful, fresh LOW read taken right now.
        if reading is False:
            self.state = PowerState.COMMITTED
            self._deadline = None
            self._expiry_block = None
            return [PowerEvent.COMMITTED]
        block = PowerEvent.EXPIRY_AWAITING_READ if reading is None else PowerEvent.EXPIRY_AWAITING_LOW
        if block is self._expiry_block:
            return []  # already reported; avoid logging every poll
        self._expiry_block = block
        return [block]

    def _reset_countdown(self) -> None:
        self.state = PowerState.NORMAL
        self._deadline = None
        self._expiry_block = None


class GpioInput(Protocol):
    def read(self) -> bool: ...
    def close(self) -> None: ...


def _import_gpiod() -> Any:
    """Use Raspberry Pi OS's python3-libgpiod, which a plain venv hides.

    Same narrow fallback as the BH1750 path: no pip install or internet
    connection is required on the car Pi.
    """
    try:
        import gpiod  # type: ignore[import-not-found]
        return gpiod
    except ImportError:
        pass
    for candidate in (Path("/usr/lib/python3/dist-packages"), Path("/usr/local/lib/python3/dist-packages")):
        text = str(candidate)
        if candidate.is_dir() and text not in sys.path:
            sys.path.append(text)
            try:
                import gpiod  # type: ignore[import-not-found]
                return gpiod
            except ImportError:
                continue
    raise RuntimeError("python3-libgpiod is not available (sudo apt install python3-libgpiod)")


class GpiodInput:
    """libgpiod v2 character-device input with the internal pull-down enabled.

    Character-device access needs only membership of the ``gpio`` group, no
    root.  The pull-down only applies while this request is held, which is
    exactly the FoxDash lifetime.
    """

    def __init__(self, chip: str, pin: int, *, consumer: str = "foxdash-power") -> None:
        gpiod = _import_gpiod()
        from gpiod.line import Bias, Direction, Value  # type: ignore[import-not-found]

        self._pin = int(pin)
        self._active = Value.ACTIVE
        self._request = gpiod.request_lines(
            chip,
            consumer=consumer,
            config={self._pin: gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_DOWN)},
        )

    def read(self) -> bool:
        return self._request.get_value(self._pin) == self._active

    def close(self) -> None:
        try:
            self._request.release()
        except Exception:
            pass


class SystemPower:
    """Request power-off/reboot without running FoxDash as root.

    1. logind via ``systemctl --no-ask-password``.  When FoxDash runs in the
       active autologin desktop session (normal boot), polkit's default
       ``allow_active=yes`` permits this with no extra configuration.
    2. Fallback: ``sudo -n /usr/bin/systemctl poweroff|reboot``, permitted by
       the narrow rule from scripts/linux/setup_power_control.sh.  This covers
       FoxDash being (re)started outside the active session, e.g. over SSH,
       where polkit answers "challenge".
    ``--no-ask-password`` and ``sudo -n`` guarantee we never block on a prompt.
    """

    SYSTEMCTL = "/usr/bin/systemctl"
    BUSCTL = "/usr/bin/busctl"
    SUDO = "/usr/bin/sudo"

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self._run = runner

    def _call(self, argv: list[str], timeout: float) -> tuple[bool, str]:
        try:
            result = self._run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        detail = (result.stderr or result.stdout or "").strip()
        return result.returncode == 0, detail or f"exit {result.returncode}"

    def _request(self, verb: str) -> tuple[bool, str]:
        ok, detail = self._call([self.SYSTEMCTL, "--no-ask-password", verb], 15.0)
        if ok:
            return True, f"logind accepted {verb}"
        sudo_ok, sudo_detail = self._call([self.SUDO, "-n", self.SYSTEMCTL, verb], 15.0)
        if sudo_ok:
            return True, f"logind refused ({detail}); sudo accepted {verb}"
        return False, f"logind refused ({detail}); sudo refused ({sudo_detail})"

    def poweroff(self) -> tuple[bool, str]:
        return self._request("poweroff")

    def reboot(self) -> tuple[bool, str]:
        return self._request("reboot")

    def can_poweroff(self) -> str:
        """logind's verdict for *this* process: yes / challenge / no / na."""
        ok, detail = self._call(
            [self.BUSCTL, "call", "org.freedesktop.login1", "/org/freedesktop/login1",
             "org.freedesktop.login1.Manager", "CanPowerOff"],
            5.0,
        )
        if not ok:
            return f"unknown ({detail})"
        return detail.removeprefix("s ").strip('"') or "unknown"

    def sudo_fallback_available(self) -> bool:
        """True only for a *passwordless* rule covering ``systemctl poweroff``.

        ``sudo -n -l`` merely lists policy; it never executes anything.  A
        plain ``sudo -l CMD`` check is not enough: an admin user's ordinary
        password-protected ``ALL`` rule would also "permit" the command.
        """
        try:
            result = self._run([self.SUDO, "-n", "-l"], capture_output=True, text=True, timeout=5.0, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        target = f"{self.SYSTEMCTL} poweroff"
        return any("NOPASSWD:" in line and target in line for line in result.stdout.splitlines())

    def describe_permissions(self) -> str:
        return (
            f"logind CanPowerOff={self.can_poweroff()}, "
            f"sudo fallback={'available' if self.sudo_fallback_available() else 'NOT configured'}"
        )


class PowerManager:
    """Polls the ignition GPIO off the UI thread and publishes ``PowerStatus``.

    ``on_commit`` runs once, on this worker thread, when a real (non dry-run)
    shutdown is committed.  In dry-run mode the decision is logged instead.
    """

    def __init__(
        self,
        config: PowerConfig,
        *,
        log: Callable[[str], None],
        on_commit: Callable[[], None] | None = None,
        gpio_factory: Callable[[], GpioInput] | None = None,
        clock: Callable[[], float] = time.monotonic,
        startup_probe: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self._log = log
        self._on_commit = on_commit
        self._gpio_factory = gpio_factory or (lambda: GpiodInput(config.gpio_chip, config.gpio_pin))
        self._clock = clock
        self._startup_probe = startup_probe
        self._machine = PowerStateMachine(
            shutdown_delay_s=config.shutdown_delay_s,
            debounce_s=config.debounce_s,
            dry_run=config.dry_run,
        )
        self._gpio: GpioInput | None = None
        self._next_open_at = 0.0
        self._gpio_error = ""
        self._gpio_ok = False
        self._commit_fired = False
        self._lock = threading.Lock()
        self._status = PowerStatus(
            enabled=True,
            shutdown_delay_s=config.shutdown_delay_s,
            dry_run=config.dry_run,
            detail="starting",
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._log(
            f"Power management: GPIO{self.config.gpio_pin} on {self.config.gpio_chip} (pull-down), "
            f"shutdown delay {self.config.shutdown_delay_s:g}s, debounce {self.config.debounce_s:g}s, "
            f"{'DRY RUN' if self.config.dry_run else 'LIVE'}"
        )
        self._thread = threading.Thread(target=self._run, name="foxdash-power", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Idempotent. Safe to call from the worker itself (it just flags)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def status(self) -> PowerStatus:
        with self._lock:
            return self._status

    # -- worker ----------------------------------------------------------
    def _run(self) -> None:
        if self._startup_probe is not None:
            try:
                self._log(f"Power management: shutdown permission: {self._startup_probe()}")
            except Exception as exc:
                self._log(f"Power management: CanPowerOff probe failed: {type(exc).__name__}: {exc}")
        interval = self.config.poll_interval_s
        try:
            while not self._stop.is_set():
                self.poll_once()
                self._stop.wait(interval)
        finally:
            # Only the worker touches the GPIO handle, so only it releases it.
            self._close_gpio()

    def read_now(self) -> bool | None:
        """One GPIO read with explicit error handling; ``None`` means unknown."""
        now = self._clock()
        if self._gpio is None:
            if now < self._next_open_at:
                return None
            try:
                self._gpio = self._gpio_factory()
            except Exception as exc:
                self._next_open_at = now + GPIO_REOPEN_INTERVAL_S
                self._record_gpio_error(f"open failed: {type(exc).__name__}: {exc}")
                return None
        try:
            value = bool(self._gpio.read())
        except Exception as exc:
            self._close_gpio()
            self._next_open_at = now + GPIO_REOPEN_INTERVAL_S
            self._record_gpio_error(f"read failed: {type(exc).__name__}: {exc}")
            return None
        if not self._gpio_ok:
            if self._gpio_error:
                self._log(f"Power GPIO{self.config.gpio_pin} recovered (was: {self._gpio_error})")
            self._gpio_ok = True
            self._gpio_error = ""
        return value

    def poll_once(self) -> list[PowerEvent]:
        reading = self.read_now()
        now = self._clock()
        events = self._machine.observe(reading, now)
        for event in events:
            self._log_event(event, now)
        self._publish(now)
        if PowerEvent.COMMITTED in events:
            self._handle_commit()
        return events

    def _handle_commit(self) -> None:
        if self.config.dry_run:
            self._log(
                "Power DRY RUN: shutdown conditions met. Would now stop FoxDash, turn off the BlinkStick, "
                "dim the backlight and request systemctl poweroff. Restore power to reset."
            )
            return
        if self._commit_fired:
            return
        self._commit_fired = True
        if self._on_commit is None:
            self._log("Power: shutdown committed but no shutdown handler is configured")
            return
        try:
            self._on_commit()
        except Exception as exc:
            self._log(f"Power: shutdown handler failed: {type(exc).__name__}: {exc}")

    # -- helpers ---------------------------------------------------------
    def _record_gpio_error(self, message: str) -> None:
        # Log transitions and changed errors only, never every failed poll.
        if self._gpio_ok or message != self._gpio_error:
            self._log(f"Power GPIO{self.config.gpio_pin} error: {message}")
        self._gpio_ok = False
        self._gpio_error = message

    def _close_gpio(self) -> None:
        gpio, self._gpio = self._gpio, None
        if gpio is not None:
            gpio.close()

    def _log_event(self, event: PowerEvent, now: float) -> None:
        machine = self._machine
        if event is PowerEvent.COUNTDOWN_STARTED:
            remaining = machine.remaining(now)
            self._log(
                f"Power: {event.value} #{machine.countdown_id} "
                f"({machine.shutdown_delay_s:g}s, {remaining:.1f}s remaining)"
            )
        elif event is PowerEvent.COUNTDOWN_CANCELLED:
            self._log(f"Power: {event.value} #{machine.countdown_id}")
        else:
            self._log(f"Power: {event.value}")

    def _publish(self, now: float) -> None:
        machine = self._machine
        status = PowerStatus(
            enabled=True,
            state=machine.state,
            external_power=machine.external_power,
            remaining_s=machine.remaining(now),
            shutdown_delay_s=machine.shutdown_delay_s,
            dry_run=self.config.dry_run,
            gpio_ok=self._gpio_ok,
            gpio_error=self._gpio_error,
            countdown_id=machine.countdown_id,
            detail=self._status.detail if machine.state is PowerState.COMMITTED else "",
        )
        with self._lock:
            self._status = status

    def set_detail(self, detail: str) -> None:
        """Short progress text shown while COMMITTED (e.g. 'Saving logs')."""
        with self._lock:
            self._status = replace(self._status, detail=detail)


class PowerEventLog:
    """Append-only, timestamped, fsynced record of power transitions.

    Raspberry Pi OS keeps the journal in RAM here, and session logs are closed
    before power-off, so this file is the durable evidence of what happened
    across boots (countdowns, cancellations, the final power-off request).
    Writes happen only on transitions, never per poll.
    """

    def __init__(self, path: str | Path, *, session_id: str = "") -> None:
        self.path = Path(path)
        self.session_id = session_id
        self._lock = threading.Lock()

    @staticmethod
    def timestamp() -> str:
        return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")

    def write(self, message: str) -> None:
        line = f"{self.timestamp()} {self.session_id} {message.rstrip()}\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except (OSError, ValueError):
                pass  # logging must never interfere with shutdown


class ShutdownSequence:
    """The irreversible part. Runs at most once, on the power worker thread.

    Order: persist/stop application work -> lights off (done by the LED stop)
    -> dim backlight -> confirm power is still absent -> ask logind.  If power
    came back while we were tidying up, reboot instead of halting: a halted Pi
    on a still-powered UPS would otherwise stay dark until the capacitors fully
    discharge (see docs/power-management.md).
    """

    def __init__(
        self,
        *,
        stop_application: Callable[[], None],
        dim_display: Callable[[], None],
        read_external_power: Callable[[], bool | None],
        system: SystemPower,
        log: Callable[[str], None],
        set_detail: Callable[[str], None] = lambda _detail: None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._stop_application = stop_application
        self._dim_display = dim_display
        self._read = read_external_power
        self._system = system
        self._log = log
        self._set_detail = set_detail
        self._sleep = sleep
        self._lock = threading.Lock()
        self._ran = False
        self.outcome = ""

    def __call__(self) -> None:
        with self._lock:
            if self._ran:
                return
            self._ran = True
        started = time.monotonic()
        self._log("Power: shutdown sequence starting (stopping FoxDash workers, saving logs)")
        self._set_detail("Saving logs")
        self._safely("stop application", self._stop_application)
        self._set_detail("Lights off")
        self._safely("dim display", self._dim_display)
        if self._power_confirmed_restored():
            self._log("Power: external power returned during shutdown preparation; requesting reboot instead")
            self._set_detail("Power returned - restarting")
            ok, detail = self._system.reboot()
            self.outcome = "reboot" if ok else "reboot-failed"
        else:
            self._set_detail("Powering off")
            self._log(f"Power: requesting systemctl poweroff ({time.monotonic() - started:.1f}s after commit)")
            ok, detail = self._system.poweroff()
            self.outcome = "poweroff" if ok else "poweroff-failed"
        self._log(f"Power: {self.outcome}: {detail}")
        if not ok:
            self._set_detail("Power-off request failed")

    def _power_confirmed_restored(self) -> bool:
        # Require every sample to be a successful HIGH; unknown never counts.
        for index in range(FINAL_RECHECK_SAMPLES):
            if self._read() is not True:
                return False
            if index + 1 < FINAL_RECHECK_SAMPLES:
                self._sleep(FINAL_RECHECK_GAP_S)
        return True

    def _safely(self, label: str, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:
            self._log(f"Power: {label} failed: {type(exc).__name__}: {exc}")
