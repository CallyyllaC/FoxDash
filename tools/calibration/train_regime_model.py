from __future__ import annotations

"""Train/export a tiny FoxDash operating-regime model from enriched UI logs.

This script is deliberately offline-only.  It uses pandas/scikit-learn here so
runtime FoxDash does not need either package.  The output JSON is consumed by
``foxdash_lite.regime_model`` and contains only normalisation statistics and
centroids, never raw journey data.
"""

import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from foxdash_lite.regime_model import MODEL_SCHEMA_VERSION

DEFAULT_FEATURES = (
    "rpm",
    "speed_mph",
    "absLoadProxy",
    "relativeAccel_mps2",
    "boostTargetProxy",
    "railTargetProxy",
    "airFlowSetting",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="Enriched/UI CSV files produced by FoxDash replay or live logging")
    parser.add_argument("--output", type=Path, required=True, help="Output regime-model JSON")
    parser.add_argument("--clusters", type=int, default=6, help="Number of k-means regimes (default: 6)")
    parser.add_argument("--seed", type=int, default=807, help="Deterministic random seed")
    parser.add_argument("--min-engine-temp", type=float, default=70.0, help="Warm-baseline filter when engineTempProxy exists")
    parser.add_argument("--min-speed", type=float, default=1.0, help="Minimum speed in mph")
    parser.add_argument("--max-per-session", type=int, default=4000, help="Cap samples per journey/session before fitting")
    parser.add_argument("--features", nargs="+", default=list(DEFAULT_FEATURES), help="Continuous model features")
    parser.add_argument("--note", default="Offline FoxDash regime training export", help="Training note stored in the model")
    return parser.parse_args()


def _load_frame(paths: list[Path], features: list[str], min_temp: float, min_speed: float):
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - friendly CLI path
        raise SystemExit("Offline calibration dependencies missing. Run: pip install -r tools/calibration/requirements.txt") from exc

    frames = []
    for path in paths:
        if not path.exists():
            raise SystemExit(f"Input does not exist: {path}")
        frame = pd.read_csv(path, low_memory=False)
        missing = [name for name in features if name not in frame.columns]
        if missing:
            raise SystemExit(f"{path}: missing required feature columns: {', '.join(missing)}")

        if "telemetryValid" in frame.columns:
            valid = frame["telemetryValid"].astype(str).str.lower().isin({"true", "1", "1.0"})
            frame = frame.loc[valid]
        if "engineTempProxy" in frame.columns:
            frame = frame.loc[pd.to_numeric(frame["engineTempProxy"], errors="coerce") >= min_temp]
        frame = frame.loc[pd.to_numeric(frame["speed_mph"], errors="coerce") >= min_speed]

        if "gear" in frame.columns:
            gear = frame["gear"].astype(str).str.strip().str.upper()
            engaged = gear.eq("R") | gear.str.fullmatch(r"[1-8]")
            frame = frame.loc[engaged]

        if "dpfRegenerationActive" in frame.columns:
            regen = frame["dpfRegenerationActive"].astype(str).str.lower().isin({"true", "1", "1.0"})
            frame = frame.loc[~regen]
        if "dpfStatus" in frame.columns:
            frame = frame.loc[~frame["dpfStatus"].astype(str).str.upper().eq("PRESSURE")]

        session_col = next((name for name in ("sessionId", "session_id") if name in frame.columns), None)
        frame = frame.copy()
        frame["_session"] = frame[session_col].astype(str) if session_col else path.stem
        frame["_source"] = path.name
        frames.append(frame[[*features, "_session", "_source"]])

    if not frames:
        raise SystemExit("No input frames loaded")
    data = pd.concat(frames, ignore_index=True)
    for name in features:
        data[name] = pd.to_numeric(data[name], errors="coerce")
    data = data.dropna(subset=features)
    finite_mask = data[features].map(lambda value: math.isfinite(float(value))).all(axis=1)
    return data.loc[finite_mask].reset_index(drop=True)


def main() -> int:
    args = parse_args()
    if args.clusters < 2:
        raise SystemExit("--clusters must be at least 2")
    if args.max_per_session < 10:
        raise SystemExit("--max-per-session must be at least 10")

    try:
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score
        from sklearn.preprocessing import RobustScaler
    except ImportError as exc:  # pragma: no cover - friendly CLI path
        raise SystemExit("Offline calibration dependencies missing. Run: pip install -r tools/calibration/requirements.txt") from exc

    features = list(args.features)
    data = _load_frame(args.inputs, features, args.min_engine_temp, args.min_speed)
    if len(data) < args.clusters * 20:
        raise SystemExit(f"Only {len(data)} usable rows; insufficient for {args.clusters} regimes")

    # Every journey gets a bounded number of votes.  Long motorway slogs still
    # provide tight variance, but they do not democratically annex the model.
    balanced = (
        data.groupby("_session", group_keys=False, sort=False)
        .apply(lambda group: group.sample(n=min(len(group), args.max_per_session), random_state=args.seed))
        .reset_index(drop=True)
    )

    matrix = balanced[features].to_numpy(dtype=float)
    scaler = RobustScaler(quantile_range=(10.0, 90.0))
    scaled = scaler.fit_transform(matrix)
    # RobustScaler may produce a zero scale for an accidentally constant field.
    if np.any(~np.isfinite(scaler.scale_)) or np.any(scaler.scale_ <= 0.0):
        raise SystemExit("At least one feature has zero/non-finite robust scale; remove or replace that feature")

    model = KMeans(n_clusters=args.clusters, n_init=20, random_state=args.seed)
    labels = model.fit_predict(scaled)
    raw_centroids = scaler.inverse_transform(model.cluster_centers_)

    silhouette_n = min(len(scaled), 20000)
    if silhouette_n >= args.clusters * 3:
        rng = np.random.default_rng(args.seed)
        indices = rng.choice(len(scaled), size=silhouette_n, replace=False) if silhouette_n < len(scaled) else np.arange(len(scaled))
        silhouette = float(silhouette_score(scaled[indices], labels[indices]))
    else:
        silhouette = float("nan")

    counts = np.bincount(labels, minlength=args.clusters)
    regimes = []
    for index in range(args.clusters):
        regimes.append({
            "id": f"regime-{index}",
            "label": f"regime {index}",
            "centroid": [round(float(value), 8) for value in raw_centroids[index]],
            "sample_count": int(counts[index]),
        })

    payload = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "model_version": args.output.stem,
        "observation_only": True,
        "training_note": (
            f"{args.note}; usable_rows={len(data)}; balanced_rows={len(balanced)}; "
            f"sessions={balanced['_session'].nunique()}; silhouette={silhouette:.4f}"
        ),
        "features": features,
        "normalisation": {
            "centre": [round(float(value), 8) for value in scaler.center_],
            "scale": [round(float(value), 8) for value in scaler.scale_],
        },
        "regimes": regimes,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {args.output}: {len(data)} usable rows, {len(balanced)} balanced rows, "
        f"{balanced['_session'].nunique()} sessions, K={args.clusters}, silhouette={silhouette:.4f}"
    )
    print("Regime labels are intentionally generic; interpret/rename them after reviewing centroids and journey coverage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
