from __future__ import annotations

import csv
import random
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from foxdash_lite.regime_model import RegimeModel

FEATURES = (
    "rpm",
    "speed_mph",
    "absLoadProxy",
    "relativeAccel_mps2",
    "boostTargetProxy",
    "railTargetProxy",
    "airFlowSetting",
)

CENTRES = (
    (1400, 30, 15, -0.10, 50, 500, 270),
    (1600, 50, 30, 0.00, 250, 850, 400),
    (2100, 65, 70, 0.35, 1200, 1250, 800),
)


def main() -> int:
    rng = random.Random(807)
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        source = temp_path / "synthetic_enriched.csv"
        output = temp_path / "trained.json"
        fields = [
            "sessionId", "telemetryValid", "engineTempProxy", "gear",
            "dpfRegenerationActive", "dpfStatus", *FEATURES,
        ]
        with source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for cluster_index, centre in enumerate(CENTRES):
                for row_index in range(180):
                    writer.writerow({
                        "sessionId": f"S{cluster_index}-{row_index // 60}",
                        "telemetryValid": True,
                        "engineTempProxy": 88.0,
                        "gear": 4 + min(cluster_index, 2),
                        "dpfRegenerationActive": False,
                        "dpfStatus": "STABLE",
                        "rpm": centre[0] + rng.gauss(0, 35),
                        "speed_mph": centre[1] + rng.gauss(0, 2),
                        "absLoadProxy": centre[2] + rng.gauss(0, 2),
                        "relativeAccel_mps2": centre[3] + rng.gauss(0, 0.03),
                        "boostTargetProxy": centre[4] + rng.gauss(0, 35),
                        "railTargetProxy": centre[5] + rng.gauss(0, 35),
                        "airFlowSetting": centre[6] + rng.gauss(0, 25),
                    })

        subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "calibration" / "train_regime_model.py"),
                str(source),
                "--output", str(output),
                "--clusters", "3",
                "--max-per-session", "1000",
            ],
            cwd=REPO_ROOT,
            check=True,
        )
        model = RegimeModel.load(output)
        assert model.observation_only
        assert len(model.regimes) == 3
        assert len(model.features) == len(FEATURES)
        for centre in CENTRES:
            result = model.classify(dict(zip(FEATURES, centre)))
            assert result is not None
            assert result.distance < 1.0

    print("OK: offline regime trainer exported a loadable three-regime model from synthetic enriched journeys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
