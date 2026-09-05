from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from .i2c_controller import I2cController
from .led_app import LedApp
from .runtime_types import ConnectionAttemptEvent, EnvironmentSnapshot, PsaPollResult
from .session_identity import allocate_session_identity
from .source_adapters import OfflineSweepSource, ReplaySource, SourceFrame
from .state_store import DashboardStateStore, waiting_snapshot
from .telemetry import TelemetrySnapshot
from .telemetry_engine import TelemetryEngine
from .telemetry_logger import TelemetryLogger


@dataclass(frozen=True)
class RuntimeConfig:
    source: str = "live"  # live | replay | sweep
    replay_log: str | None = None
    replay_random_start: bool = True
    replay_speed: float = 1.0
    log_dir: str | Path | None = None
    enable_leds: bool = False
    led_reverse: bool = False
    led_max_band_width_fraction: float = 0.50
    enable_i2c: bool = True
    i2c_bus: int = 11
    bh1750_address: int = 0x23
    ambient_poll_interval_s: float = 1.0
    # Calibration mode is intentionally the default: collect lux history first
    # and leave existing UI/LED brightness unchanged.
    enable_ambient_brightness: bool = False


class FoxDashRuntime:
    """Owns lifecycle and the full telemetry train.

    Reader/replay source -> telemetry engine -> state store -> UI/LED.
    Logger observes events and snapshots but never becomes part of the
    transport path. This is the manager, not a god object: it coordinates
    limbs while each limb keeps its own job.
    """

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.logger = TelemetryLogger(config.log_dir)
        identity = allocate_session_identity(self.logger.log_dir)
        self.session_sequence = identity.sequence
        self.session_started_at = identity.started_at
        self.session_id = identity.session_id
        self.boot_id = self._boot_id()
        self.store = DashboardStateStore(source_name=config.source)
        self.engine = TelemetryEngine(
            session_id=self.session_id,
            boot_id=self.boot_id,
            session_started_at=self.session_started_at,
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reader = None
        self._i2c = I2cController(
            self._publish_environment,
            bus_number=config.i2c_bus,
            address=config.bh1750_address,
            poll_interval_s=config.ambient_poll_interval_s,
        )
        self._led = LedApp(
            self.store,
            reverse=config.led_reverse,
            max_mood_width_fraction=config.led_max_band_width_fraction,
            use_ambient_brightness=config.enable_ambient_brightness,
        )
        self._last_publish_monotonic = time.monotonic()

    @staticmethod
    def _boot_id() -> str:
        """Use the OS boot identity where available; fall back portably."""
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
        return "runtime-" + uuid.uuid4().hex

    def start(self) -> None:
        self.logger.start(source_name=self.config.source, session_id=self.session_id, boot_id=self.boot_id, session_started_at=self.session_started_at)
        if self.config.enable_i2c:
            self.logger.note(
                f"Ambient sensor: BH1750 bus {self.config.i2c_bus}, "
                f"address 0x{self.config.bh1750_address:02X}, "
                f"poll {self.config.ambient_poll_interval_s:g}s, "
                f"auto-brightness={'on' if self.config.enable_ambient_brightness else 'calibration/logging only'}"
            )
            self._i2c.start()
        if self.config.enable_leds:
            self._led.start()
        self._thread = threading.Thread(target=self._run, name="foxdash-runtime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._reader is not None:
            self._reader.stop()
        if self._thread is not None:
            self._thread.join(timeout=4.0)
        self._i2c.stop()
        self._led.stop()
        self.logger.close()

    def _run(self) -> None:
        try:
            if self.config.source == "live":
                self._run_live()
            elif self.config.source == "replay":
                self._run_replay()
            elif self.config.source == "sweep":
                self._run_sweep()
            else:
                raise ValueError(f"Unsupported runtime source: {self.config.source}")
        except Exception as exc:
            self.logger.note(f"Runtime fatal: {type(exc).__name__}: {exc}")
            self._publish_status(f"runtime error: {type(exc).__name__}", connection="error")

    def _run_live(self) -> None:
        from .obd_reader import ObdReaderConfig, PsaObdReader
        self._reader = PsaObdReader(ObdReaderConfig())
        self._publish_status("waiting for USB OBD adapter", connection="reconnecting")
        self._reader.run_forever(
            self._stop.is_set,
            on_connection_attempt=self._on_connection_attempt,
            on_poll=self._on_live_poll,
            on_error=self._on_reader_error,
        )

    def _run_replay(self) -> None:
        if not self.config.replay_log:
            raise ValueError("Replay source requires a CSV log path")
        source = ReplaySource(
            self.config.replay_log,
            random_start=self.config.replay_random_start,
            speed=self.config.replay_speed,
        )
        for frame in source.frames(self._stop.is_set):
            self._on_source_frame(frame)

    def _run_sweep(self) -> None:
        for frame in OfflineSweepSource().frames(self._stop.is_set):
            self._on_source_frame(frame)

    def _on_connection_attempt(self, event: ConnectionAttemptEvent) -> None:
        self.logger.log_connection_attempt(event)
        if not event.ok:
            self._publish_status(f"{event.stage}: {event.error or event.port_diagnostics}", connection="reconnecting")

    def _on_reader_error(self, message: str) -> None:
        self.logger.note(message)
        self._publish_status(message, connection="reconnecting")

    def _on_live_poll(self, result: PsaPollResult) -> None:
        self.logger.log_poll_result(result)
        # Setup-only result belongs in raw log but has no values to publish.
        if not result.canonical:
            return
        snapshot = self.engine.process(
            result.canonical,
            timestamp=result.timestamp,
            sample=result.sample,
            obd_connection="live",
            adapter_state=f"USB/{result.port}",
            ecu_session_state="engine/SID807",
            protocol=result.protocol,
            poll_health=result.poll_health,
            poll_ok=result.poll_ok,
            last_update_age_s=0.0,
        )
        self._publish(snapshot, source_name="live")

    def _on_source_frame(self, frame: SourceFrame) -> None:
        snapshot = self.engine.process(
            frame.canonical,
            timestamp=frame.timestamp,
            sample=frame.sample,
            obd_connection=frame.source_name,
            adapter_state=frame.adapter_state,
            protocol=frame.protocol,
            poll_health=frame.poll_health,
            poll_ok=frame.poll_ok,
            last_update_age_s=0.0,
        )
        self._publish(snapshot, source_name=frame.source_name)

    def _publish_environment(self, environment: EnvironmentSnapshot) -> None:
        """Publish and record the independent low-rate sensor stream."""
        self.store.publish_environment(environment)
        self.logger.log_environment(environment)

    def _publish(self, snapshot: TelemetrySnapshot, *, source_name: str) -> None:
        # Correlate each normal UI/OBD row with the newest ambient state.  The
        # dedicated ambient CSV retains every one-second sensor sample too.
        environment = self.store.latest().environment
        snapshot = replace(
            snapshot,
            ambientLuxRaw=environment.ambient_lux_raw,
            ambientLuxFiltered=environment.ambient_lux_filtered,
            ambientLightSensorOk=environment.sensor_ok,
            ambientLightState=environment.light_state,
        )
        self._last_publish_monotonic = time.monotonic()
        self.store.publish_telemetry(snapshot, source_name=source_name)
        self.logger.log_snapshot(snapshot)

    def _publish_status(self, detail: str, *, connection: str) -> None:
        previous = self.store.latest().telemetry
        age = max(0.0, time.monotonic() - self._last_publish_monotonic)
        base = previous if previous.sample else waiting_snapshot(detail)
        snapshot = replace(
            base,
            sessionId=self.session_id,
            bootId=self.boot_id,
            sessionStartedAt=self.session_started_at,
            obdConnection=connection,
            adapterState=detail,
            pollHealth="--",
            lastUpdateAge_s=age,
            moodState="reconnecting" if connection == "reconnecting" else "no OBD",
        )
        self.store.publish_telemetry(snapshot, source_name=self.config.source)
