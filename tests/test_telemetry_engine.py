from __future__ import annotations

import unittest

from foxdash_lite.telemetry_engine import (
    KM_TO_MPS,
    TelemetryRollingState,
    derive_dpf_state,
    derive_driving_state,
    update_rolling_derived,
)


class TelemetryEngineCalibrationTests(unittest.TestCase):
    def test_acceleration_uses_short_rolling_regression(self) -> None:
        rolling = TelemetryRollingState()
        result = None
        for timestamp, speed in ((0.0, 0.0), (0.8, 1.0), (1.6, 2.0), (2.4, 3.0)):
            result = update_rolling_derived({"speed_mph": speed}, rolling, timestamp)

        self.assertIsNotNone(result)
        expected = 1.25 * KM_TO_MPS
        self.assertAlmostEqual(result["relativeAccel_mps2"], expected, places=5)

    def test_acceleration_window_resets_after_large_sample_gap(self) -> None:
        rolling = TelemetryRollingState()
        update_rolling_derived({"speed_mph": 20.0}, rolling, 0.0)
        update_rolling_derived({"speed_mph": 21.0}, rolling, 0.8)
        self.assertIsNotNone(rolling.relative_accel_mps2)

        result = update_rolling_derived({"speed_mph": 40.0}, rolling, 10.0)
        self.assertIsNone(result["relativeAccel_mps2"])

    def test_speed_limiter_high_pedal_cruise_is_not_high_demand(self) -> None:
        values = {
            "rpm": 2000.0,
            "speed_mph": 71.0,
            "pedalProxy": 90.0,
            "loadProxy": 51.0,
            "absLoadProxy": 50.0,
            "boostTargetProxy": 500.0,
            "relativeAccel_mps2": 0.0,
            "gear": "6",
            "CL": 0.0,
            "BR": 0.0,
        }
        self.assertEqual(derive_driving_state(values, TelemetryRollingState(), 0.0), "cruise")

    def test_real_high_demand_needs_engine_effort_not_pedal_alone(self) -> None:
        values = {
            "rpm": 1950.0,
            "speed_mph": 59.0,
            "pedalProxy": 85.0,
            "loadProxy": 63.0,
            "absLoadProxy": 73.0,
            "boostTargetProxy": 900.0,
            "relativeAccel_mps2": 0.20,
            "gear": "5",
            "CL": 0.0,
            "BR": 0.0,
        }
        self.assertEqual(derive_driving_state(values, TelemetryRollingState(), 0.0), "high-demand")

    def test_moderate_low_rpm_cruise_is_not_lugging(self) -> None:
        values = {
            "rpm": 1400.0,
            "speed_mph": 50.0,
            "pedalProxy": 35.0,
            "loadProxy": 33.0,
            "absLoadProxy": 40.0,
            "boostTargetProxy": 200.0,
            "relativeAccel_mps2": 0.0,
            "gear": "6",
            "CL": 0.0,
            "BR": 0.0,
        }
        self.assertEqual(derive_driving_state(values, TelemetryRollingState(), 0.0), "cruise")

    def test_loaded_low_rpm_condition_requires_persistence_before_lugging(self) -> None:
        values = {
            "rpm": 1400.0,
            "speed_mph": 50.0,
            "pedalProxy": 35.0,
            "loadProxy": 45.0,
            "absLoadProxy": 50.0,
            "boostTargetProxy": 500.0,
            "relativeAccel_mps2": 0.20,
            "gear": "6",
            "CL": 0.0,
            "BR": 0.0,
        }
        rolling = TelemetryRollingState()
        self.assertEqual(derive_driving_state(values, rolling, 0.0), "low-rpm-demand")
        self.assertEqual(derive_driving_state(values, rolling, 0.8), "low-rpm-demand")
        self.assertEqual(derive_driving_state(values, rolling, 1.1), "lugging")

    def test_confirmed_regeneration_uses_ecu_flag(self) -> None:
        canonical = {
            "rpm": 1900.0,
            "fapSoot": 4.0,
            "fapTemp": 450.0,
            "fapDiffPressure": 40.0,
            "activeRegeneration": 1.0,
        }
        status, _arrow = derive_dpf_state(canonical, TelemetryRollingState())
        self.assertEqual(status, "REGEN")

    def test_hot_falling_soot_remains_burning_not_confirmed_regen(self) -> None:
        rolling = TelemetryRollingState()
        rolling.soot_window.extend(((0.0, 5.0), (120.0, 4.0)))
        canonical = {
            "rpm": 1900.0,
            "fapSoot": 4.0,
            "fapTemp": 450.0,
            "fapDiffPressure": 40.0,
            "activeRegeneration": 0.0,
        }
        status, arrow = derive_dpf_state(canonical, rolling)
        self.assertEqual(status, "BURNING")
        self.assertEqual(arrow, "↓")


if __name__ == "__main__":
    unittest.main()
