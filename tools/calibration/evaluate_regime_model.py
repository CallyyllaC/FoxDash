from __future__ import annotations

"""Evaluate an observe-only regime model through the real FoxDash replay path."""

import argparse
import collections
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from foxdash_lite.regime_model import RegimeModel
from foxdash_lite.source_adapters import ReplaySource
from foxdash_lite.telemetry_calibration import CalibratedTelemetryEngine
from foxdash_lite.telemetry_engine import canonical_from_decoded_row, canonical_from_ui_display_row, parse_timestamp_seconds


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay", type=Path, help="FoxDash decoded/UI replay CSV or journey ZIP")
    parser.add_argument("--model", type=Path, default=REPO_ROOT / "models" / "regime_model_2026-09-observe.json")
    parser.add_argument("--limit", type=int, default=0, help="Maximum replay rows; 0 means all")
    return parser.parse_args()


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    position = max(0.0, min(len(ordered) - 1.0, fraction * (len(ordered) - 1.0)))
    lo = int(position)
    hi = min(len(ordered) - 1, lo + 1)
    portion = position - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * portion


def main() -> int:
    args = parse_args()
    model = RegimeModel.load(args.model)
    source = ReplaySource(args.replay, random_start=False, speed=1000.0)
    engine = CalibratedTelemetryEngine(session_id="regime-eval", boot_id="offline", session_started_at="offline")

    counts: collections.Counter[str] = collections.Counter()
    distances: list[float] = []
    confidences: list[float] = []
    incomplete = 0
    invalid = 0
    processed = 0

    for index, row in enumerate(source.rows):
        if args.limit and index >= args.limit:
            break
        canonical = canonical_from_decoded_row(row) if source.kind == "decoded_core" else canonical_from_ui_display_row(row)
        timestamp = row.get("timestamp")
        snapshot = engine.process(
            canonical,
            timestamp=str(timestamp or ""),
            sample=index + 1,
            obd_connection="replay-eval",
            adapter_state="offline",
            protocol="CSV replay -> calibrated engine -> regime observation",
            source_time_s=parse_timestamp_seconds(timestamp, float(index)),
        )
        processed += 1
        if not snapshot.telemetryValid:
            invalid += 1
            continue
        result = model.classify(snapshot)
        if result is None:
            incomplete += 1
            continue
        counts[result.regime_id] += 1
        distances.append(result.distance)
        confidences.append(result.confidence)

    classified = sum(counts.values())
    print(f"model={model.model_version} observation_only={model.observation_only}")
    print(f"processed={processed} valid_classified={classified} invalid={invalid} incomplete_features={incomplete}")
    if classified:
        for regime in model.regimes:
            count = counts[regime.regime_id]
            pct = 100.0 * count / classified
            print(f"  {regime.regime_id:18s} {count:7d} {pct:6.2f}%  {regime.label}")
        print(
            "distance "
            f"p50={statistics.median(distances):.3f} "
            f"p95={percentile(distances, 0.95):.3f} "
            f"p99={percentile(distances, 0.99):.3f}; "
            f"confidence median={statistics.median(confidences):.3f}"
        )
    return 0 if classified else 2


if __name__ == "__main__":
    raise SystemExit(main())
