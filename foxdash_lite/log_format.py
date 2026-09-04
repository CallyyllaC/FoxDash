from __future__ import annotations

"""Compact, portable serialisation used by every FoxDash text log."""

import csv
import math
from typing import Any, Iterable, Mapping, TextIO


# These limits are deliberately decimal-place limits, not ``g`` formatting.
# They retain more precision than the dashboard presents while avoiding binary
# float tails and scientific notation in ordinary vehicle measurements.
FIELD_DECIMAL_PLACES: dict[str, int] = {
    "response_time_ms": 3,
    "elapsed_ms": 3,
    "sampleRateHz": 3,
    "lastUpdateAge_s": 3,
    "relativeAccel_mps2": 4,
    "relativeAccelSessionMin_mps2": 4,
    "relativeAccelSessionMax_mps2": 4,
    "relativeAccel_g": 5,
    "batteryV": 3,
    "airCPress_bar": 3,
    "airCPressSessionMin_bar": 3,
    "airCPressSessionMax_bar": 3,
    "ambient_lux_raw": 3,
    "ambient_lux_filtered": 3,
    "ambientLuxRaw": 3,
    "ambientLuxFiltered": 3,
}


# Stable categorical values are stored as small integers. Readers accept both
# these codes and historical text values; the mapping is also copied into each
# archive manifest for tools that do not import FoxDash.
STATUS_ENUMS: dict[str, dict[str, int]] = {
    "gear": {"--": -1, "N": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "R": 9, "?": 99},
    "dpfStatus": {"UNKNOWN": 0, "COLD": 1, "STABLE": 2, "CLIMBING": 3, "HOT": 4, "BURNING": 5, "PRESSURE": 6},
    "drivingState": {
        "unknown": 0, "idle": 1, "neutral": 2, "neutral-roll": 3, "braking": 4,
        "clutch": 5, "coasting": 6, "engine-braking": 7, "cruise": 8,
        "accelerating": 9, "decelerating": 10, "high-demand": 11,
        "low-rpm-demand": 12, "lugging": 13, "reversing": 14,
        "reverse-load": 15, "reverse-lugging": 16,
    },
    "thermalState": {"unknown": 0, "cold": 1, "warming": 2, "normal": 3, "heat-soak": 4, "hot": 5},
    "relativeAccelState": {"unknown": 0, "steady": 1, "gaining": 2, "pulling": 3, "easing": 4, "braking/down": 5},
    "moodState": {
        "unknown": 0, "happy": 1, "smug": 2, "fine": 3, "content": 4,
        "grumbling": 5, "strained": 6, "sulking": 7, "upset": 8,
        "lugging": 9, "hot": 10, "DPF pressure": 11, "reconnecting": 12,
        "no OBD": 13, "low volts": 14,
    },
    "ambientLightState": {"unavailable": 0, "starting": 1, "measuring": 2, "error": 3},
    "light_state": {"unavailable": 0, "starting": 1, "measuring": 2, "error": 3},
    "confidence": {"unknown": 0, "candidate": 1, "provisional": 2, "confirmed_identity": 3, "confirmed": 4},
    "classification": {"empty": 0, "static": 1, "rare_change": 2, "moving": 3},
    "known": {"no": 0, "yes": 1},
}


def status_schema() -> dict[str, dict[str, str]]:
    return {
        field: {str(code): label for label, code in values.items()}
        for field, values in STATUS_ENUMS.items()
    }


def decode_status(field_name: str, value: Any) -> Any:
    mapping = STATUS_ENUMS.get(field_name)
    if mapping is None or value in {None, ""}:
        return value
    code = str(value).strip()
    for label, number in mapping.items():
        if code == str(number):
            return label
    return value


def expand_compact_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {name: decode_status(name, value) for name, value in row.items()}


def decimal_places_for(field_name: str) -> int:
    if field_name in FIELD_DECIMAL_PLACES:
        return FIELD_DECIMAL_PLACES[field_name]
    lowered = field_name.lower()
    if lowered in {"old", "new", "delta", "final_value"}:
        return 6
    if lowered.endswith(("score", "confidence")):
        return 3
    if lowered.endswith(("temp", "temperature")):
        return 3
    if "pressure" in lowered or lowered.endswith(("_bar", "proxy")):
        return 3
    return 6


def compact_value(value: Any, *, field_name: str = "") -> Any:
    """Return a CSV-safe scalar without redundant decimal text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, float):
        if isinstance(value, str) and field_name in STATUS_ENUMS:
            return STATUS_ENUMS[field_name].get(value, value)
        return value
    if not math.isfinite(value):
        return "" if math.isnan(value) else str(value)
    if value == 0:
        return "0"
    places = decimal_places_for(field_name)
    rendered = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", "+0"} else rendered


def compact_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {name: compact_value(value, field_name=name) for name, value in row.items()}


class CompactDictWriter:
    """``csv.DictWriter`` wrapper with UTF-8-friendly Unix CSV output."""

    def __init__(
        self,
        handle: TextIO,
        *,
        fieldnames: Iterable[str],
        extrasaction: str = "raise",
    ) -> None:
        self._writer = csv.DictWriter(
            handle,
            fieldnames=list(fieldnames),
            extrasaction=extrasaction,
            lineterminator="\n",
        )

    def writeheader(self) -> Any:
        return self._writer.writeheader()

    def writerow(self, row: Mapping[str, Any]) -> Any:
        return self._writer.writerow(compact_row(row))

    def writerows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        for row in rows:
            self.writerow(row)
