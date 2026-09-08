from __future__ import annotations

import unittest
from dataclasses import replace

from foxdash_lite.led_app import (
    EFFICIENCY_VISUAL_CURVE,
    LedFrameMapper,
    _curve_value,
)
from foxdash_lite.runtime_types import DashboardState, EnvironmentSnapshot
from foxdash_lite.telemetry import TelemetrySnapshot


class LedScoreCalibrationTests(unittest.TestCase):
    @staticmethod
    def _state(telemetry: TelemetrySnapshot) -> DashboardState:
        return DashboardState(
            telemetry=telemetry,
            environment=EnvironmentSnapshot(),
            sequence=1,
            source_name="test",
        )

    @staticmethod
    def _telemetry(**changes) -> TelemetrySnapshot:
        base = TelemetrySnapshot(
            timestamp="test",
            sample=1,
            telemetryValid=True,
            efficiencyScore=88.0,
            moodScore=90.0,
            guidanceCorrection=0.0,
        )
        return replace(base, **changes)

    def test_mood_curve_makes_healthy_and_unhappy_widths_visibly_distinct(self) -> None:
        mapper = LedFrameMapper()
        narrow = mapper.render(self._state(self._telemetry(moodScore=60.0)), now=1.0)
        healthy = mapper.render(self._state(self._telemetry(moodScore=96.0)), now=1.0)

        self.assertLess(narrow.band_width, 4.5)
        self.assertGreater(healthy.band_width, 10.0)
        self.assertGreater(healthy.band_width - narrow.band_width, 6.0)

    def test_efficiency_transfer_uses_the_full_palette_range(self) -> None:
        self.assertAlmostEqual(_curve_value(68.0, EFFICIENCY_VISUAL_CURVE), 38.0)
        self.assertAlmostEqual(_curve_value(76.0, EFFICIENCY_VISUAL_CURVE), 55.0)
        self.assertAlmostEqual(_curve_value(92.0, EFFICIENCY_VISUAL_CURVE), 90.0)
        self.assertAlmostEqual(_curve_value(97.0, EFFICIENCY_VISUAL_CURVE), 100.0)

    def test_efficiency_colour_changes_materially_across_realistic_scores(self) -> None:
        mapper = LedFrameMapper()
        hard_work = mapper.render(self._state(self._telemetry(efficiencyScore=68.0)), now=1.0)
        efficient = mapper.render(self._state(self._telemetry(efficiencyScore=92.0)), now=1.0)
        self.assertNotEqual(hard_work.frame, efficient.frame)

    def test_regen_colour_override_keeps_calibrated_mood_geometry(self) -> None:
        mapper = LedFrameMapper()
        normal = mapper.render(
            self._state(self._telemetry(moodScore=82.0, guidanceCorrection=0.35)),
            now=1.0,
        )
        regen = mapper.render(
            self._state(
                self._telemetry(
                    moodScore=82.0,
                    guidanceCorrection=0.35,
                    dpfStatus="REGEN",
                    dpfRegenerationActive=True,
                )
            ),
            now=1.0,
        )
        self.assertAlmostEqual(regen.band_width, normal.band_width)
        self.assertAlmostEqual(regen.guidance_position, normal.guidance_position)
        self.assertNotEqual(regen.frame, normal.frame)


if __name__ == "__main__":
    unittest.main()
