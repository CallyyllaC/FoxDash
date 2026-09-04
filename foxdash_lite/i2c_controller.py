from __future__ import annotations

import datetime as dt
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .runtime_types import EnvironmentSnapshot


BH1750_CONTINUOUS_HIGH_RESOLUTION_MODE = 0x10
BH1750_ONE_TIME_HIGH_RESOLUTION_MODE = 0x20
BH1750_POWER_DOWN = 0x00
BH1750_LUX_DIVISOR = 1.2
BH1750_HIGH_RESOLUTION_MAX_WAIT_S = 0.18
DEFAULT_I2C_BUS = 11
DEFAULT_BH1750_ADDRESS = 0x23
DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_FILTER_TAU_S = 6.0


class I2cController:
    """Own the low-rate BH1750 daylight sensor path.

    The controller deliberately produces two values:
    - ``ambient_lux_raw`` is the sensor reading as reported by the BH1750;
    - ``ambient_lux_filtered`` is a gentle time-based EMA reserved for future
      palette/LED control.

    FoxDash currently *logs* both values but does not enable ambient-driven
    brightness automatically. Calibration comes after real in-car data, not
    after a phone-torch experiment and a burst of misplaced confidence.
    """

    def __init__(
        self,
        publish_environment: Callable[[EnvironmentSnapshot], Any],
        *,
        bus_number: int = DEFAULT_I2C_BUS,
        address: int = DEFAULT_BH1750_ADDRESS,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        filter_tau_s: float = DEFAULT_FILTER_TAU_S,
    ) -> None:
        if bus_number < 0:
            raise ValueError("I²C bus number must be non-negative")
        if not 0 <= address <= 0x7F:
            raise ValueError("BH1750 address must be a 7-bit I²C address")

        self._publish = publish_environment
        self.bus_number = int(bus_number)
        self.address = int(address)
        self.poll_interval_s = max(0.2, float(poll_interval_s))
        self.filter_tau_s = max(0.1, float(filter_tau_s))

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._bus: Any | None = None
        self._i2c_msg: Any | None = None
        self._filtered_lux: float | None = None
        self._last_sample_monotonic: float | None = None
        self._sample = 0
        self._last_error_signature = ""

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="foxdash-i2c", daemon=True)
        self._thread.start()

    @staticmethod
    def _timestamp() -> str:
        return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")

    def _publish_status(self, *, state: str, error: str = "") -> None:
        self._publish(EnvironmentSnapshot(
            sensor_ok=False,
            light_state=state,
            sensor_bus=self.bus_number,
            sensor_address=self.address,
            sensor_error=error,
            updated_at=self._timestamp(),
            sample=self._sample,
        ))

    @staticmethod
    def _smbus_backend():
        """Return an SMBus implementation and optional raw-I²C message helper.

        ``smbus2`` is the preferred portable dependency.  The Pi already has
        ``python3-smbus`` installed for the sensor bring-up, however, and a
        normal venv hides Debian's ``dist-packages``.  The narrow fallback below
        reuses that local package rather than requiring the car Pi to invent an
        internet connection merely to read daylight.
        """
        try:
            from smbus2 import SMBus, i2c_msg
            return SMBus, i2c_msg
        except ImportError:
            pass

        try:
            from smbus import SMBus
            return SMBus, None
        except ImportError:
            pass

        for candidate in (
            Path("/usr/lib/python3/dist-packages"),
            Path("/usr/local/lib/python3/dist-packages"),
        ):
            text = str(candidate)
            if candidate.is_dir() and text not in sys.path:
                sys.path.append(text)
                try:
                    from smbus import SMBus
                    return SMBus, None
                except ImportError:
                    continue
        raise RuntimeError(
            "No SMBus implementation is available. Install smbus2 in FoxDash's venv "
            "or install the Raspberry Pi package python3-smbus."
        )

    def _open_bus(self) -> None:
        SMBus, self._i2c_msg = self._smbus_backend()
        self._bus = SMBus(self.bus_number)
        if self._i2c_msg is not None:
            self._bus.write_byte(self.address, BH1750_CONTINUOUS_HIGH_RESOLUTION_MODE)
            # The BH1750 needs up to 180 ms for its first high-resolution
            # sample. Use the documented maximum before the first raw read.
            self._stop.wait(BH1750_HIGH_RESOLUTION_MAX_WAIT_S)

    def _close_bus(self) -> None:
        bus, self._bus = self._bus, None
        self._i2c_msg = None
        if bus is None:
            return
        try:
            bus.close()
        except Exception:
            pass

    def _read_raw_lux(self) -> float:
        if self._bus is None:
            self._open_bus()
        if self._bus is None:
            raise RuntimeError("BH1750 bus was not opened")

        if self._i2c_msg is not None:
            # BH1750 has no register address. Its read format is address+read
            # followed immediately by the high and low data bytes. Using
            # read_i2c_block_data(address, 0x00, 2) is not equivalent: SMBus
            # sends 0x00 as a command first, which means POWER DOWN to BH1750
            # and freezes the first conversion in the data register.
            message = self._i2c_msg.read(self.address, 2)
            self._bus.i2c_rdwr(message)
            data = list(message)
        else:
            # Debian's classic python3-smbus lacks raw i2c_msg reads. Preserve
            # the offline-Pi fallback by starting a fresh one-time conversion
            # before every read. One-time mode is already powered down once
            # conversion completes, so the command byte added by the legacy
            # block-read API cannot stop an in-progress measurement.
            self._bus.write_byte(self.address, BH1750_ONE_TIME_HIGH_RESOLUTION_MODE)
            self._stop.wait(BH1750_HIGH_RESOLUTION_MAX_WAIT_S)
            data = self._bus.read_i2c_block_data(self.address, BH1750_POWER_DOWN, 2)

        if len(data) != 2:
            raise RuntimeError(f"BH1750 returned {len(data)} bytes, expected 2")
        raw_count = (int(data[0]) << 8) | int(data[1])
        return raw_count / BH1750_LUX_DIVISOR

    def _filtered(self, raw_lux: float, now: float) -> float:
        previous = self._filtered_lux
        previous_at = self._last_sample_monotonic
        if previous is None or previous_at is None:
            self._filtered_lux = raw_lux
        else:
            elapsed_s = max(0.0, now - previous_at)
            alpha = 1.0 - math.exp(-elapsed_s / self.filter_tau_s)
            self._filtered_lux = previous + ((raw_lux - previous) * alpha)
        self._last_sample_monotonic = now
        return self._filtered_lux

    def _publish_reading(self, raw_lux: float, filtered_lux: float) -> None:
        self._sample += 1
        self._publish(EnvironmentSnapshot(
            ambient_lux_raw=raw_lux,
            ambient_lux_filtered=filtered_lux,
            sensor_ok=True,
            light_state="measuring",
            sensor_bus=self.bus_number,
            sensor_address=self.address,
            updated_at=self._timestamp(),
            sample=self._sample,
        ))

    def _run(self) -> None:
        self._publish_status(state="starting")
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                raw_lux = self._read_raw_lux()
                filtered_lux = self._filtered(raw_lux, started)
                self._last_error_signature = ""
                self._publish_reading(raw_lux, filtered_lux)
                delay_s = max(0.0, self.poll_interval_s - (time.monotonic() - started))
            except Exception as exc:
                self._close_bus()
                self._filtered_lux = None
                self._last_sample_monotonic = None
                signature = f"{type(exc).__name__}: {exc}"
                # Do not fill a CSV with identical failures every second when a
                # cable falls out. State still remains honest; the backoff keeps
                # the Pi from performing a tiny I²C panic attack.
                if signature != self._last_error_signature:
                    self._publish_status(state="error", error=signature)
                    self._last_error_signature = signature
                delay_s = max(2.0, self.poll_interval_s)
            self._stop.wait(delay_s)
        self._close_bus()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._close_bus()
