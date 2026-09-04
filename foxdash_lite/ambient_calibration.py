from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .log_format import CompactDictWriter

from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Horizontal
from textual.widgets import Static

from .backlight import HyperPixelBacklightController, USABLE_MAX, USABLE_MIN
from .i2c_controller import I2cController
from .state_store import DashboardStateStore


@dataclass(frozen=True)
class CalibrationPoint:
    timestamp: str
    ambient_lux_raw: float
    ambient_lux_filtered: float
    brightness_factor: float
    palette_percent: float
    backlight_value: int
    backlight_percent: float
    usable_backlight_min: int
    usable_backlight_max: int
    hardware_backlight_max: int | None
    sensor_bus: int | None
    sensor_address: int | None


class AmbientCalibrationApp(App[None]):
    """Manual ambient calibration for one linked visual-brightness control.

    Each keypress changes one normalised brightness factor. That factor drives
    both pieces of the real dashboard experience together:

    * HyperPixel PWM backlight: usable hardware range 6..56.
    * FoxDash palette: conservative night endpoint through to the full day UI.

    The BH1750 is only the input. Cali decides which linked visual level is
    comfortable at a given ambient reading, then SPACE records that relationship.
    """

    CSS = """
    Screen {
        background: #03050a;
        color: #eefbff;
        padding: 0 1;
    }

    #title {
        height: 2;
        padding: 0 1;
        content-align: center middle;
        text-style: bold;
        background: #07101a;
        color: #eefbff;
    }

    #sensor,
    #backlight,
    #capture,
    #help,
    #preview_status,
    #preview_neutral {
        padding: 0 1;
    }

    #sensor {
        height: 2;
        content-align: center middle;
        color: #a8bac7;
    }

    #backlight {
        height: 3;
        margin: 0 0 1 0;
        content-align: center middle;
        border: round #dce8f2;
        background: #10121a;
        color: #eefbff;
        text-style: bold;
    }

    #preview_status {
        height: 1;
        content-align: center middle;
        background: #07131a;
        color: #eefbff;
    }

    #preview_row {
        height: 8;
    }

    .preview-card {
        width: 1fr;
        height: 100%;
        margin: 0 1;
        padding: 0 1;
        border: round #54606f;
        color: #eefbff;
        content-align: center middle;
    }

    #preview_cyan {
        border: round #22dbff;
        background: #081923;
    }

    #preview_fuchsia {
        border: round #f04cff;
        background: #1b0b20;
    }

    #preview_neutral {
        height: 4;
        margin: 1 1 0 1;
        border: round #dce8f2;
        background: #10121a;
        color: #eefbff;
        content-align: center middle;
    }

    #capture {
        height: 2;
        margin: 1 0 0 0;
        content-align: center middle;
        color: #ffb000;
    }

    #help {
        height: 2;
        content-align: center middle;
        color: #a8bac7;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("h", "fine_dim", "-1"),
        ("l", "fine_brighten", "+1"),
        ("j", "coarse_dim", "-5"),
        ("k", "coarse_brighten", "+5"),
        ("[", "fine_dim", "-1"),
        ("]", "fine_brighten", "+1"),
        ("m", "minimum", "Minimum"),
        ("x", "maximum", "Maximum"),
        ("space", "capture", "Save point"),
    ]

    # Each pair is (night endpoint, day endpoint). The same one linked factor
    # interpolates every element of the preview, so no hidden separate palette
    # control can drift away from physical backlight control.
    _PALETTE: dict[str, dict[str, tuple[str, str]]] = {
        "screen": {
            "background": ("#010205", "#03050a"),
            "text": ("#73808b", "#eefbff"),
        },
        "title": {
            "background": ("#020408", "#07101a"),
            "text": ("#9daab5", "#eefbff"),
        },
        "sensor": {"text": ("#58656f", "#a8bac7")},
        "backlight": {
            "border": ("#66717c", "#dce8f2"),
            "background": ("#06080c", "#10121a"),
            "text": ("#a8b5bf", "#eefbff"),
        },
        "preview_status": {
            "background": ("#03080b", "#07131a"),
            "text": ("#9cc8d2", "#eefbff"),
        },
        "cyan": {
            "border": ("#0b7186", "#22dbff"),
            "background": ("#03090d", "#081923"),
            "text": ("#a2dce7", "#eefbff"),
        },
        "fuchsia": {
            "border": ("#7a1a87", "#f04cff"),
            "background": ("#0a040c", "#1b0b20"),
            "text": ("#e3a6e9", "#eefbff"),
        },
        "neutral": {
            "border": ("#77838d", "#dce8f2"),
            "background": ("#07080b", "#10121a"),
            "text": ("#b7c2cb", "#eefbff"),
        },
        "capture": {"text": ("#8d6508", "#ffb000")},
        "help": {"text": ("#58656f", "#a8bac7")},
    }

    def __init__(
        self,
        *,
        bus_number: int = 11,
        address: int = 0x23,
        poll_interval_s: float = 0.25,
        filter_tau_s: float = 0.8,
        start_backlight: int = USABLE_MAX,
        points_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self.store = DashboardStateStore(source_name="ambient-calibration")
        self.i2c = I2cController(
            self.store.publish_environment,
            bus_number=bus_number,
            address=address,
            poll_interval_s=poll_interval_s,
            filter_tau_s=filter_tau_s,
        )
        self.backlight = HyperPixelBacklightController()
        self.backlight_value = self.backlight.clamp(start_backlight)
        self.points_path = self._resolve_points_path(points_path)
        self._saved_count = 0
        self._last_capture = "No point saved yet."
        self._backlight_error: str | None = None
        self._hardware_max: int | None = None

    @staticmethod
    def _resolve_points_path(value: str | Path | None) -> Path:
        if value:
            return Path(value).expanduser()
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        return Path.home() / "CarOBD" / "calibration" / f"ambient_linked_brightness_points_{stamp}.csv"

    @property
    def brightness_factor(self) -> float:
        """One 0..1 factor shared by backlight and visual palette."""
        return (self.backlight_value - USABLE_MIN) / (USABLE_MAX - USABLE_MIN)

    @property
    def palette_percent(self) -> float:
        """FoxDash uses 1..100 for its approved night-to-day palette range."""
        return 1.0 + (99.0 * self.brightness_factor)

    @staticmethod
    def _lerp_hex(start: str, end: str, amount: float) -> str:
        amount = max(0.0, min(1.0, amount))
        left = tuple(int(start[index:index + 2], 16) for index in (1, 3, 5))
        right = tuple(int(end[index:index + 2], 16) for index in (1, 3, 5))
        mixed = tuple(round(a + ((b - a) * amount)) for a, b in zip(left, right))
        return "#%02x%02x%02x" % mixed

    def _palette_colour(self, section: str, part: str) -> Color:
        night, day = self._PALETTE[section][part]
        return Color.parse(self._lerp_hex(night, day, self.brightness_factor))

    def _apply_linked_palette(self) -> None:
        """Apply the exact same factor that currently drives PWM backlight."""
        self.screen.styles.background = self._palette_colour("screen", "background")
        self.screen.styles.color = self._palette_colour("screen", "text")

        title = self.query_one("#title", Static)
        title.styles.background = self._palette_colour("title", "background")
        title.styles.color = self._palette_colour("title", "text")

        self.query_one("#sensor", Static).styles.color = self._palette_colour("sensor", "text")

        backlight = self.query_one("#backlight", Static)
        backlight.styles.background = self._palette_colour("backlight", "background")
        backlight.styles.color = self._palette_colour("backlight", "text")
        backlight.styles.border = ("round", self._palette_colour("backlight", "border"))

        preview_status = self.query_one("#preview_status", Static)
        preview_status.styles.background = self._palette_colour("preview_status", "background")
        preview_status.styles.color = self._palette_colour("preview_status", "text")

        cyan = self.query_one("#preview_cyan", Static)
        cyan.styles.background = self._palette_colour("cyan", "background")
        cyan.styles.color = self._palette_colour("cyan", "text")
        cyan.styles.border = ("round", self._palette_colour("cyan", "border"))

        fuchsia = self.query_one("#preview_fuchsia", Static)
        fuchsia.styles.background = self._palette_colour("fuchsia", "background")
        fuchsia.styles.color = self._palette_colour("fuchsia", "text")
        fuchsia.styles.border = ("round", self._palette_colour("fuchsia", "border"))

        neutral = self.query_one("#preview_neutral", Static)
        neutral.styles.background = self._palette_colour("neutral", "background")
        neutral.styles.color = self._palette_colour("neutral", "text")
        neutral.styles.border = ("round", self._palette_colour("neutral", "border"))

        self.query_one("#capture", Static).styles.color = self._palette_colour("capture", "text")
        self.query_one("#help", Static).styles.color = self._palette_colour("help", "text")

    def compose(self) -> ComposeResult:
        yield Static("AMBIENT / LINKED BRIGHTNESS CALIBRATION", id="title")
        yield Static("Waiting for BH1750…", id="sensor")
        yield Static("LINKED BRIGHTNESS 100%", id="backlight")
        yield Static("14.4 V   |   OBD OK   |   FOXDASH LINKED DAY-PALETTE PREVIEW", id="preview_status")
        with Horizontal(id="preview_row"):
            yield Static("EFFICIENCY\n\nOPTIMAL  92%\n■■■■■■■■■□\n\nLight, legible cyan", id="preview_cyan", classes="preview-card")
            yield Static("MOOD\n\nCOMFORTABLE  88%\n■■■■■■■■■□\n\nFuchsia stays distinct", id="preview_fuchsia", classes="preview-card")
        yield Static("DRIVER  2,050 RPM   63 mph   6th\nTHERMAL / SYSTEM  89°C   |   A/C  6.4 bar\nALERT SAMPLE  DPF REGEN ACTIVE", id="preview_neutral")
        yield Static("No point saved yet.", id="capture")
        yield Static("[ / ] or H / L: ±1 linked step   J / K: ±5   M: 6 / night   X: 56 / day   SPACE: save lux → linked brightness   Q: quit", id="help")

    def on_mount(self) -> None:
        self._apply_backlight(self.backlight_value)
        self._apply_linked_palette()
        self.i2c.start()
        self.set_interval(0.10, self._refresh)

    def on_unmount(self) -> None:
        self.i2c.stop()

    def _refresh(self) -> None:
        environment = self.store.latest().environment
        raw = environment.ambient_lux_raw
        filtered = environment.ambient_lux_filtered
        if raw is None:
            state = environment.light_state.upper() if environment.light_state else "WAITING"
            error = f" | {environment.sensor_error}" if environment.sensor_error else ""
            sensor_text = (
                f"BH1750 {state}  |  i2c-{environment.sensor_bus if environment.sensor_bus is not None else '--'} "
                f"/ 0x{environment.sensor_address:02X}" if environment.sensor_address is not None
                else f"BH1750 {state}{error}"
            )
        else:
            delta = abs(float(raw) - float(filtered if filtered is not None else raw))
            stable_limit = max(0.5, float(raw) * 0.08)
            settling = "SETTLING" if delta > stable_limit else "STABLE"
            sensor_text = (
                f"BH1750  {float(raw):.1f} lx raw  |  "
                f"{float(filtered if filtered is not None else raw):.1f} lx smoothed  |  {settling}"
            )
        self.query_one("#sensor", Static).update(sensor_text)

        factor_percent = self.brightness_factor * 100.0
        hardware_max = "--" if self._hardware_max is None else str(self._hardware_max)
        state = "OK" if not self._backlight_error else "ERROR"
        self.query_one("#backlight", Static).update(
            f"LINKED BRIGHTNESS  {factor_percent:05.1f}%  |  "
            f"PWM {self.backlight_value:02d} / {hardware_max}  |  "
            f"palette {self.palette_percent:05.1f}%  |  "
            f"usable PWM {USABLE_MIN}–{USABLE_MAX}  |  {state}"
        )
        capture_suffix = f"  |  ERROR: {self._backlight_error}" if self._backlight_error else ""
        self.query_one("#capture", Static).update(
            f"{self._last_capture}   |   saved {self._saved_count}  |   {self.points_path.name}{capture_suffix}"
        )

    def _apply_backlight(self, value: int | float) -> None:
        self.backlight_value = self.backlight.clamp(value)
        status = self.backlight.set(self.backlight_value)
        self._hardware_max = status.hardware_max
        self._backlight_error = status.error
        if status.available and status.current is not None:
            # The driver can be externally changed. Display what it actually
            # accepted, while retaining the app's deliberate 6..56 boundary.
            self.backlight_value = self.backlight.clamp(status.current)

    def _set_backlight(self, value: int | float) -> None:
        target = self.backlight.clamp(value)
        if target == self.backlight_value and not self._backlight_error:
            return
        self._apply_backlight(target)
        self._apply_linked_palette()
        self._refresh()

    def action_fine_dim(self) -> None:
        self._set_backlight(self.backlight_value - 1)

    def action_fine_brighten(self) -> None:
        self._set_backlight(self.backlight_value + 1)

    def action_coarse_dim(self) -> None:
        self._set_backlight(self.backlight_value - 5)

    def action_coarse_brighten(self) -> None:
        self._set_backlight(self.backlight_value + 5)

    def action_minimum(self) -> None:
        self._set_backlight(USABLE_MIN)

    def action_maximum(self) -> None:
        self._set_backlight(USABLE_MAX)

    def action_capture(self) -> None:
        environment = self.store.latest().environment
        raw = environment.ambient_lux_raw
        filtered = environment.ambient_lux_filtered
        if not environment.sensor_ok or raw is None:
            self._last_capture = "Not saved: BH1750 has no current reading"
            self._refresh()
            return
        if self._backlight_error:
            self._last_capture = "Not saved: real backlight control is unavailable"
            self._refresh()
            return

        factor = self.brightness_factor
        point = CalibrationPoint(
            timestamp=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            ambient_lux_raw=float(raw),
            ambient_lux_filtered=float(filtered if filtered is not None else raw),
            brightness_factor=factor,
            palette_percent=self.palette_percent,
            backlight_value=self.backlight_value,
            backlight_percent=factor * 100.0,
            usable_backlight_min=USABLE_MIN,
            usable_backlight_max=USABLE_MAX,
            hardware_backlight_max=self._hardware_max,
            sensor_bus=environment.sensor_bus,
            sensor_address=environment.sensor_address,
        )
        try:
            self._append_point(point)
        except OSError as exc:
            self._last_capture = f"Not saved: {type(exc).__name__}: {exc}"
        else:
            self._saved_count += 1
            self._last_capture = (
                f"Saved {point.ambient_lux_raw:.1f} lx → linked {point.backlight_percent:.1f}% "
                f"(PWM {point.backlight_value}, palette {point.palette_percent:.1f}%)"
            )
        self._refresh()

    def _append_point(self, point: CalibrationPoint) -> None:
        self.points_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.points_path.exists() or self.points_path.stat().st_size == 0
        with self.points_path.open("a", newline="", encoding="utf-8") as handle:
            writer = CompactDictWriter(handle, fieldnames=list(CalibrationPoint.__dataclass_fields__.keys()))
            if new_file:
                writer.writeheader()
            writer.writerow({
                "timestamp": point.timestamp,
                "ambient_lux_raw": f"{point.ambient_lux_raw:.3f}",
                "ambient_lux_filtered": f"{point.ambient_lux_filtered:.3f}",
                "brightness_factor": f"{point.brightness_factor:.5f}",
                "palette_percent": f"{point.palette_percent:.2f}",
                "backlight_value": point.backlight_value,
                "backlight_percent": f"{point.backlight_percent:.2f}",
                "usable_backlight_min": point.usable_backlight_min,
                "usable_backlight_max": point.usable_backlight_max,
                "hardware_backlight_max": "" if point.hardware_backlight_max is None else point.hardware_backlight_max,
                "sensor_bus": "" if point.sensor_bus is None else point.sensor_bus,
                "sensor_address": "" if point.sensor_address is None else f"0x{point.sensor_address:02X}",
            })


def run_ambient_calibration(
    *,
    bus_number: int = 11,
    address: int = 0x23,
    poll_interval_s: float = 0.25,
    filter_tau_s: float = 0.8,
    start_backlight: int = USABLE_MAX,
    points_path: str | Path | None = None,
) -> int:
    app = AmbientCalibrationApp(
        bus_number=bus_number,
        address=address,
        poll_interval_s=poll_interval_s,
        filter_tau_s=filter_tau_s,
        start_backlight=start_backlight,
        points_path=points_path,
    )
    app.run()
    return 0
