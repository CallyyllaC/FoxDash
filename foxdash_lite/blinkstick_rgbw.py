#!/usr/bin/env python3
"""Raw RGBW frame transport for a BlinkStick Pro channel.

The BlinkStick Pro's WS2812 mode accepts an arbitrary byte stream on each
output channel. This driver deliberately treats the R output as a raw data
lane and writes one four-byte GRBW record per physical RGBW LED.

Connection behaviour intentionally matches the user's proven BlinkStick
utility: finding the device is mandatory, but selecting mode 2 is best-effort.
Some BlinkStick/Windows combinations reject or fail to acknowledge a mode
change even though the existing device mode can still drive the strip.
"""

from __future__ import annotations

import time
from typing import Final, Optional, Sequence, Tuple

try:
    from blinkstick import blinkstick
except ImportError as exc:  # pragma: no cover - hardware dependency
    blinkstick = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

RGBW = Tuple[int, int, int, int]
GRBW_ORDER: Final[str] = "GRBW"
BLINKSTICK_RED_CHANNEL: Final[int] = 0


def _byte(value: int | float) -> int:
    return max(0, min(255, int(round(value))))


class BlinkStickProRgbw:
    """Buffered RGBW transport over the BlinkStick Pro R output."""

    def __init__(
        self,
        led_count: int = 24,
        *,
        fps: float = 40.0,
        usb_ma_budget: float = 300.0,
        estimated_pixel_max_ma: float = 50.0,
    ) -> None:
        if led_count <= 0:
            raise ValueError("led_count must be positive")
        if fps <= 0:
            raise ValueError("fps must be positive")
        if usb_ma_budget <= 0:
            raise ValueError("usb_ma_budget must be positive")
        if estimated_pixel_max_ma <= 0:
            raise ValueError("estimated_pixel_max_ma must be positive")

        self.led_count = int(led_count)
        self.fps = float(fps)
        self.usb_ma_budget = float(usb_ma_budget)
        self.estimated_pixel_max_ma = float(estimated_pixel_max_ma)
        self.user_brightness = 0.55
        self._pixels: list[RGBW] = [(0, 0, 0, 0)] * self.led_count
        self._device: Optional[object] = None
        self._last_frame = time.monotonic()

    @property
    def frame_byte_count(self) -> int:
        return self.led_count * 4

    @property
    def usb_limit(self) -> float:
        ceiling = self.usb_ma_budget / (self.led_count * self.estimated_pixel_max_ma)
        return max(0.01, min(1.0, ceiling))

    @property
    def effective_brightness(self) -> float:
        return max(0.0, min(1.0, self.usb_limit * self.user_brightness))

    def connect(self) -> None:
        if blinkstick is None:
            raise RuntimeError(
                "BlinkStick Python package is missing. Install it with: py -m pip install blinkstick"
            ) from _IMPORT_ERROR

        # Deliberately the same discovery call as the known-working old app.
        device = blinkstick.find_first()
        if device is None:
            raise RuntimeError("No BlinkStick device found. Check the USB cable and use the same Python install as the working test.")

        self._device = device

        # Best effort only. Do not abort if Windows/the firmware refuses the
        # mode command, and do not send an initial all-off frame here. This
        # mirrors the previously working driver exactly where it matters.
        try:
            device.set_mode(2)  # type: ignore[attr-defined]
        except Exception:
            pass

    def set_brightness(self, brightness: float) -> None:
        self.user_brightness = max(0.0, min(1.0, float(brightness)))

    def set_pixel(self, index: int, colour: RGBW) -> None:
        if not 0 <= index < self.led_count:
            raise IndexError(f"Pixel index {index} is outside 0..{self.led_count - 1}")
        r, g, b, w = colour
        self._pixels[index] = (_byte(r), _byte(g), _byte(b), _byte(w))

    def set_frame(self, frame: Sequence[RGBW]) -> None:
        if len(frame) != self.led_count:
            raise ValueError(f"Expected {self.led_count} pixels, got {len(frame)}")
        self._pixels = [tuple(_byte(value) for value in pixel) for pixel in frame]  # type: ignore[list-item]

    def fill(self, colour: RGBW) -> None:
        r, g, b, w = colour
        safe = (_byte(r), _byte(g), _byte(b), _byte(w))
        self._pixels = [safe] * self.led_count

    def clear(self) -> None:
        self._pixels = [(0, 0, 0, 0)] * self.led_count

    def build_frame(self) -> list[int]:
        """Return exact raw bytes: 24 physical RGBW LEDs = 96 GRBW bytes."""
        brightness = self.effective_brightness
        data: list[int] = []
        for r, g, b, w in self._pixels:
            data.extend((
                _byte(g * brightness),
                _byte(r * brightness),
                _byte(b * brightness),
                _byte(w * brightness),
            ))
        return data

    def show(self) -> None:
        if self._device is None:
            raise RuntimeError("BlinkStick is not connected. Call connect() first.")
        self._device.set_led_data(BLINKSTICK_RED_CHANNEL, self.build_frame())  # type: ignore[attr-defined]
        self._last_frame = time.monotonic()

    def off(self) -> None:
        self.clear()
        if self._device is not None:
            self._device.set_led_data(BLINKSTICK_RED_CHANNEL, [0] * self.frame_byte_count)  # type: ignore[attr-defined]
        self._last_frame = time.monotonic()

    def wait_for_next_frame(self) -> None:
        target = 1.0 / self.fps
        remaining = target - (time.monotonic() - self._last_frame)
        if remaining > 0:
            time.sleep(remaining)

    def close(self) -> None:
        try:
            self.off()
        except Exception:
            pass
        self._device = None
