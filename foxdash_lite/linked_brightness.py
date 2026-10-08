from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from .backlight import HyperPixelBacklightController, USABLE_MAX, USABLE_MIN


def pwm_for_palette(percent: float) -> int:
    """Use the exact factor shared by the HyperPixel calibration preview.

    A palette of 1..100% maps linearly to the *usable* PWM range 6..56,
    independently of the kernel's much larger advertised hardware maximum.
    """
    if not math.isfinite(percent):
        raise ValueError("Palette percentage must be finite")
    factor = max(0.0, min(1.0, (percent - 1.0) / 99.0))
    return max(USABLE_MIN, min(USABLE_MAX, round(USABLE_MIN + (USABLE_MAX - USABLE_MIN) * factor)))


@dataclass(frozen=True)
class LinkedBacklightState:
    target_pwm: int | None
    actual_pwm: int | None
    error: str | None


class LinkedBacklightOutput:
    """Asynchronous, rate-limited HyperPixel writer for the linked brightness.

    sysfs may require a passwordless sudo subprocess. Do not run that on
    Textual's refresh thread and do not create 10 writes per second on a Pi.
    The worker keeps only the latest requested target and stops retrying a
    persistent failure; the colour palette and ambient logs continue.
    """

    def __init__(
        self,
        controller: HyperPixelBacklightController | None = None,
        *,
        min_write_interval_s: float = 0.35,
    ) -> None:
        self._controller = controller if controller is not None else HyperPixelBacklightController()
        self._min_write_interval_s = max(0.1, float(min_write_interval_s))
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._target_pwm: int | None = None
        self._last_sent_pwm: int | None = None
        self._actual_pwm: int | None = None
        self._error: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="foxdash-backlight", daemon=True)
        self._thread.start()

    def request_palette(self, percent: float) -> None:
        target = pwm_for_palette(percent)
        with self._lock:
            if self._error is not None or self._stop.is_set():
                return
            if target == self._target_pwm:
                return
            self._target_pwm = target
        self._wake.set()

    def snapshot(self) -> LinkedBacklightState:
        with self._lock:
            return LinkedBacklightState(self._target_pwm, self._actual_pwm, self._error)

    def _run(self) -> None:
        next_write_at = 0.0
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            while not self._stop.is_set():
                with self._lock:
                    target = self._target_pwm
                    if self._error is not None or target is None or target == self._last_sent_pwm:
                        break
                delay = max(0.0, next_write_at - time.monotonic())
                if self._stop.wait(delay):
                    return
                try:
                    status = self._controller.set(target)
                    succeeded = status.available and status.current == target
                    error = None if succeeded else (
                        status.error or f"Backlight returned {status.current}, wanted {target}"
                    )
                except Exception as exc:
                    succeeded = False
                    error = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    if succeeded:
                        self._last_sent_pwm = target
                        self._actual_pwm = target
                    else:
                        self._error = error
                if not succeeded:
                    break
                next_write_at = time.monotonic() + self._min_write_interval_s

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
