from __future__ import annotations

"""Clock-independent identity for FoxDash logging sessions."""

import datetime as dt
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


_STATE_NAME = ".session-sequence.json"
_STATE_VERSION = 1
_SEQUENCE_WIDTH = 8
_SESSION_SEQUENCE = re.compile(r"^S(?P<sequence>\d{8})-")
_FILENAME_SEQUENCE = re.compile(r"(?:^|_)S(?P<sequence>\d{8})-")
_allocation_lock = threading.Lock()


@dataclass(frozen=True)
class SessionIdentity:
    """Stable session identity plus informational wall-clock metadata."""

    sequence: int
    session_id: str
    started_at: str


def session_sequence_from_id(session_id: str) -> int | None:
    match = _SESSION_SEQUENCE.match(session_id.strip())
    if match is None:
        return None
    return int(match.group("sequence"))


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


def _read_last_sequence(state_path: Path) -> int:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if int(payload.get("version", 0)) != _STATE_VERSION:
            return 0
        value = int(payload.get("last_sequence", 0))
        return max(0, value)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 0


def _scan_existing_sequence(log_dir: Path) -> int:
    highest = 0
    try:
        paths = tuple(log_dir.iterdir())
    except OSError:
        return highest
    for path in paths:
        match = _FILENAME_SEQUENCE.search(path.name)
        if match is not None:
            highest = max(highest, int(match.group("sequence")))
    return highest


def _persist_last_sequence(state_path: Path, sequence: int) -> None:
    temporary = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
    payload = {
        "version": _STATE_VERSION,
        "last_sequence": sequence,
    }
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, state_path)
    _flush_directory(state_path.parent)


def allocate_session_identity(log_dir: str | Path) -> SessionIdentity:
    """Allocate a durable monotonically increasing session identity.

    The sequence is persisted before the caller opens any log files. A power
    loss can therefore create a harmless gap, but it cannot make a completed
    later session sort before an earlier one merely because the Pi clock was
    stale. Existing new-format filenames are also scanned so deleting or
    corrupting the state file does not silently reuse an older sequence while
    the logs themselves still exist.
    """

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / _STATE_NAME

    with _allocation_lock:
        previous = max(
            _read_last_sequence(state_path),
            _scan_existing_sequence(directory),
        )
        sequence = previous + 1
        _persist_last_sequence(state_path, sequence)

    now = dt.datetime.now().astimezone()
    wall_stamp = now.strftime("%Y%m%d_%H%M%S")
    session_id = f"S{sequence:0{_SEQUENCE_WIDTH}d}-{wall_stamp}-{uuid.uuid4().hex[:6]}"
    return SessionIdentity(
        sequence=sequence,
        session_id=session_id,
        started_at=now.isoformat(timespec="seconds"),
    )
