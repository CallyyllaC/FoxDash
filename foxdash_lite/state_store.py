from __future__ import annotations

import threading
from dataclasses import replace

from .runtime_types import DashboardState, EnvironmentSnapshot
from .telemetry import TelemetrySnapshot


def waiting_snapshot(reason: str = "waiting/device") -> TelemetrySnapshot:
    """Safe initial state so UI consumers never need to touch serial state."""
    return TelemetrySnapshot(
        timestamp="",
        sample=0,
        obdConnection="reconnecting",
        adapterState=reason,
        ecuSessionState="engine/SID807",
        protocol="PSA UDS raw header",
        pollHealth="--",
        lastUpdateAge_s=None,
        dpfStatus="WAITING",
        dpfTrendArrow="·",
        drivingState="waiting",
        thermalState="unknown",
        moodState="reconnecting",
    )


class DashboardStateStore:
    """Thread-safe latest-state store.

    UI and LEDs read only the newest state. Historical samples go to the
    logger, not into an unbounded queue that eventually becomes a tiny RAM
    landfill. ``sequence`` lets consumers cheaply notice a new publish.
    """

    def __init__(self, *, source_name: str = "starting") -> None:
        self._lock = threading.RLock()
        self._state = DashboardState(
            telemetry=waiting_snapshot(),
            environment=EnvironmentSnapshot(),
            sequence=0,
            source_name=source_name,
        )

    def latest(self) -> DashboardState:
        with self._lock:
            return self._state

    def publish_telemetry(self, snapshot: TelemetrySnapshot, *, source_name: str | None = None) -> DashboardState:
        with self._lock:
            self._state = replace(
                self._state,
                telemetry=snapshot,
                sequence=self._state.sequence + 1,
                source_name=source_name if source_name is not None else self._state.source_name,
            )
            return self._state

    def publish_environment(self, environment: EnvironmentSnapshot) -> DashboardState:
        with self._lock:
            self._state = replace(
                self._state,
                environment=environment,
                sequence=self._state.sequence + 1,
            )
            return self._state
