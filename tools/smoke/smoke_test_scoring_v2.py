from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from foxdash_lite.led_app import LedFrameMapper
from foxdash_lite.runtime_types import DashboardState, EnvironmentSnapshot
from foxdash_lite.telemetry import TelemetrySnapshot
from foxdash_lite.telemetry_engine import TelemetryEngine


def state_for(snapshot: TelemetrySnapshot) -> DashboardState:
    return DashboardState(snapshot, EnvironmentSnapshot(), sequence=1, source_name="smoke")


def canonical(*, rpm: float | None = 2000.0, speed: float | None = 50.0, gear: str = "5", pedal: float | None = 20.0, ac: float | None = 6.0) -> dict[str, object]:
    # Enough actual/target values to exercise the real proxy path rather than
    # feeding scores directly. The numbers are synthetic only for policy tests.
    return {
        "rpm": rpm,
        "speed_mph": speed,
        "gear": gear,
        "accelPedal": pedal,
        "atmospheric": 1000.0,
        "turboMeasured": 1550.0,
        "turboTarget": 1560.0,
        "fuelRailMeasured_bar": 760.0,
        "fuelRailTarget_bar": 770.0,
        "injFlow": 36.0,
        "fuelFlowReg": 40.0,
        "airFlowSetting": 430.0,
        "airFlowMeasured": 435.0,
        "coolant": 88.0,
        "airManifoldTemp": 30.0,
        "externalTemp": 20.0,
        "batteryV": 14.2,
        "airCPress_bar": ac,
        "fapSoot": 4.0,
        "fapTemp": 210.0,
        "fapDiffPressure": 20.0,
    }


def main() -> int:
    engine = TelemetryEngine(session_id="smoke", boot_id="boot", session_started_at="now")

    # Incomplete core telemetry never emits plausible normal scores.
    invalid = engine.process(canonical(rpm=None), sample=1, source_time_s=0.0)
    assert not invalid.telemetryValid
    assert invalid.efficiencyScore is None and invalid.moodScore is None
    assert invalid.scoreConfidence == 0.0

    # Pedal-free low-RPM descent/coast is explicitly not lugging.
    coast = engine.process(canonical(rpm=1180, speed=38, gear="4", pedal=0, ac=4.5), sample=2, source_time_s=1.0)
    assert coast.telemetryValid
    assert coast.drivingState == "coasting"
    assert "lugging" not in coast.drivingState
    assert coast.guidanceReason == "coasting_ok"

    # Sustained reverse demand is treated as an engaged drivetrain state.
    reverse_first = engine.process(canonical(rpm=1050, speed=2.5, gear="R", pedal=42, ac=5.5), sample=3, source_time_s=2.0)
    reverse_lug = engine.process(canonical(rpm=1030, speed=2.6, gear="R", pedal=42, ac=7.8), sample=4, source_time_s=3.0)
    assert reverse_first.drivingState == "reverse-load"
    assert reverse_lug.drivingState == "reverse-lugging"
    assert reverse_lug.guidanceCorrection is not None and reverse_lug.guidanceCorrection > 0.9
    assert reverse_lug.airCPressSessionMin_bar == 4.5
    assert reverse_lug.airCPressSessionMax_bar == 7.8

    # Busy second-gear running is not allowed to masquerade as peak economy.
    high_second = engine.process(canonical(rpm=3600, speed=35, gear="2", pedal=48), sample=5, source_time_s=4.0)
    assert high_second.guidanceReason == "high_rpm_low_gear"
    assert high_second.guidanceCorrection is not None and high_second.guidanceCorrection < 0.0
    assert high_second.efficiencyScore is not None and high_second.efficiencyScore < 75.0

    mapper = LedFrameMapper()
    relaxed = TelemetrySnapshot(
        timestamp="", sample=1, telemetryValid=True, efficiencyScore=100.0, moodScore=100.0,
        guidanceCorrection=0.0, rpm=2000, speed_mph=55, gear="6",
    )
    narrow = TelemetrySnapshot(
        timestamp="", sample=2, telemetryValid=True, efficiencyScore=55.0, moodScore=20.0,
        guidanceCorrection=0.8, rpm=1250, speed_mph=48, gear="5",
    )
    relaxed_render = mapper.render(state_for(relaxed), now=1.0)
    narrow_render = mapper.render(state_for(narrow), now=1.0)
    assert 11.8 <= relaxed_render.band_width <= 12.1
    assert 2.9 <= narrow_render.band_width < relaxed_render.band_width
    assert relaxed_render.guidance_position is not None and 9.0 <= relaxed_render.guidance_position <= 14.0
    assert narrow_render.guidance_position is not None and narrow_render.guidance_position > relaxed_render.guidance_position

    print("OK: scoring v2 policy passed: validity, coast, reverse lugging, high-rpm penalty, session extrema, moving mood band.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
