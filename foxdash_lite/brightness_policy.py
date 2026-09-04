from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrightnessLevels:
    ui_percent: float
    led_percent: float
    palette_mode: str


class BrightnessPolicy:
    """Placeholder policy. I²C will provide lux later; UI/LED consume output."""

    def resolve(self, ambient_lux: float | None) -> BrightnessLevels:
        if ambient_lux is None:
            return BrightnessLevels(ui_percent=80.0, led_percent=35.0, palette_mode="fallback")
        if ambient_lux < 5:
            return BrightnessLevels(ui_percent=18.0, led_percent=8.0, palette_mode="night")
        if ambient_lux < 80:
            return BrightnessLevels(ui_percent=50.0, led_percent=24.0, palette_mode="dusk")
        return BrightnessLevels(ui_percent=100.0, led_percent=65.0, palette_mode="day")
