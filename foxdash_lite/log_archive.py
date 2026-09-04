from __future__ import annotations

"""Crash-safe completed-journey archival for FoxDash logs."""

import datetime as dt
import csv
import json
import os
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO

from . import __version__
from .log_format import status_schema
from .psa_protocol import change_field_schema


MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
_LOG_NAME = re.compile(r"^psa_.+_(?P<session>\d{8}_\d{6}(?:-[A-Za-z0-9-]+)?)\.(?:csv|txt|log|json)$")
_HEADER_PREFIXES: dict[str, tuple[str, ...]] = {
    "psa_raw_blocks_": ("timestamp", "sample", "phase"),
    "psa_connection_attempts_": ("timestamp", "attempt", "port"),
    "psa_decoded_core_": ("timestamp", "sample"),
    "psa_decode_changes_": ("timestamp", "sample"),
    "psa_unknown_timeseries_": ("timestamp", "sample"),
    "psa_unknown_report_": ("command", "offset"),
    "psa_ui_display_values_": ("timestamp", "sample"),
    "psa_ambient_light_": ("updated_at", "sample"),
    "psa_final_values_": ("field", "fap_label", "command"),
}


@dataclass
class CleanupSummary:
    completed_journeys_found: int = 0
    journeys_archived: int = 0
    journeys_already_archived: int = 0
    loose_files_compressed: int = 0
    zero_byte_files_deleted: int = 0
    header_only_files_deleted: int = 0
    raw_bytes_before_compression: int = 0
    final_archive_bytes: int = 0
    failures: list[str] = field(default_factory=list)
    retained_source_files: int = 0

    @property
    def compression_ratio(self) -> float:
        if not self.raw_bytes_before_compression:
            return 0.0
        return self.final_archive_bytes / self.raw_bytes_before_compression

    def to_log_line(self) -> str:
        payload = asdict(self)
        payload["compression_ratio"] = round(self.compression_ratio, 4)
        return "journey_cleanup " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class JourneyFiles:
    session_id: str
    boot_id: str
    started_at: str
    files: tuple[Path, ...]


def safe_session_id(session_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", session_id.strip())
    return cleaned.strip(".-") or "unknown-session"


def _session_from_name(path: Path) -> str | None:
    match = _LOG_NAME.match(path.name)
    return match.group("session") if match else None


def _read_session_identity(path: Path, fallback: str) -> tuple[str, str, str]:
    session_id, boot_id, started_at = fallback, "unavailable", ""
    if path.suffix.lower() != ".txt" or "psa_session_" not in path.name:
        return session_id, boot_id, started_at
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for line in handle:
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                value = value.strip()
                if key == "Session ID" and value:
                    session_id = safe_session_id(value)
                elif key == "Boot ID" and value:
                    boot_id = value
                elif line.startswith("FoxDash session started "):
                    started_at = line.removeprefix("FoxDash session started ").strip()
    except (OSError, UnicodeError):
        pass
    return session_id, boot_id, started_at


def discover_completed_journeys(
    log_dir: Path,
    active_session_id: str,
    *,
    additional_active_session_ids: Iterable[str] = (),
) -> list[JourneyFiles]:
    """Group by explicit filename session identity, never by mtime ordering."""
    active_ids = {safe_session_id(active_session_id), *(safe_session_id(item) for item in additional_active_session_ids)}
    archived_identity_by_file: dict[str, tuple[str, str, str]] = {}
    for archive_path in log_dir.glob("journey_*.zip"):
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
            identity = (
                safe_session_id(str(manifest["session_id"])),
                str(manifest.get("boot_id", "unavailable")),
                str(manifest.get("journey_start_time", "")),
            )
            for item in manifest.get("files", []):
                archived_identity_by_file[str(item["name"])] = identity
        except (OSError, KeyError, UnicodeError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
            continue
    grouped: dict[str, list[Path]] = {}
    for path in log_dir.iterdir():
        if not path.is_file() or path.name.endswith((".zip", ".zip.tmp")):
            continue
        session = _session_from_name(path)
        if session is not None and safe_session_id(session) not in active_ids:
            grouped.setdefault(session, []).append(path)

    journeys: list[JourneyFiles] = []
    for filename_session, files in grouped.items():
        identity_file = next((p for p in files if p.name.startswith("psa_session_") and p.suffix == ".txt"), None)
        archived_identity = next((archived_identity_by_file[p.name] for p in files if p.name in archived_identity_by_file), None)
        if identity_file:
            session_id, boot_id, started_at = _read_session_identity(identity_file, filename_session)
        elif archived_identity:
            session_id, boot_id, started_at = archived_identity
        else:
            session_id, boot_id, started_at = filename_session, "unavailable", ""
        # A session header is authoritative. Its ID is also checked against the
        # active identity, protecting a legacy timestamp group from clock jumps.
        if safe_session_id(session_id) in active_ids:
            continue
        journeys.append(JourneyFiles(safe_session_id(session_id), boot_id, started_at, tuple(sorted(files))))
    return sorted(journeys, key=lambda item: item.session_id)


def _is_header_only_csv(path: Path) -> bool:
    if path.suffix.lower() != ".csv":
        return False
    expected = next((columns for prefix, columns in _HEADER_PREFIXES.items() if path.name.startswith(prefix)), None)
    if expected is None:
        return False
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            first: str | None = None
            for line in handle:
                if not line.strip():
                    continue
                if first is not None:
                    return False
                first = line
        if first is None:
            return False
        columns = next(csv.reader([first]), [])
        return tuple(columns[:len(expected)]) == expected
    except (OSError, UnicodeError):
        return False


def _clean_empty_files(journey: JourneyFiles, summary: CleanupSummary) -> JourneyFiles:
    retained: list[Path] = []
    for path in journey.files:
        try:
            if path.stat().st_size == 0:
                path.unlink()
                summary.zero_byte_files_deleted += 1
            elif _is_header_only_csv(path):
                path.unlink()
                summary.header_only_files_deleted += 1
            else:
                retained.append(path)
        except OSError as exc:
            summary.failures.append(f"cleanup {path.name}: {exc}")
            retained.append(path)
    return JourneyFiles(journey.session_id, journey.boot_id, journey.started_at, tuple(retained))


def _iso_from_timestamp(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat(timespec="seconds")


def _manifest(journey: JourneyFiles, files: tuple[Path, ...]) -> dict[str, Any]:
    mtimes = [path.stat().st_mtime for path in files]
    return {
        "manifest_version": MANIFEST_VERSION,
        "session_id": journey.session_id,
        "boot_id": journey.boot_id,
        "journey_start_time": journey.started_at or (_iso_from_timestamp(min(mtimes)) if mtimes else ""),
        "journey_end_time": _iso_from_timestamp(max(mtimes)) if mtimes else "",
        "encoding": "utf-8",
        "newline": "\\n",
        "files": [{"name": path.name, "uncompressed_length": path.stat().st_size} for path in files],
        "archive_created_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(timespec="seconds"),
        "application_version": __version__,
        "status_enums": status_schema(),
        "change_fields": change_field_schema(),
    }


def verify_archive(path: Path, expected: dict[str, int] | None = None, *, session_id: str | None = None) -> dict[str, Any]:
    """Read every entry (triggering CRC checks) and validate the manifest."""
    with zipfile.ZipFile(path, "r") as archive:
        names = set(archive.namelist())
        if MANIFEST_NAME not in names:
            raise ValueError("archive has no manifest.json")
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        if session_id is not None and manifest.get("session_id") != session_id:
            raise ValueError("archive session ID does not match")
        manifest_entries = {item["name"]: int(item["uncompressed_length"]) for item in manifest.get("files", [])}
        wanted = manifest_entries if expected is None else expected
        if set(wanted) - names:
            raise ValueError(f"archive missing entries: {sorted(set(wanted) - names)}")
        if manifest_entries != wanted:
            raise ValueError("manifest file list or lengths do not match expected files")
        for name, length in wanted.items():
            info = archive.getinfo(name)
            if info.file_size != length:
                raise ValueError(f"archive length mismatch for {name}")
            with archive.open(info, "r") as entry:
                observed = 0
                while chunk := entry.read(1024 * 1024):
                    observed += len(chunk)
            if observed != length:
                raise ValueError(f"archive read length mismatch for {name}")
        bad_entry = archive.testzip()
        if bad_entry is not None:
            raise ValueError(f"archive CRC failure in {bad_entry}")
        return manifest


def _flush_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _remove_sources(files: tuple[Path, ...]) -> int:
    removed = 0
    for path in files:
        if path.exists():
            path.unlink()
            removed += 1
    return removed


def archive_journey(journey: JourneyFiles, summary: CleanupSummary) -> bool:
    files = tuple(path for path in journey.files if path.exists())
    if not files:
        return True
    archive_path = files[0].parent / f"journey_{journey.session_id}.zip"
    temporary_path = archive_path.with_suffix(".zip.tmp")
    expected = {path.name: path.stat().st_size for path in files}
    raw_bytes = sum(expected.values())

    if temporary_path.exists():
        temporary_path.unlink()

    if archive_path.exists():
        try:
            existing_manifest = verify_archive(archive_path, session_id=journey.session_id)
            archived_entries = {
                item["name"]: int(item["uncompressed_length"])
                for item in existing_manifest["files"]
            }
            if any(archived_entries.get(name) != length for name, length in expected.items()):
                raise ValueError("loose files do not match the completed archive")
            _remove_sources(files)
            summary.journeys_already_archived += 1
            return True
        except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
            # Keep every source while a replacement is made transactionally.
            pass

        # A readable manifest can prove that this is only a partial duplicate
        # source set. Never replace such an archive from fewer originals.
        try:
            with zipfile.ZipFile(archive_path, "r") as existing:
                declared = json.loads(existing.read(MANIFEST_NAME).decode("utf-8"))
            declared_names = {str(item["name"]) for item in declared.get("files", [])}
            if declared_names - set(expected):
                summary.failures.append(
                    f"archive {journey.session_id}: corrupt/incomplete final archive and "
                    f"{len(declared_names - set(expected))} declared source files are unavailable"
                )
                summary.retained_source_files += len(files)
                return False
        except (OSError, KeyError, UnicodeError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
            # With no usable manifest the loose group is the only recoverable
            # authoritative set, and a verified replacement is safe.
            pass

    manifest = _manifest(journey, files)
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        with zipfile.ZipFile(
            temporary_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=False,
        ) as archive:
            for path in files:
                archive.write(path, arcname=path.name)
            manifest_info = zipfile.ZipInfo(MANIFEST_NAME, date_time=dt.datetime.now().timetuple()[:6])
            manifest_info.compress_type = zipfile.ZIP_DEFLATED
            manifest_info.flag_bits |= 0x800
            archive.writestr(manifest_info, manifest_bytes, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        with temporary_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        verify_archive(temporary_path, expected, session_id=journey.session_id)
        os.replace(temporary_path, archive_path)
        _flush_directory(archive_path.parent)
        verify_archive(archive_path, expected, session_id=journey.session_id)
        _remove_sources(files)
        summary.journeys_archived += 1
        summary.loose_files_compressed += len(files)
        summary.raw_bytes_before_compression += raw_bytes
        summary.final_archive_bytes += archive_path.stat().st_size
        return True
    except Exception as exc:
        summary.failures.append(f"archive {journey.session_id}: {type(exc).__name__}: {exc}")
        summary.retained_source_files += sum(path.exists() for path in files)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def cleanup_completed_journeys(
    log_dir: str | Path,
    *,
    active_session_id: str,
    additional_active_session_ids: Iterable[str] = (),
) -> CleanupSummary:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    summary = CleanupSummary()
    # A .tmp is never authoritative: sources are retained until final rename,
    # so an interrupted attempt can always restart from the loose files.
    for stale in directory.glob("journey_*.zip.tmp"):
        try:
            stale.unlink()
        except OSError as exc:
            summary.failures.append(f"stale archive {stale.name}: {exc}")
    journeys = discover_completed_journeys(
        directory,
        active_session_id,
        additional_active_session_ids=additional_active_session_ids,
    )
    summary.completed_journeys_found = len(journeys)
    for journey in journeys:
        cleaned = _clean_empty_files(journey, summary)
        archive_journey(cleaned, summary)
    return summary


def iter_archive_entries(path: str | Path, *, prefix: str = "") -> Iterator[str]:
    with zipfile.ZipFile(path, "r") as archive:
        for name in archive.namelist():
            if name != MANIFEST_NAME and Path(name).name.startswith(prefix):
                yield name


class ArchiveTextReader:
    """Context manager that streams one UTF-8 log from loose CSV or ZIP."""

    def __init__(self, path: str | Path, *, member_prefix: str = "") -> None:
        self.path = Path(path)
        self.member_prefix = member_prefix
        self._archive: zipfile.ZipFile | None = None
        self._binary: Any = None
        self._text: TextIO | None = None

    def __enter__(self) -> TextIO:
        if self.path.suffix.lower() != ".zip":
            self._text = self.path.open("r", encoding="utf-8-sig", newline="")
            return self._text
        self._archive = zipfile.ZipFile(self.path, "r")
        candidates = [
            name for name in self._archive.namelist()
            if name != MANIFEST_NAME and Path(name).name.startswith(self.member_prefix) and name.lower().endswith(".csv")
        ]
        if not candidates:
            self._archive.close()
            raise ValueError(f"No {self.member_prefix or 'CSV'} log in archive {self.path}")
        self._binary = self._archive.open(sorted(candidates)[0], "r")
        import io
        self._text = io.TextIOWrapper(self._binary, encoding="utf-8-sig", newline="")
        return self._text

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._text is not None:
            self._text.close()
        if self._archive is not None:
            self._archive.close()
