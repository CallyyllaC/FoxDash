from __future__ import annotations

import math
import unittest

from foxdash_lite.brightness_policy import AmbientPaletteController, BrightnessPolicy


class BrightnessPolicyTests(unittest.TestCase):
    def test_missing_and_invalid_readings_use_legacy_fallback(self) -> None:
        policy = BrightnessPolicy()
        for value in (None, -1.0, float("nan"), float("inf")):
            with self.subTest(value=value):
                levels = policy.resolve(value)
                self.assertEqual(levels.palette_mode, "fallback")
                self.assertEqual(levels.ui_percent, 80.0)
                self.assertEqual(levels.led_percent, 35.0)

    def test_night_through_day_is_continuous_bounded_and_monotonic(self) -> None:
        policy = BrightnessPolicy()
        values = [policy.resolve(lux) for lux in (0, 1, 7.5, 80, 400, 1000, 5632.5)]
        self.assertEqual(values[0].ui_percent, 25.0)
        self.assertEqual(values[-1].ui_percent, 100.0)
        self.assertEqual(values[-2].ui_percent, values[-1].ui_percent)
        self.assertTrue(all(25 <= v.ui_percent <= 100 for v in values))
        self.assertTrue(all(8 <= v.led_percent <= 65 for v in values))
        self.assertTrue(all(a.ui_percent <= b.ui_percent for a, b in zip(values, values[1:])))
        self.assertGreater(policy.resolve(400).ui_percent, policy.resolve(7.5).ui_percent)
        self.assertTrue(math.isfinite(policy.resolve(7.5).ui_percent))


class AmbientPaletteControllerTests(unittest.TestCase):
    def test_darkness_dims_gradually_without_touching_hardware(self) -> None:
        control = AmbientPaletteController(initial_percent=100.0)
        self.assertEqual(control.update(ambient_lux=0.0, sensor_ok=True, sample=1, now=0.0), 100.0)
        self.assertAlmostEqual(control.update(ambient_lux=0.0, sensor_ok=True, sample=2, now=1.0), 96.5)
        self.assertAlmostEqual(control.update(ambient_lux=0.0, sensor_ok=True, sample=3, now=2.0), 93.0)

    def test_brightening_is_limited_and_cannot_exceed_full_palette(self) -> None:
        control = AmbientPaletteController(initial_percent=25.0)
        control.update(ambient_lux=0.0, sensor_ok=True, sample=1, now=0.0)
        self.assertAlmostEqual(control.update(ambient_lux=5632.5, sensor_ok=True, sample=2, now=1.0), 31.0)
        self.assertLessEqual(control.update(ambient_lux=1e9, sensor_ok=True, sample=3, now=2.0), 100.0)

    def test_bad_sensor_or_stale_sample_holds_last_palette(self) -> None:
        control = AmbientPaletteController(initial_percent=60.0)
        control.update(ambient_lux=7.5, sensor_ok=True, sample=1, now=0.0)
        result = control.update(ambient_lux=7.5, sensor_ok=True, sample=2, now=1.0)
        self.assertEqual(control.update(ambient_lux=None, sensor_ok=False, sample=2, now=2.0), result)
        self.assertEqual(control.update(ambient_lux=float("nan"), sensor_ok=True, sample=2, now=3.0), result)
        self.assertEqual(control.update(ambient_lux=7.5, sensor_ok=True, sample=2, now=6.0), result)
        next_value = control.update(ambient_lux=7.5, sensor_ok=True, sample=3, now=6.1)
        self.assertLess(result - next_value, 1.0)

    def test_empty_sensor_does_not_dim_initial_palette(self) -> None:
        control = AmbientPaletteController(initial_percent=100.0)
        self.assertEqual(control.update(ambient_lux=None, sensor_ok=False, sample=0, now=100), 100.0)


if __name__ == "__main__":
    unittest.main()
