from __future__ import annotations

import unittest

from foxdash_lite.i2c_controller import I2cController


class AmbientResponseTests(unittest.TestCase):
    def test_darkness_is_filtered_faster_than_brightening(self) -> None:
        dark = I2cController(lambda snapshot: None)
        bright = I2cController(lambda snapshot: None)
        self.assertEqual(dark._filtered(1000.0, 0.0), 1000.0)
        self.assertEqual(bright._filtered(0.0, 0.0), 0.0)
        dark_lux = dark._filtered(0.0, 1.0)
        bright_lux = bright._filtered(1000.0, 1.0)
        self.assertLess(dark_lux, 250.0)
        self.assertLess(bright_lux, 350.0)
        self.assertGreater(1000.0 - dark_lux, bright_lux)

    def test_raw_values_are_not_replaced_by_filtered_values(self) -> None:
        samples = []
        controller = I2cController(samples.append)
        controller._filtered(500.0, 0.0)
        smoothed = controller._filtered(0.0, 1.0)
        controller._publish_reading(0.0, smoothed)
        self.assertEqual(samples[-1].ambient_lux_raw, 0.0)
        self.assertAlmostEqual(samples[-1].ambient_lux_filtered, smoothed)

    def test_manual_calibration_cannot_be_slower_than_caller_tau(self) -> None:
        controller = I2cController(lambda snapshot: None, filter_tau_s=0.8)
        self.assertAlmostEqual(controller.darkening_tau_s, 0.6)


if __name__ == "__main__":
    unittest.main()
