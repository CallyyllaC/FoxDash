from __future__ import annotations

import csv
import datetime as dt
import json
import os
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .psa_protocol import (
    CANONICAL_COLUMN_NAMES,
    CONNECTION_COLUMNS,
    DECODE_CHANGE_COLUMNS,
    RAW_COLUMNS,
    UnknownTracker,
    change_field_id,
    change_field_schema,
    diagnostic_value,
    write_final_values,
)
from .log_archive import CleanupSummary, cleanup_completed_journeys, safe_session_id
from .log_format import CompactDictWriter, status_schema
from .runtime_types import ConnectionAttemptEvent, EnvironmentSnapshot, PsaPollResult
from .session_identity import session_sequence_from_id
from .telemetry import DISPLAY_FIELD_NAMES, TelemetrySnapshot


def _session_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


_cleanup_lock = threading.Lock()
_active_session_ids: set[str] = set()


ENVIRONMENT_COLUMNS = [
    "updated_at", "sample",
    "ambient_lux_raw", "ambient_lux_filtered",
    "sensor_ok", "light_state", "sensor_bus", "sensor_address", "sensor_error",
]


class TelemetryLogger:
    """Session recorder. Receives events; never talks to OBD or UI."""

    def __init__(self, log_dir: str | Path | None = None, *, fsync_every_rows: int = 25) -> None:
        self.log_dir = Path(log_dir) if log_dir is not None else Path.home() / "CarOBD" / "logs"
        self.fsync_every_rows = max(1, int(fsync_every_rows))
        self._handles: dict[str, Any] = {}
        self._writers: dict[str, csv.DictWriter] = {}
        self._counts: dict[str, int] = {}
        self._tracker = UnknownTracker()
        self._last_values: dict[tuple[str, str], Any] = {}
        self._last_decoded: dict[str, dict[str, dict[str, Any]]] = {}
        self.paths: dict[str, Path] = {}
        self._unknown_header: list[str] | None = None
        self._lock = threading.RLock()
        self.cleanup_summary = CleanupSummary()
        self._session_id = ""
        self._cleanup_thread: threading.Thread | None = None

    def start(self, *, source_name: str, session_id: str = "", boot_id: str = "", session_started_at: str = "") -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = safe_session_id(session_id or _session_stamp())
        sequence = session_sequence_from_id(session_id or stamp)
        # Register the new session before opening any files or starting cleanup.
        # Archiving can take minutes on the Pi when a large backlog exists, so
        # it must never sit on the UI's synchronous startup path.
        with _cleanup_lock:
            _active_session_ids.add(stamp)
            self._session_id = stamp
        names = {
            "raw": f"psa_raw_blocks_{stamp}.csv",
            "connection": f"psa_connection_attempts_{stamp}.csv",
            "decoded": f"psa_decoded_core_{stamp}.csv",
            "changes": f"psa_decode_changes_{stamp}.csv",
            "unknown": f"psa_unknown_timeseries_{stamp}.csv",
            "unknown_report": f"psa_unknown_report_{stamp}.csv",
            "ui": f"psa_ui_display_values_{stamp}.csv",
            "ambient": f"psa_ambient_light_{stamp}.csv",
            "final": f"psa_final_values_{stamp}.csv",
            "text": f"psa_session_{stamp}.txt",
            "schema": f"psa_schema_{stamp}.json",
        }
        self.paths = {key: self.log_dir / name for key, name in names.items()}
        self._open_csv("raw", RAW_COLUMNS)
        self._open_csv("connection", CONNECTION_COLUMNS)
        self._open_csv("decoded", ["timestamp", "sample", *CANONICAL_COLUMN_NAMES])
        self._open_csv("changes", DECODE_CHANGE_COLUMNS)
        self._open_csv("ui", DISPLAY_FIELD_NAMES)
        self._open_csv("ambient", ENVIRONMENT_COLUMNS)
        text = open(self.paths["text"], "w", encoding="utf-8", newline="\n", buffering=1)
        self._handles["text"] = text
        text.write(f"FoxDash session started {session_started_at or dt.datetime.now().astimezone().isoformat(timespec='seconds')}\n")
        text.write(f"Session ID: {session_id or stamp}\n")
        if sequence is not None:
            text.write(f"Session Sequence: {sequence}\n")
            text.write("Chronology: session sequence authoritative; wall clock informational\n")
        text.write(f"Boot ID: {boot_id or 'unavailable'}\n")
        text.write(f"Source: {source_name}\n")
        text.write("Logging owns files; reader owns serial; UI owns pixels.\n\n")
        text.write("journey_cleanup scheduled in background\n")
        schema_payload = {
            "schema_version": 2,
            "session_id": session_id or stamp,
            "session_sequence": sequence,
            "boot_id": boot_id or "unavailable",
            "encoding": "utf-8",
            "newline": "\\n",
            "change_fields": change_field_schema(),
            "status_enums": status_schema(),
        }
        self.paths["schema"].write_text(
            json.dumps(schema_payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self._cleanup_thread = threading.Thread(
            target=self._run_cleanup,
            args=(stamp,),
            name="foxdash-log-cleanup",
            daemon=True,
        )
        self._cleanup_thread.start()

    def _run_cleanup(self, active_session_id: str) -> None:
        try:
            with _cleanup_lock:
                active_ids = tuple(_active_session_ids)
            summary = cleanup_completed_journeys(
                self.log_dir,
                active_session_id=active_session_id,
                additional_active_session_ids=active_ids,
            )
        except Exception as exc:
            # Archival must never prevent the vehicle logger from running.
            summary = CleanupSummary(failures=[f"cleanup run: {type(exc).__name__}: {exc}"])
        self.cleanup_summary = summary
        self.note(summary.to_log_line())

    def _open_csv(self, key: str, fields: list[str]) -> None:
        handle = open(self.paths[key], "w", newline="", encoding="utf-8", buffering=1)
        writer = CompactDictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        self._handles[key] = handle
        self._writers[key] = writer
        self._counts[key] = 0

    def _write(self, key: str, row: dict[str, Any]) -> None:
        # OBD and I²C have separate worker threads. Serialising file writes
        # keeps their session records intact rather than relying on luck and
        # CPython implementation trivia.
        with self._lock:
            self._writers[key].writerow(row)
            self._counts[key] += 1
            handle = self._handles[key]
            handle.flush()
            if self._counts[key] % self.fsync_every_rows == 0:
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass

    def note(self, message: str) -> None:
        with self._lock:
            handle = self._handles.get("text")
            if handle is not None and not handle.closed:
                handle.write(message.rstrip() + "\n")

    def log_connection_attempt(self, event: ConnectionAttemptEvent) -> None:
        if "connection" not in self._writers:
            return
        self._write("connection", asdict(event))

    def log_poll_result(self, result: PsaPollResult) -> None:
        if "raw" not in self._writers:
            return
        for event in result.raw_events:
            self._write("raw", asdict(event))
        if not result.decoded_by_command:
            return

        self._last_decoded = result.decoded_by_command
        self._write("decoded", {"timestamp": result.timestamp, "sample": result.sample, **result.canonical})

        for command, body in result.bodies_by_command.items():
            self._tracker.update_body(command, body)
        unknown_row = {"timestamp": result.timestamp, "sample": result.sample, **self._tracker.unknown_row(result.bodies_by_command)}
        if "unknown" not in self._writers:
            self._unknown_header = list(unknown_row.keys())
            self._open_csv("unknown", self._unknown_header)
        self._write("unknown", unknown_row)

        for command, decoded in result.decoded_by_command.items():
            for field_name, item in decoded.items():
                if item.get("alias_for"):
                    continue
                key = (command, field_name)
                new_value = item.get("value")
                old_value = self._last_values.get(key)
                if old_value != new_value:
                    delta: Any = ""
                    if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
                        delta = new_value - old_value
                    field = item["field"]
                    self._write("changes", {
                        "timestamp": result.timestamp, "sample": result.sample,
                        "field_id": change_field_id(command, field_name),
                        "old": "" if old_value is None else old_value,
                        "new": new_value, "delta": delta,
                        "raw_value": diagnostic_value(item.get("raw"), field),
                    })
                self._last_values[key] = new_value

    def log_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        if "ui" in self._writers:
            self._write("ui", asdict(snapshot))

    def log_environment(self, environment: EnvironmentSnapshot) -> None:
        if "ambient" in self._writers:
            self._write("ambient", asdict(environment))

    def close(self) -> None:
        try:
            if self._last_decoded:
                write_final_values(str(self.paths["final"]), self._last_decoded)
            self._tracker.write_report(str(self.paths["unknown_report"]))
            self.note(f"\nSession stopped {dt.datetime.now().astimezone().isoformat(timespec='seconds')}")
            for key, path in self.paths.items():
                self.note(f"{key}={path}")
        finally:
            with self._lock:
                for handle in list(self._handles.values()):
                    try:
                        handle.close()
                    except Exception:
                        pass
                self._handles.clear()
                self._writers.clear()
            with _cleanup_lock:
                _active_session_ids.discard(self._session_id)
            self._session_id = ""
