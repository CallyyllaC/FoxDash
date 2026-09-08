from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from foxdash_lite.led_app import LedFrameMapper
from foxdash_lite.runtime_types import DashboardState, EnvironmentSnapshot
from foxdash_lite.state_store import waiting_snapshot
from foxdash_lite.telemetry import TelemetrySnapshot


def state_for(snapshot: TelemetrySnapshot, sequence: int = 1) -> DashboardState:
    return DashboardState(snapshot, EnvironmentSnapshot(), sequence=sequence, source_name="smoke")


def lit_count(frame) -> int:
    return sum(pixel != (0, 0, 0, 0) for pixel in frame)


def main() -> int:
    mapper = LedFrameMapper()

    waiting = mapper.render(state_for(waiting_snapshot(), sequence=0), now=0.0)
    assert lit_count(waiting.frame) == 0 and waiting.mode == "off"

    relaxed = TelemetrySnapshot(
        timestamp="", sample=1, telemetryValid=True, efficiencyScore=100.0, moodScore=100.0,
        guidanceCorrection=0.0, guidanceReason="matched", rpm=2000.0, speed_mph=55.0, gear="6",
    )
    relaxed_render = mapper.render(state_for(relaxed), now=1.0)
    assert relaxed_render.mode == "normal"
    assert 11.8 <= relaxed_render.band_width <= 12.1
    assert relaxed_render.guidance_position is not None and 9.0 <= relaxed_render.guidance_position <= 14.0

    # Low mood narrows the band; positive upstream correction moves it toward
    # the "more engine speed" end. No raw RPM/pedal inference occurs here.
    picky = TelemetrySnapshot(
        timestamp="", sample=2, telemetryValid=True, efficiencyScore=55.0, moodScore=20.0,
        guidanceCorrection=0.85, guidanceReason="low_rpm_high_demand", rpm=1250.0,
        speed_mph=48.0, gear="5",
    )
    picky_render = mapper.render(state_for(picky), now=1.0)
    assert picky_render.band_width < relaxed_render.band_width
    assert picky_render.guidance_position is not None and picky_render.guidance_position > relaxed_render.guidance_position

    # Confirmed DPF regeneration owns colour only. Mood width and guidance
    # position remain useful, and the band stays localised rather than turning
    # the entire strip into an orange warning lamp.
    regen_base = TelemetrySnapshot(
        timestamp="", sample=3, telemetryValid=True, efficiencyScore=70.0, moodScore=79.0,
        guidanceCorrection=-0.30, dpfStatus="STABLE", dpfRegenerationActive=False,
        rpm=2200.0, speed_mph=60.0, gear="6",
    )
    normal_regen_geometry = mapper.render(state_for(regen_base), now=1.0)
    confirmed_regen = TelemetrySnapshot(
        **{
            **regen_base.__dict__,
            "dpfStatus": "REGEN",
            "dpfRegenerationActive": True,
        }
    )
    regen_render = mapper.render(state_for(confirmed_regen), now=1.0)
    assert regen_render.mode == "regen"
    assert 0 < lit_count(regen_render.frame) < mapper.led_count
    assert abs(regen_render.band_width - normal_regen_geometry.band_width) < 1e-9
    assert abs(regen_render.guidance_position - normal_regen_geometry.guidance_position) < 1e-9
    assert regen_render.frame != normal_regen_geometry.frame

    # Hot/falling-soot BURNING is descriptive evidence only, not confirmation.
    inferred_burning = TelemetrySnapshot(
        **{
            **regen_base.__dict__,
            "dpfStatus": "BURNING",
            "dpfRegenerationActive": False,
        }
    )
    burning_render = mapper.render(state_for(inferred_burning), now=1.0)
    assert burning_render.mode == "normal"

    print("OK: LED mapping policy passed: colour=efficiency, width=mood, marker=guidance, confirmed regen=colour only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
