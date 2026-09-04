from __future__ import annotations

"""Live serial/ELM reader.

It owns ports, ELM setup, polling and decoding. It emits events/results to
the runtime and does not open CSV files, render a console or calculate UI
scores. That is the whole refactor in one boundary.
"""

import os
import re
import time
from dataclasses import dataclass
from glob import glob
from typing import Callable, Iterable

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except ImportError as exc:  # pragma: no cover - setup issue
    raise SystemExit("pyserial is required. Run scripts/linux/setup.sh or scripts/windows/setup.bat.") from exc

from .psa_protocol import (
    build_canonical,
    clean_response_lines,
    decode_body,
    extract_hex_frames,
    reassemble_isotp_raw_header,
    response_clean,
)
from .runtime_types import ConnectionAttemptEvent, PsaPollResult, RawObdEvent


def now_iso() -> str:
    import datetime as dt
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class ObdReaderConfig:
    # Edit these internal constants when hardware changes. The runner does
    # not inject serial configuration through mystery shell variables.
    port_candidates: tuple[str, ...] = (
        "/dev/serial/by-id/*vLinker*",
        "/dev/serial/by-id/*",
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
        "COM3", "COM4", "COM5", "COM6", "COM7", "COM8",
    )
    baudrates: tuple[int, ...] = (115200,)
    serial_timeout_seconds: float = 1.0
    command_timeout_seconds: float = 2.5
    connect_retry_delay_seconds: float = 2.0
    poll_interval_ms: int = 250
    active_profiles: tuple[str, ...] = ("engine",)
    profile_setup_mode: str = "once_per_session"


COMMON_SETUP = (
    "ATZ", "ATWS", "ATD", "ATE0", "ATL0", "ATH1", "ATS0", "ATAL", "ATV1", "ATSP6",
)
PROFILE_SETUP = {
    "engine": ("ATSH6A8", "ATCRA688", "ATFCSH6A8", "ATFCSD300000", "ATFCSM1", "81"),
}
POLL_COMMANDS = {
    "engine": ("21C98001", "21CA8001", "21CB8001", "21CC8001", "21CD8001"),
}


class ElmConnection:
    def __init__(self, port: str, baudrate: int, *, serial_timeout_seconds: float) -> None:
        self.serial = serial.Serial(port=port, baudrate=baudrate, timeout=serial_timeout_seconds)
        time.sleep(0.3)
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

    def close(self) -> None:
        try:
            self.serial.close()
        except Exception:
            pass

    def command(self, command: str, *, timeout: float) -> tuple[str, float]:
        start = time.perf_counter()
        self.serial.reset_input_buffer()
        self.serial.write((command.strip() + "\r").encode("ascii"))
        self.serial.flush()
        chunks: list[bytes] = []
        deadline = start + timeout
        while time.perf_counter() < deadline:
            waiting = self.serial.in_waiting
            if waiting:
                data = self.serial.read(waiting)
                chunks.append(data)
                if b">" in data or b">" in b"".join(chunks[-3:]):
                    break
            else:
                time.sleep(0.003)
        return b"".join(chunks).decode("latin1", errors="replace"), (time.perf_counter() - start) * 1000.0


def expand_port_candidates(candidates: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        matches = sorted(glob(candidate))
        # On Windows COM ports do not exist as regular filesystem paths;
        # list_ports gives us real candidates below. Keep configured COMs too.
        items = matches or [candidate]
        for item in items:
            if item not in seen:
                result.append(item)
                seen.add(item)
    for item in list_ports.comports():
        if item.device not in seen:
            result.append(item.device)
            seen.add(item.device)
    return result


def port_diagnostics(port: str) -> tuple[bool, str]:
    if os.name == "nt" and re.fullmatch(r"COM\d+", port.upper()):
        return True, "windows COM candidate"
    try:
        st = os.stat(port)
        return True, f"exists mode={oct(st.st_mode & 0o777)} uid={st.st_uid} gid={st.st_gid}"
    except FileNotFoundError:
        return False, "missing"
    except OSError as exc:
        return False, f"stat_error {type(exc).__name__}: {exc}"


class PsaObdReader:
    def __init__(self, config: ObdReaderConfig | None = None) -> None:
        self.config = config or ObdReaderConfig()
        self._elm: ElmConnection | None = None
        self._port = ""
        self._identity = ""
        self._sample = 0

    def stop(self) -> None:
        if self._elm is not None:
            self._elm.close()
            self._elm = None

    def run_forever(
        self,
        stop_requested: Callable[[], bool],
        *,
        on_connection_attempt: Callable[[ConnectionAttemptEvent], None],
        on_poll: Callable[[PsaPollResult], None],
        on_error: Callable[[str], None],
    ) -> None:
        attempt = 0
        while not stop_requested():
            if self._elm is None:
                self._connect_once(on_connection_attempt, on_error, attempt)
                attempt += 1
                if self._elm is None:
                    time.sleep(self.config.connect_retry_delay_seconds)
                    continue
                setup_events = self._run_setup()
                # Setup is deliberately emitted as raw events in a zero-value
                # result so the logger sees it but the engine does not score it.
                if setup_events:
                    on_poll(PsaPollResult(
                        timestamp=now_iso(), sample=self._sample, canonical={}, decoded_by_command={}, bodies_by_command={},
                        raw_events=setup_events, poll_ok_count=0, poll_total_count=0, parse_issues=[],
                        port=self._port, adapter_identity=self._identity,
                    ))

            try:
                started = time.perf_counter()
                result = self._poll_once()
                on_poll(result)
                sleep_s = max(0.0, self.config.poll_interval_ms / 1000.0 - (time.perf_counter() - started))
                if sleep_s:
                    time.sleep(sleep_s)
            except Exception as exc:
                on_error(f"reader poll error: {type(exc).__name__}: {exc}")
                self.stop()
                time.sleep(self.config.connect_retry_delay_seconds)

    def _connect_once(
        self,
        on_attempt: Callable[[ConnectionAttemptEvent], None],
        on_error: Callable[[str], None],
        outer_attempt: int,
    ) -> None:
        for port in expand_port_candidates(self.config.port_candidates):
            exists, diag = port_diagnostics(port)
            on_attempt(ConnectionAttemptEvent(now_iso(), outer_attempt, port, exists, diag, "", "port_check", exists, error="" if exists else diag))
            if not exists:
                continue
            for baud in self.config.baudrates:
                elm: ElmConnection | None = None
                try:
                    elm = ElmConnection(port, baud, serial_timeout_seconds=self.config.serial_timeout_seconds)
                    raw, elapsed = elm.command("ATI", timeout=2.0)
                    clean = response_clean(clean_response_lines(raw))
                    ok = bool(raw.strip())
                    on_attempt(ConnectionAttemptEvent(now_iso(), outer_attempt, port, True, diag, baud, "ATI", ok, elapsed, clean, "" if ok else "empty ATI response"))
                    if ok:
                        self._elm = elm
                        self._port = port
                        self._identity = raw.strip()
                        return
                    elm.close()
                except Exception as exc:
                    on_attempt(ConnectionAttemptEvent(now_iso(), outer_attempt, port, True, diag, baud, "open_or_ATI", False, None, "", f"{type(exc).__name__}: {exc}"))
                    if elm is not None:
                        elm.close()
        on_error("No USB OBD adapter/session available yet")

    def _run_setup(self) -> list[RawObdEvent]:
        if self._elm is None:
            return []
        events: list[RawObdEvent] = []
        for command in COMMON_SETUP:
            events.append(self._run_setup_command("common_setup", "common", command))
        for profile in self.config.active_profiles:
            for command in PROFILE_SETUP.get(profile, ()):
                events.append(self._run_setup_command("profile_setup", profile, command))
        return events

    def _run_setup_command(self, phase: str, profile: str, command: str) -> RawObdEvent:
        assert self._elm is not None
        raw, elapsed = self._elm.command(command, timeout=self.config.command_timeout_seconds)
        clean = response_clean(clean_response_lines(raw))
        return RawObdEvent(now_iso(), self._sample, phase, profile, command, bool(clean), elapsed, clean, "", "", None, "setup", raw)

    def _poll_once(self) -> PsaPollResult:
        if self._elm is None:
            raise RuntimeError("reader not connected")
        self._sample += 1
        timestamp = now_iso()
        decoded_by_command: dict[str, dict[str, dict[str, object]]] = {}
        bodies_by_command: dict[str, bytes] = {}
        raw_events: list[RawObdEvent] = []
        poll_ok_count = 0
        poll_total_count = 0
        parse_issues: list[str] = []

        for profile in self.config.active_profiles:
            for command in POLL_COMMANDS.get(profile, ()):
                raw, elapsed = self._elm.command(command, timeout=self.config.command_timeout_seconds)
                clean = response_clean(clean_response_lines(raw))
                parsed = reassemble_isotp_raw_header(extract_hex_frames(clean_response_lines(raw)))
                ok = parsed.body is not None
                poll_total_count += 1
                if ok:
                    poll_ok_count += 1
                else:
                    parse_issues.append(f"{command}:{parsed.parse_status}")
                raw_events.append(RawObdEvent(
                    now_iso(), self._sample, "poll", profile, command, ok, elapsed, clean,
                    parsed.payload_hex, parsed.body_hex, len(parsed.body) if parsed.body is not None else None,
                    parsed.parse_status, raw,
                ))
                if parsed.body is None:
                    continue
                bodies_by_command[command] = parsed.body
                decoded_by_command[command] = decode_body(command, parsed.body)

        canonical = build_canonical(decoded_by_command) if decoded_by_command else {}
        canonical["timestamp"] = timestamp
        canonical["sample"] = self._sample
        return PsaPollResult(
            timestamp=timestamp, sample=self._sample, canonical=canonical,
            decoded_by_command=decoded_by_command, bodies_by_command=bodies_by_command,
            raw_events=raw_events, poll_ok_count=poll_ok_count, poll_total_count=poll_total_count,
            parse_issues=parse_issues, port=self._port, adapter_identity=self._identity,
        )
