from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BrightnessLevels:
    ui_percent: float
    led_percent: float
    palette_mode: str


class BrightnessPolicy:
    """Provisional, bounded daylight curve; tune with mounted-car lux logs.

    log1p spans dim cabins through daylight without letting a brief headlight
    or torch reading define the whole useful range.
    """

    DAYLIGHT_LUX = 1000.0
    UI_NIGHT_PERCENT = 25.0
    LED_NIGHT_PERCENT = 8.0
    LED_DAY_PERCENT = 65.0

    def resolve(self, ambient_lux: float | None) -> BrightnessLevels:
        if ambient_lux is None or not math.isfinite(ambient_lux) or ambient_lux < 0:
            return BrightnessLevels(ui_percent=80.0, led_percent=35.0, palette_mode="fallback")

        amount = math.log1p(min(ambient_lux, self.DAYLIGHT_LUX)) / math.log1p(self.DAYLIGHT_LUX)
        ui = self.UI_NIGHT_PERCENT + (100.0 - self.UI_NIGHT_PERCENT) * amount
        led = self.LED_NIGHT_PERCENT + (self.LED_DAY_PERCENT - self.LED_NIGHT_PERCENT) * amount
        mode = "night" if ambient_lux < 10.0 else "dusk" if ambient_lux < 80.0 else "day"
        return BrightnessLevels(ui_percent=ui, led_percent=led, palette_mode=mode)


class AmbientPaletteController:
    """Slew-limited brightness factor for linked palette and HyperPixel PWM.

    The BH1750 filter smooths the lux reading; this additional rate limit
    prevents abrupt visual changes. Missing, faulty or stale samples hold
    the last factor for both outputs until a fresh sensor sample arrives.
    """

    SENSOR_TIMEOUT_S = 4.0
    RISE_PERCENT_PER_S = 12.0
    FALL_PERCENT_PER_S = 7.0

    def __init__(self, *, initial_percent: float = 100.0) -> None:
        self.current_percent = max(1.0, min(100.0, float(initial_percent)))
        self.policy = BrightnessPolicy()
        self._last_sample: int | None = None
        self._last_sample_at: float | None = None
        self._last_tick_at: float | None = None

    @property
    def has_measurement(self) -> bool:
        """Prevent physical PWM writes until at least one valid lux sample."""
        return self._last_sample_at is not None

    def update(
        self,
        *,
        ambient_lux: float | None,
        sensor_ok: bool,
        sample: int,
        now: float,
    ) -> float:
        elapsed = 0.0 if self._last_tick_at is None else max(0.0, min(0.5, now - self._last_tick_at))
        self._last_tick_at = now

        if (not sensor_ok or ambient_lux is None or
                not math.isfinite(ambient_lux) or ambient_lux < 0):
            return self.current_percent

        if sample != self._last_sample:
            self._last_sample = sample
            self._last_sample_at = now
        elif self._last_sample_at is None or now - self._last_sample_at > self.SENSOR_TIMEOUT_S:
            return self.current_percent

        target = self.policy.resolve(ambient_lux).ui_percent
        difference = target - self.current_percent
        rate = self.RISE_PERCENT_PER_S if difference > 0 else self.FALL_PERCENT_PER_S
        movement = min(abs(difference), rate * elapsed)
        self.current_percent += math.copysign(movement, difference) if difference else 0.0
        return self.current_percent
