from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BrightnessLevels:
    ui_percent: float
    led_percent: float
    palette_mode: str


class BrightnessPolicy:
    """Provisional bounded curve with explicit dark/day calibration anchors.

    Under a box the sensor can still report several lux, so tying the minimum
    output to the impossible goal of exactly zero lux makes the minimum
    unreachable. These conservative test thresholds must be tuned after
    mounting the BH1750 in the vehicle.
    """

    # Normal dashboard uses the whole calibrated 1..100% palette / PWM 6..56.
    UI_DARK_LUX = 20.0
    UI_DAY_LUX = 400.0
    UI_NIGHT_PERCENT = 1.0
    # LED curve is independent and unchanged by the dashboard endpoints.
    LED_DAYLIGHT_LUX = 1000.0
    LED_NIGHT_PERCENT = 8.0
    LED_DAY_PERCENT = 65.0

    def resolve(self, ambient_lux: float | None) -> BrightnessLevels:
        if ambient_lux is None or not math.isfinite(ambient_lux) or ambient_lux < 0:
            return BrightnessLevels(ui_percent=80.0, led_percent=35.0, palette_mode="fallback")

        # Clip first so a dim but nonzero BH1750 reading reaches *exactly*
        # the manual minimum (PWM 6); bright daylight reaches PWM 56.
        light = max(self.UI_DARK_LUX, min(ambient_lux, self.UI_DAY_LUX))
        low = math.log1p(self.UI_DARK_LUX)
        high = math.log1p(self.UI_DAY_LUX)
        ui_amount = (math.log1p(light) - low) / (high - low)
        ui = self.UI_NIGHT_PERCENT + (100.0 - self.UI_NIGHT_PERCENT) * ui_amount

        # LEDs retain their independent provisional scaling and brightness.
        led_amount = math.log1p(min(ambient_lux, self.LED_DAYLIGHT_LUX)) / math.log1p(self.LED_DAYLIGHT_LUX)
        led = self.LED_NIGHT_PERCENT + (self.LED_DAY_PERCENT - self.LED_NIGHT_PERCENT) * led_amount
        mode = "night" if ambient_lux < 10.0 else "dusk" if ambient_lux < 80.0 else "day"
        return BrightnessLevels(ui_percent=ui, led_percent=led, palette_mode=mode)


class AmbientPaletteController:
    """Asymmetric slew-limited palette and HyperPixel PWM factor.

    Dim quickly on sudden darkness such as tunnels; brighten more gently to
    avoid headlight-induced glare. Faulty/stale sensors hold the last level.
    """

    SENSOR_TIMEOUT_S = 4.0
    RISE_PERCENT_PER_S = 28.0
    FALL_PERCENT_PER_S = 55.0

    def __init__(self, *, initial_percent: float = 100.0) -> None:
        self.current_percent = max(1.0, min(100.0, float(initial_percent)))
        self.policy = BrightnessPolicy()
        self._last_sample: int | None = None
        self._last_sample_at: float | None = None
        self._last_tick_at: float | None = None
        self.target_percent: float | None = None

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
        self.target_percent = target
        difference = target - self.current_percent
        rate = self.RISE_PERCENT_PER_S if difference > 0 else self.FALL_PERCENT_PER_S
        movement = min(abs(difference), rate * elapsed)
        self.current_percent += math.copysign(movement, difference) if difference else 0.0
        return self.current_percent
