from __future__ import annotations

"""Small runtime-free operating-regime classifier.

The model produced by the offline calibration tooling is intentionally tiny:
feature centres/scales plus a handful of raw-unit centroids.  Runtime code does
not depend on numpy, pandas or scikit-learn.  More importantly, the classifier
is observation-only for now.  It can describe where a sample sits relative to
historical driving regimes without changing deterministic state, Efficiency,
Mood or guidance.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

MODEL_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RegimeDefinition:
    regime_id: str
    label: str
    centroid: tuple[float, ...]
    sample_count: int = 0


@dataclass(frozen=True)
class RegimeClassification:
    regime_id: str
    label: str
    distance: float
    second_distance: float | None
    confidence: float
    complete: bool


@dataclass(frozen=True)
class RegimeModel:
    model_version: str
    features: tuple[str, ...]
    centres: tuple[float, ...]
    scales: tuple[float, ...]
    regimes: tuple[RegimeDefinition, ...]
    observation_only: bool = True
    training_note: str = ""

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RegimeModel":
        schema_version = int(payload.get("schema_version", 0))
        if schema_version != MODEL_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported regime-model schema {schema_version}; expected {MODEL_SCHEMA_VERSION}"
            )

        features_raw = payload.get("features")
        if not isinstance(features_raw, list) or not features_raw:
            raise ValueError("Regime model requires a non-empty features list")
        features = tuple(str(item) for item in features_raw)
        if len(set(features)) != len(features):
            raise ValueError("Regime model features must be unique")

        normalisation = payload.get("normalisation")
        if not isinstance(normalisation, Mapping):
            raise ValueError("Regime model requires normalisation metadata")

        centres = _float_vector(normalisation.get("centre"), len(features), "normalisation.centre")
        scales = _float_vector(normalisation.get("scale"), len(features), "normalisation.scale")
        if any(scale <= 0.0 for scale in scales):
            raise ValueError("Regime model normalisation scales must be positive")

        regimes_raw = payload.get("regimes")
        if not isinstance(regimes_raw, list) or not regimes_raw:
            raise ValueError("Regime model requires at least one regime")

        regimes: list[RegimeDefinition] = []
        seen_ids: set[str] = set()
        for item in regimes_raw:
            if not isinstance(item, Mapping):
                raise ValueError("Each regime must be an object")
            regime_id = str(item.get("id", "")).strip()
            if not regime_id:
                raise ValueError("Each regime requires an id")
            if regime_id in seen_ids:
                raise ValueError(f"Duplicate regime id: {regime_id}")
            seen_ids.add(regime_id)
            label = str(item.get("label", regime_id)).strip() or regime_id
            centroid = _float_vector(item.get("centroid"), len(features), f"regime {regime_id} centroid")
            sample_count = max(0, int(item.get("sample_count", 0) or 0))
            regimes.append(RegimeDefinition(regime_id, label, centroid, sample_count))

        return cls(
            model_version=str(payload.get("model_version", "unversioned")),
            features=features,
            centres=centres,
            scales=scales,
            regimes=tuple(regimes),
            observation_only=bool(payload.get("observation_only", True)),
            training_note=str(payload.get("training_note", "")),
        )

    @classmethod
    def load(cls, path: str | Path) -> "RegimeModel":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("Regime model root must be an object")
        return cls.from_dict(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MODEL_SCHEMA_VERSION,
            "model_version": self.model_version,
            "observation_only": self.observation_only,
            "training_note": self.training_note,
            "features": list(self.features),
            "normalisation": {
                "centre": list(self.centres),
                "scale": list(self.scales),
            },
            "regimes": [
                {
                    "id": regime.regime_id,
                    "label": regime.label,
                    "centroid": list(regime.centroid),
                    "sample_count": regime.sample_count,
                }
                for regime in self.regimes
            ],
        }

    def classify(self, values: Mapping[str, Any] | Any) -> RegimeClassification | None:
        vector: list[float] = []
        for name in self.features:
            raw = values.get(name) if isinstance(values, Mapping) else getattr(values, name, None)
            parsed = _finite_float(raw)
            if parsed is None:
                return None
            vector.append(parsed)

        normalised = tuple(
            (value - centre) / scale
            for value, centre, scale in zip(vector, self.centres, self.scales)
        )

        ranked: list[tuple[float, RegimeDefinition]] = []
        for regime in self.regimes:
            regime_norm = tuple(
                (value - centre) / scale
                for value, centre, scale in zip(regime.centroid, self.centres, self.scales)
            )
            # Root-mean-square standardised distance keeps the value comparable
            # when future model versions add or remove features.
            distance = math.sqrt(
                sum((value - expected) ** 2 for value, expected in zip(normalised, regime_norm))
                / len(self.features)
            )
            ranked.append((distance, regime))

        ranked.sort(key=lambda item: item[0])
        best_distance, best = ranked[0]
        second_distance = ranked[1][0] if len(ranked) > 1 else None

        # Confidence is diagnostic, not a probability.  It rewards proximity to
        # a known centroid and separation from the runner-up without pretending
        # k-means discovered a Bayesian truth about a Citroen.
        proximity = math.exp(-0.5 * best_distance * best_distance)
        if second_distance is None:
            separation = 1.0
        else:
            separation = max(0.0, min(1.0, (second_distance - best_distance) / max(second_distance, 1e-9)))
        confidence = max(0.0, min(1.0, proximity * (0.55 + 0.45 * separation)))

        return RegimeClassification(
            regime_id=best.regime_id,
            label=best.label,
            distance=best_distance,
            second_distance=second_distance,
            confidence=confidence,
            complete=True,
        )


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _float_vector(value: Any, expected_length: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be an array")
    if len(value) != expected_length:
        raise ValueError(f"{label} has {len(value)} values; expected {expected_length}")
    parsed: list[float] = []
    for item in value:
        number = _finite_float(item)
        if number is None:
            raise ValueError(f"{label} contains a non-finite value")
        parsed.append(number)
    return tuple(parsed)
