from __future__ import annotations

"""Compatibility CSV helpers.

The real proxy/scoring brain lives in telemetry_engine. This file keeps the
old log-conversion command working without letting replay calculations fork
away from live runtime calculations.
"""

import csv
from dataclasses import asdict
from pathlib import Path

from .log_archive import ArchiveTextReader
from .log_format import CompactDictWriter, expand_compact_row
from .telemetry import DISPLAY_FIELD_NAMES, TelemetrySnapshot
from .telemetry_engine import (
    TelemetryEngine,
    TelemetryRollingState,
    canonical_from_decoded_row,
    canonical_from_ui_display_row,
    parse_float,
    parse_int,
    parse_str,
    parse_timestamp_seconds,
)

ConversionRollingState = TelemetryRollingState


def detect_csv_kind(path: str | Path) -> str:
    candidate_prefixes = ("psa_decoded_core_", "psa_ui_display_values_") if Path(path).suffix.lower() == ".zip" else ("",)
    for prefix in candidate_prefixes:
        try:
            with ArchiveTextReader(path, member_prefix=prefix) as f:
                names = set(next(csv.reader(f)))
        except (ValueError, StopIteration):
            continue
        if {"efficiencyScore", "moodScore", "boostProxy", "railProxy"}.issubset(names):
            return "ui_display"
        if {"rpm", "speed_mph", "turboMeasured", "fuelRailMeasured_bar"}.issubset(names):
            return "decoded_core"
    raise ValueError(f"Unrecognised FoxDash-compatible CSV format: {path}")


def load_snapshots_from_csv(path: str | Path, *, max_rows: int | None = None) -> list[TelemetrySnapshot]:
    kind = detect_csv_kind(path)
    engine = TelemetryEngine()
    snapshots: list[TelemetrySnapshot] = []
    member_prefix = "psa_decoded_core_" if kind == "decoded_core" else "psa_ui_display_values_"
    with ArchiveTextReader(path, member_prefix=member_prefix) as f:
        reader = csv.DictReader(f)
        for i, compact_row in enumerate(reader, 1):
            if max_rows is not None and i > max_rows:
                break
            row = expand_compact_row(compact_row)
            canonical = canonical_from_decoded_row(row) if kind == "decoded_core" else canonical_from_ui_display_row(row)
            timestamp = parse_str(row.get("timestamp"))
            snapshots.append(engine.process(
                canonical,
                timestamp=timestamp,
                sample=parse_int(row.get("sample"), i),
                obd_connection="replay",
                adapter_state=f"log/{Path(path).name}",
                protocol="CSV replay",
                source_time_s=parse_timestamp_seconds(timestamp, float(i)),
            ))
    return snapshots


def convert_decoded_csv_to_display_csv(input_path: str | Path, output_path: str | Path, *, max_rows: int | None = None) -> int:
    snapshots = load_snapshots_from_csv(input_path, max_rows=max_rows)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = CompactDictWriter(f, fieldnames=DISPLAY_FIELD_NAMES, extrasaction="ignore")
        writer.writeheader()
        for snap in snapshots:
            writer.writerow(asdict(snap))
    return len(snapshots)
