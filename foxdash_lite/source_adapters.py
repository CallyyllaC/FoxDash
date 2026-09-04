from __future__ import annotations

import csv
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .conversion import detect_csv_kind
from .log_archive import ArchiveTextReader
from .log_format import expand_compact_row
from .telemetry_engine import (
    canonical_from_decoded_row,
    canonical_from_ui_display_row,
    parse_int,
    parse_timestamp_seconds,
)


@dataclass(frozen=True)
class SourceFrame:
    canonical: dict[str, object]
    timestamp: str
    sample: int
    source_name: str
    adapter_state: str
    protocol: str
    poll_ok: bool = True
    poll_health: str = "100%"


class ReplaySource:
    """Timed CSV source that feeds the same engine path as live OBD."""

    def __init__(self, path: str | Path, *, random_start: bool = True, speed: float = 1.0) -> None:
        self.path = Path(path)
        self.kind = detect_csv_kind(self.path)
        member_prefix = "psa_decoded_core_" if self.kind == "decoded_core" else "psa_ui_display_values_"
        with ArchiveTextReader(self.path, member_prefix=member_prefix) as f:
            self.rows = [expand_compact_row(row) for row in csv.DictReader(f)]
        if not self.rows:
            raise ValueError(f"No replayable rows found in {self.path}")
        self.speed = max(0.05, float(speed))
        self.index = random.randrange(len(self.rows)) if random_start and len(self.rows) > 1 else 0

    def frames(self, stop_requested) -> Iterator[SourceFrame]:
        start_index = self.index
        wall_start = time.monotonic()
        log_start = self._time_for(self.index)
        while not stop_requested():
            row = self.rows[self.index]
            canonical = canonical_from_decoded_row(row) if self.kind == "decoded_core" else canonical_from_ui_display_row(row)
            yield SourceFrame(
                canonical=canonical,
                timestamp=str(row.get("timestamp", "")),
                sample=parse_int(row.get("sample"), self.index + 1),
                source_name="replay",
                adapter_state=f"log/{self.path.name}",
                protocol="CSV replay → engine",
            )
            next_index = (self.index + 1) % len(self.rows)
            if next_index == start_index:
                wall_start = time.monotonic()
                log_start = self._time_for(self.index)
            else:
                target = max(0.01, (self._time_for(next_index) - log_start) / self.speed)
                while not stop_requested() and time.monotonic() - wall_start < target:
                    time.sleep(0.005)
            self.index = next_index

    def _time_for(self, index: int) -> float:
        return parse_timestamp_seconds(self.rows[index].get("timestamp"), float(index))


class OfflineSweepSource:
    """Placeholder source for no-OBD UI bring-up. Not fake live telemetry."""

    def frames(self, stop_requested) -> Iterator[SourceFrame]:
        sample = 0
        while not stop_requested():
            sample += 1
            # Minimal canonical sweep keeps the full engine path exercised.
            phase = (sample % 240) / 240.0
            pedal = phase * 70.0
            canonical = {
                "rpm": 800 + pedal * 20,
                "speed_mph": phase * 65,
                "gear": "--",
                "accelPedal": pedal,
                "coolant": 70 + phase * 20,
                "fuelTemp": 25 + phase * 10,
                "airManifoldTemp": 18 + phase * 20,
                "externalTemp": 14.0,
                "atmospheric": 1000.0,
                "turboMeasured": 1000 + pedal * 12,
                "turboTarget": 1000 + pedal * 12,
                "fuelRailMeasured_bar": 250 + pedal * 15,
                "fuelRailTarget_bar": 250 + pedal * 15,
                "airFlowSetting": 150 + pedal * 8,
                "airFlowMeasured": 150 + pedal * 8,
                "batteryV": 14.2,
                "fapSoot": 0.0,
                "fapTemp": 0.0,
                "fapDiffPressure": 0.0,
            }
            yield SourceFrame(canonical, "", sample, "sweep", "waiting/device", "offline sweep → engine", False, "--")
            time.sleep(0.10)
