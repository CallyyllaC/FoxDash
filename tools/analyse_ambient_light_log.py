from __future__ import annotations

"""Summarise BH1750 calibration logs after real FoxDash driving.

Usage:
    python analyse_ambient_light_log.py ~/CarOBD/logs
    python analyse_ambient_light_log.py ~/CarOBD/logs/psa_ambient_light_*.csv
    python analyse_ambient_light_log.py --filtered ~/CarOBD/logs
"""

import argparse
import csv
import glob
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from typing import Iterable

from foxdash_lite.log_archive import ArchiveTextReader, iter_archive_entries


def expand_inputs(values: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        candidate = Path(value).expanduser()
        if candidate.is_dir():
            paths.extend(sorted(candidate.glob("psa_ambient_light_*.csv")))
            paths.extend(
                archive for archive in sorted(candidate.glob("journey_*.zip"))
                if any(iter_archive_entries(archive, prefix="psa_ambient_light_"))
            )
            continue
        matches = [Path(match) for match in glob.glob(str(candidate))]
        paths.extend(matches if matches else [candidate])
    # Preserve deterministic order and tolerate a repeated shell expansion.
    return sorted({path.resolve() for path in paths if path.is_file()})


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        raise ValueError("cannot calculate a percentile of no values")
    p = max(0.0, min(100.0, float(p)))
    position = (len(sorted_values) - 1) * (p / 100.0)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = position - low
    return sorted_values[low] + ((sorted_values[high] - sorted_values[low]) * fraction)


def load_lux(paths: Iterable[Path], column: str) -> tuple[list[float], int]:
    values: list[float] = []
    skipped = 0
    for path in paths:
        with ArchiveTextReader(path, member_prefix="psa_ambient_light_") as handle:
            for row in csv.DictReader(handle):
                if str(row.get("sensor_ok", "")).strip().lower() not in {"true", "1", "yes"}:
                    skipped += 1
                    continue
                try:
                    value = float(row.get(column, ""))
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                if value < 0:
                    skipped += 1
                    continue
                values.append(value)
    return values, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarise FoxDash BH1750 ambient-light logs.")
    parser.add_argument("paths", nargs="+", help="ambient CSV file(s), shell glob(s), or a log directory")
    parser.add_argument("--filtered", action="store_true", help="analyse the EMA-filtered lux column instead of raw sensor readings")
    args = parser.parse_args()

    paths = expand_inputs(args.paths)
    if not paths:
        parser.error("No psa_ambient_light_*.csv files found")

    column = "ambient_lux_filtered" if args.filtered else "ambient_lux_raw"
    values, skipped = load_lux(paths, column)
    if not values:
        parser.error(f"No valid {column} samples found")
    values.sort()

    print(f"Files: {len(paths)} | valid samples: {len(values):,} | skipped rows: {skipped:,}")
    print(f"Column: {column}")
    for label, p in (("min", 0.0), ("P1", 1.0), ("P5", 5.0), ("P50", 50.0), ("P95", 95.0), ("P99", 99.0), ("max", 100.0)):
        print(f"{label:>4}: {percentile(values, p):9.2f} lux")
    print()
    print("Calibration candidates: P1 = dark endpoint, P99 = full-day endpoint.")
    print("Use raw values for the final percentile decision; filtered values are only a behaviour sanity check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
