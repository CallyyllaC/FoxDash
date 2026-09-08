from __future__ import annotations

import unittest
from dataclasses import replace

from foxdash_lite.led_app import LedFrameMapper
from foxdash_lite.runtime_types import DashboardState, EnvironmentSnapshot
from foxdash_lite.telemetry import TelemetrySnapshot


class LedFrameMapperCalibrationTests(unittest.TestCase):
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
            efficiencyScore=80.0,
            moodScore=70.0,
            guidanceCorrection=0.25,
            dpfStatus="STABLE",
            dpfRegenerationActive=False,
        )
        return replace(base, **changes)

    def test_confirmed_regeneration_changes_colour_but_preserves_geometry(self) -> None:
        mapper = LedFrameMapper()
        normal = mapper.render(self._state(self._telemetry()), now=1.0)
        regen = mapper.render(
            self._state(self._telemetry(dpfStatus="REGEN", dpfRegenerationActive=True)),
            now=1.0,
        )

        self.assertEqual(normal.mode, "normal")
        self.assertEqual(regen.mode, "regen")
        self.assertAlmostEqual(regen.band_width, normal.band_width)
        self.assertAlmostEqual(regen.guidance_position, normal.guidance_position)
        self.assertNotEqual(regen.frame, normal.frame)

    def test_burning_without_confirmed_flag_does_not_trigger_regen_colour(self) -> None:
        mapper = LedFrameMapper()
        render = mapper.render(
            self._state(self._telemetry(dpfStatus="BURNING", dpfRegenerationActive=False)),
            now=1.0,
        )
        self.assertEqual(render.mode, "normal")

    def test_regen_band_remains_localised_not_whole_strip(self) -> None:
        mapper = LedFrameMapper()
        render = mapper.render(
            self._state(
                self._telemetry(
                    dpfStatus="REGEN",
                    dpfRegenerationActive=True,
                    moodScore=35.0,
                    guidanceCorrection=-0.5,
                )
            ),
            now=1.0,
        )
        lit_pixels = sum(pixel != (0, 0, 0, 0) for pixel in render.frame)
        self.assertLess(lit_pixels, mapper.led_count)
        self.assertGreater(lit_pixels, 0)


if __name__ == "__main__":
    unittest.main()
