from __future__ import annotations

import unittest
from dataclasses import replace

from foxdash_lite.telemetry import TelemetrySnapshot
from foxdash_lite.telemetry_calibration import (
    CalibratedTelemetryEngine,
    derive_engine_demand,
    derive_guidance,
)


class TelemetryScoreCalibrationTests(unittest.TestCase):
    @staticmethod
    def _snapshot(**changes) -> TelemetrySnapshot:
        base = TelemetrySnapshot(
            timestamp="test",
            sample=1,
            telemetryValid=True,
            scoreConfidence=100.0,
            rpm=2000.0,
            speed_mph=70.0,
            gear="6",
            drivingState="cruise",
            pedalProxy=80.0,
            absLoadProxy=40.0,
            injFlow=18.0,
            fuelFlowReg=30.0,
            boostTargetProxy=450.0,
            railTargetProxy=800.0,
            boostErrorProxy=0.0,
            railErrorProxy=0.0,
            airFlowSetting=350.0,
            airFlowMeasured=350.0,
            airFlowError=0.0,
            egrTarget=20.0,
            egrActual=20.0,
            egrError=0.0,
            airMixerTarget=10.0,
            airMixerActual=10.0,
            airMixerError=0.0,
            thermalState="normal",
        )
        return replace(base, **changes)

    def test_engine_demand_is_independent_of_limiter_pedal_position(self) -> None:
        low_pedal = self._snapshot(pedalProxy=15.0)
        high_pedal = self._snapshot(pedalProxy=95.0)
        self.assertAlmostEqual(
            derive_engine_demand(low_pedal),
            derive_engine_demand(high_pedal),
            places=7,
        )

    def test_engine_demand_separates_motorway_cruise_from_hard_load(self) -> None:
        motorway = self._snapshot(
            pedalProxy=90.0,
            absLoadProxy=50.0,
            injFlow=20.0,
            fuelFlowReg=38.0,
            boostTargetProxy=500.0,
            railTargetProxy=850.0,
        )
        hard_pull = self._snapshot(
            speed_mph=58.0,
            gear="5",
            drivingState="high-demand",
            pedalProxy=90.0,
            absLoadProxy=78.0,
            injFlow=45.0,
            fuelFlowReg=75.0,
            boostTargetProxy=1200.0,
            railTargetProxy=1450.0,
        )
        motorway_demand = derive_engine_demand(motorway)
        hard_demand = derive_engine_demand(hard_pull)
        self.assertIsNotNone(motorway_demand)
        self.assertIsNotNone(hard_demand)
        self.assertLess(motorway_demand, 50.0)
        self.assertGreater(hard_demand, 65.0)
        self.assertGreater(hard_demand - motorway_demand, 20.0)

    def test_top_gear_cruise_guidance_does_not_invent_a_seventh_gear(self) -> None:
        snapshot = self._snapshot(rpm=2000.0, speed_mph=71.0, gear="6", drivingState="cruise")
        correction, reason = derive_guidance(snapshot, 39.0)
        self.assertIsNotNone(correction)
        self.assertLess(abs(correction), 0.10)
        self.assertEqual(reason, "top_gear_cruise")

    def test_acceleration_guidance_is_continuous_not_a_fixed_step(self) -> None:
        snapshot = self._snapshot(rpm=1700.0, speed_mph=45.0, gear="4", drivingState="accelerating")
        correction, reason = derive_guidance(snapshot, 45.0)
        self.assertIsNotNone(correction)
        self.assertGreater(correction, 0.0)
        self.assertLess(correction, 0.50)
        self.assertEqual(reason, "below_target_band")

    def test_healthy_warm_cruise_can_reach_happy(self) -> None:
        engine = CalibratedTelemetryEngine()
        canonical = {
            "rpm": 1750.0,
            "speed_mph": 55.0,
            "gear": "6",
            "CL": 0.0,
            "BR": 0.0,
            "accelPedal": 90.0,
            "batteryV": 14.2,
            "coolant": 85.0,
            "fuelTemp": 30.0,
            "fuelFlowReg": 25.0,
            "fuelRailTarget_bar": 700.0,
            "fuelRailMeasured_bar": 700.0,
            "injFlow": 12.0,
            "atmospheric": 1000.0,
            "turboTarget": 1250.0,
            "turboMeasured": 1250.0,
            "airFlowSetting": 300.0,
            "airFlowMeasured": 300.0,
            "airFlowSensorTemp": 20.0,
            "airManifoldTemp": 20.0,
            "externalTemp": 15.0,
            "egrTarget": 20.0,
            "egrRepeat": 20.0,
            "airMixerTarget": 10.0,
            "airMixer": 10.0,
            "fapSoot": 4.0,
            "fapTemp": 250.0,
            "fapDiffPressure": 20.0,
        }
        snapshot = engine.process(canonical, source_time_s=0.0)
        self.assertTrue(snapshot.telemetryValid)
        self.assertIsNotNone(snapshot.engineDemandProxy)
        self.assertLess(snapshot.engineDemandProxy, 35.0)
        self.assertIsNotNone(snapshot.moodScore)
        self.assertGreaterEqual(snapshot.moodScore, 94.0)
        self.assertEqual(snapshot.moodState, "happy")

    def test_mood_hysteresis_prevents_boundary_flicker(self) -> None:
        engine = CalibratedTelemetryEngine()
        snapshot = self._snapshot(thermalState="normal")
        self.assertEqual(engine._hysteretic_mood_state(95.0, snapshot, 0.0), "happy")
        # Falling just below the nominal happy threshold is deliberately sticky.
        self.assertEqual(engine._hysteretic_mood_state(93.0, snapshot, 0.5), "happy")
        # A real drop below the exit margin must also persist before changing.
        self.assertEqual(engine._hysteretic_mood_state(91.0, snapshot, 1.0), "happy")
        self.assertEqual(engine._hysteretic_mood_state(91.0, snapshot, 2.1), "smug")


if __name__ == "__main__":
    unittest.main()
