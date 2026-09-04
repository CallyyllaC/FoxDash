from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .telemetry import TelemetrySnapshot


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """Low-rate physical environment inputs owned by the I²C worker.

    ``ambient_lux_raw`` is retained for calibration and analysis.  The filtered
    value is deliberately separate so future UI/LED control never destroys the
    original evidence needed to tune it sensibly.
    """

    ambient_lux_raw: float | None = None
    ambient_lux_filtered: float | None = None
    sensor_ok: bool = False
    light_state: str = "unavailable"
    sensor_bus: int | None = None
    sensor_address: int | None = None
    sensor_error: str = ""
    updated_at: str = ""
    sample: int = 0


@dataclass(frozen=True)
class DashboardState:
    """The one published in-memory state read by UI and future consumers."""

    telemetry: TelemetrySnapshot
    environment: EnvironmentSnapshot
    sequence: int
    source_name: str


@dataclass(frozen=True)
class ConnectionAttemptEvent:
    timestamp: str
    attempt: int
    port: str
    port_exists: bool
    port_diagnostics: str
    baudrate: int | str
    stage: str
    ok: bool
    elapsed_ms: float | None = None
    response_clean: str = ""
    error: str = ""


@dataclass(frozen=True)
class RawObdEvent:
    timestamp: str
    sample: int
    phase: str
    profile: str
    command: str
    ok: bool
    response_time_ms: float
    clean_lines: str
    payload_hex: str
    body_hex: str
    body_len: int | None
    parse_status: str
    raw: str


@dataclass
class PsaPollResult:
    timestamp: str
    sample: int
    canonical: dict[str, Any]
    decoded_by_command: dict[str, dict[str, dict[str, Any]]]
    bodies_by_command: dict[str, bytes]
    raw_events: list[RawObdEvent]
    poll_ok_count: int
    poll_total_count: int
    parse_issues: list[str]
    port: str
    adapter_identity: str
    protocol: str = "PSA UDS raw header"

    @property
    def poll_ok(self) -> bool:
        return self.poll_total_count > 0 and self.poll_ok_count == self.poll_total_count

    @property
    def poll_health(self) -> str:
        return f"{self.poll_ok_count}/{self.poll_total_count}"
