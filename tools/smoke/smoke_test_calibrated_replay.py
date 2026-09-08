from __future__ import annotations

import collections
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from foxdash_lite.regime_model import RegimeModel
from foxdash_lite.source_adapters import ReplaySource
from foxdash_lite.telemetry_calibration import CalibratedTelemetryEngine
from foxdash_lite.telemetry_engine import canonical_from_decoded_row, canonical_from_ui_display_row, parse_timestamp_seconds

SOURCE = REPO_ROOT / "sample_data" / "replay_sample.csv"
MODEL = REPO_ROOT / "models" / "regime_model_2026-09-observe.json"


def main() -> int:
    source = ReplaySource(SOURCE, random_start=False, speed=1000.0)
    engine = CalibratedTelemetryEngine(session_id="sim", boot_id="sim", session_started_at="sim")
    model = RegimeModel.load(MODEL)

    valid = 0
    classified = 0
    regimes: collections.Counter[str] = collections.Counter()
    efficiencies: list[float] = []
    moods: list[float] = []
    guidance: list[float] = []
    demands: list[float] = []

    for index, row in enumerate(source.rows):
        canonical = canonical_from_decoded_row(row) if source.kind == "decoded_core" else canonical_from_ui_display_row(row)
        timestamp = row.get("timestamp")
        snapshot = engine.process(
            canonical,
            timestamp=str(timestamp or ""),
            sample=index + 1,
            obd_connection="simulator/replay",
            adapter_state="sample_data",
            protocol="CSV replay -> calibrated engine",
            source_time_s=parse_timestamp_seconds(timestamp, float(index)),
        )
        if not snapshot.telemetryValid:
            continue
        valid += 1
        assert snapshot.efficiencyScore is not None and math.isfinite(snapshot.efficiencyScore)
        assert snapshot.moodScore is not None and math.isfinite(snapshot.moodScore)
        assert snapshot.engineDemandProxy is not None and math.isfinite(snapshot.engineDemandProxy)
        assert snapshot.guidanceCorrection is not None and math.isfinite(snapshot.guidanceCorrection)
        assert -1.0 <= snapshot.guidanceCorrection <= 1.0
        assert 0.0 <= snapshot.efficiencyScore <= 100.0
        assert 0.0 <= snapshot.moodScore <= 100.0
        assert 0.0 <= snapshot.engineDemandProxy <= 100.0

        efficiencies.append(snapshot.efficiencyScore)
        moods.append(snapshot.moodScore)
        guidance.append(snapshot.guidanceCorrection)
        demands.append(snapshot.engineDemandProxy)

        result = model.classify(snapshot)
        if result is not None:
            classified += 1
            regimes[result.regime_id] += 1
            assert math.isfinite(result.distance) and result.distance >= 0.0
            assert 0.0 <= result.confidence <= 1.0

    assert valid > 100, f"Too few valid simulator samples: {valid}"
    assert classified > 100, f"Too few regime-classified simulator samples: {classified}"
    assert regimes, "No operating regimes observed"
    assert max(efficiencies) > min(efficiencies), "Efficiency is suspiciously constant"
    assert max(moods) > min(moods), "Mood is suspiciously constant"
    assert max(demands) > min(demands), "Engine demand is suspiciously constant"

    print(
        "OK: calibrated replay simulator processed "
        f"{len(source.rows)} rows, {valid} valid, {classified} regime-classified; "
        f"eff={min(efficiencies):.1f}-{max(efficiencies):.1f}, "
        f"mood={min(moods):.1f}-{max(moods):.1f}, "
        f"demand={min(demands):.1f}-{max(demands):.1f}, "
        f"regimes={dict(regimes)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
