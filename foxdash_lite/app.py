from __future__ import annotations

from collections import deque
import math
import time
from typing import Any, Deque

from rich.console import RenderableType
from rich.align import Align
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static

from .formatting import (
    CYAN,
    FUCHSIA,
    fmt,
    fmt_pressure,
    fmt_sig,
    range_bar,
    score_bar,
    hero_score_bar,
    signed,
    signed_pressure,
    signed_sig,
    state_style,
    efficiency_state,
    mood_state,
    metric_table,
    bar_with_trend,
    value_delta_text,
    value_delta_percent_text,
)
from .runtime_types import EnvironmentSnapshot
from .state_store import DashboardStateStore
from .telemetry import TelemetrySnapshot
from .ui_text import UiText


class DitherCard(Widget):
    """Bordered card with a faint terminal-native texture beneath its content.

    Textual renders ``render()`` beneath children composed by ``compose()``.
    That lets the dither live safely inside the card without being painted over
    the border/title or the telemetry itself.
    """

    can_focus = False

    def __init__(self, content: RenderableType = "", *, texture_colour: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._content = content
        self._texture_colour = texture_colour

    def compose(self) -> ComposeResult:
        yield Static(self._content, classes="card-content")

    def update(self, content: RenderableType, *, layout: bool = True) -> None:
        self._content = content
        if self.is_mounted:
            self.query_one(".card-content", Static).update(content)

    def set_texture_colour(self, colour: str) -> None:
        if colour != self._texture_colour:
            self._texture_colour = colour
            self.refresh()

    def render(self) -> RenderableType:
        width = max(1, self.content_size.width)
        height = max(1, self.content_size.height)
        texture = Text()
        # Intentionally sparse and ASCII-only. One small dot per roughly 79
        # cells is enough to test material depth without becoming visual grit.
        for y in range(height):
            line = []
            for x in range(width):
                marker = ((x * 17) + (y * 31) + ((x // 4) * 7)) % 79
                line.append("." if marker == 0 else " ")
            texture.append("".join(line), style=self._texture_colour)
            if y < height - 1:
                texture.append("\n")
        return texture


class TrendTracker:
    """Tiny rolling trend tracker for glance arrows."""

    THRESHOLDS: dict[str, float] = {
        "coolant": 0.4,
        "fuelTemp": 0.4,
        "intakeTemp": 0.4,
        "ambientTemp": 0.4,
        "externalTemp": 0.4,
        "boostProxy": 25.0,
        "boostErrorProxy": 20.0,
        "railProxy": 25.0,
        "railErrorProxy": 15.0,
        "mapProxy": 25.0,
        "dpfDiffProxy": 1.0,
        "fapTemp": 1.0,
        "rpm": 40.0,
        "speed_mph": 0.5,
        "pedalProxy": 1.5,
        "airFlowMeasured": 20.0,
        "airFlowError": 20.0,
        "egrActual": 1.5,
        "egrError": 1.5,
        "airMixerActual": 1.5,
        "airMixerError": 1.5,
        "airCPress_bar": 0.2,
    }

    def __init__(self, maxlen: int = 8) -> None:
        self.maxlen = maxlen
        self._history: dict[str, Deque[float]] = {}

    def update(self, snapshot: TelemetrySnapshot) -> None:
        for key in self.THRESHOLDS:
            value = getattr(snapshot, key, None)
            if isinstance(value, (int, float)):
                q = self._history.setdefault(key, deque(maxlen=self.maxlen))
                q.append(float(value))

    def arrow(self, key: str) -> str:
        q = self._history.get(key)
        if not q or len(q) < 3:
            return "→"
        threshold = self.THRESHOLDS.get(key, 1.0)
        delta = q[-1] - q[0]
        if delta > threshold:
            return "↑"
        if delta < -threshold:
            return "↓"
        return "→"


class FoxDashApp(App[None]):
    """Textual prototype for the 4-inch dashboard UI."""

    CSS_PATH = "theme.tcss"

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("d", "toggle_debug", "Debug"),
        ("[", "dim_ui", "Dim UI"),
        ("]", "brighten_ui", "Brighten UI"),
    ]

    def __init__(
        self,
        *,
        state_store: DashboardStateStore,
        refresh_hz: float = 10.0,
        show_frame_counter: bool = False,
        layout_mode: str = "compact",
        emoji_mode: bool = False,
        ui_brightness: float = 100.0,
    ) -> None:
        super().__init__()
        self.state_store = state_store
        self.refresh_hz = max(1.0, min(refresh_hz, 60.0))
        self.show_debug = False
        self.show_frame_counter = show_frame_counter
        self.layout_mode = layout_mode
        self.emoji_mode = emoji_mode
        self._last_snapshot: TelemetrySnapshot | None = None
        self._last_trend_key: tuple[int, str] | None = None
        self.trends = TrendTracker()
        # Visual-only interpolation. Values/states remain raw/published.
        self._bar_visuals: dict[str, float] = {}
        self._bar_alpha = 0.35
        self._dpf_visual_temp: float | None = None
        self._dpf_thermal_alpha = 0.22
        self._feedback_classes = ("feedback-good", "feedback-caution", "feedback-alert")
        self._ui_brightness = self._clamp_brightness(ui_brightness)
        self.ui_text = UiText.load()

    def compose(self) -> ComposeResult:
        yield Static(id="status_bar")
        # One concise driving cue. It is intentionally singular and priority-based:
        # mechanical protection wins over economy when the two disagree.
        yield Static(id="feedback_bar")
        with Horizontal(id="score_row"):
            yield DitherCard(id="efficiency_card", classes="score-card", texture_colour="#0d2a35")
            yield DitherCard(id="mood_card", classes="score-card", texture_colour="#2a1031")
        with Horizontal(id="mid_row"):
            yield DitherCard(id="driver_card", classes="data-card wide-card", texture_colour="#1a2028")
            yield DitherCard(id="thermal_system_card", classes="data-card wide-card", texture_colour="#0d2b35")
        with Horizontal(id="bottom_row"):
            yield DitherCard(id="pressure_card", classes="data-card wide-card", texture_colour="#2a1031")
            yield DitherCard(id="flow_card", classes="data-card wide-card", texture_colour="#2a1031")
        # The DPF strip is a persistent footer: summary first, details above it.
        yield Static(id="dpf_bar")
        yield Static(id="debug_card", classes="debug-card hidden")

    def on_mount(self) -> None:
        self.screen.add_class(self.layout_mode)
        self._install_border_titles()
        self._apply_base_chrome()
        self._apply_card_alert_styles(self._last_snapshot) if self._last_snapshot else None
        self.set_interval(1.0 / self.refresh_hz, self.refresh_dashboard)
        self.refresh_dashboard()

    def _install_border_titles(self) -> None:
        """Place the persistent data-card titles in their upper borders.

        Textual draws border titles inside the top edge, so the labels stop
        consuming a whole content row.  The card CSS supplies the matching title
        background colour, creating the clean cut-through treatment used by the
        design reference.
        """
        titles = {
            "#driver_card": " DRIVER ",
            "#thermal_system_card": " THERMAL / SYSTEM ",
            "#pressure_card": " PRESSURE ",
            "#flow_card": " FLOW / EXHAUST ",
            "#debug_card": " DEBUG ",
        }
        for selector, title in titles.items():
            self.query_one(selector).border_title = title

    @staticmethod
    def _abs_over(value: Any, limit: float) -> bool:
        return isinstance(value, (int, float)) and abs(float(value)) > limit

    # Day/night palette endpoints. The day endpoint is the current full-bright
    # dashboard. The night endpoint is intentionally still readable rather than
    # trying to turn the car display into an almost-black stealth game HUD.
    _PALETTE: dict[str, dict[str, tuple[str, str]]] = {
        "screen": {"background": ("#010205", "#03050a")},
        "status": {"background": ("#03070b", "#07101a")},
        "efficiency_card": {
            "border": ("#0b7186", "#22dbff"), "background": ("#03090d", "#081923"),
            "title": ("#0b7186", "#22dbff"), "texture": ("#040b0f", "#0a2029"),
        },
        "mood_card": {
            "border": ("#7a1a87", "#f04cff"), "background": ("#0a040c", "#1b0b20"),
            "title": ("#7a1a87", "#f04cff"), "texture": ("#0d050f", "#210d26"),
        },
        "driver_card": {
            "border": ("#77838d", "#dce8f2"), "background": ("#07080b", "#10121a"),
            "title": ("#a4b0ba", "#eefbff"), "texture": ("#090b0f", "#141821"),
        },
        "thermal_system_card": {
            "border": ("#0b7186", "#22dbff"), "background": ("#03090d", "#081a23"),
            "title": ("#0b7186", "#22dbff"), "texture": ("#040b0f", "#0a2029"),
        },
        "pressure_card": {
            "border": ("#7a1a87", "#f04cff"), "background": ("#0a040c", "#1b0b20"),
            "title": ("#7a1a87", "#f04cff"), "texture": ("#0d050f", "#210d26"),
        },
        "flow_card": {
            "border": ("#7a1a87", "#f04cff"), "background": ("#0a040c", "#1b0b20"),
            "title": ("#7a1a87", "#f04cff"), "texture": ("#0d050f", "#210d26"),
        },
    }

    @staticmethod
    def _clamp_brightness(value: Any) -> float:
        try:
            return max(1.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            return 100.0

    def _palette_mix(self, dark: str, bright: str) -> str:
        # 1% is the dimmest approved night endpoint, 100% is full day.
        amount = (self._ui_brightness - 1.0) / 99.0
        return self._lerp_hex(dark, bright, amount)

    def _palette_colour(self, card: str, part: str) -> str:
        dark, bright = self._PALETTE[card][part]
        return self._palette_mix(dark, bright)

    def set_ui_brightness(self, percent: float) -> None:
        """Set UI palette brightness from a future ambient-light controller.

        The display/backlight itself remains hardware-owned. This only lerps the
        dashboard palette between the conservative night and full day endpoints.
        """
        value = self._clamp_brightness(percent)
        if abs(value - self._ui_brightness) < 0.05:
            return
        self._ui_brightness = value
        self._apply_base_chrome()
        if self._last_snapshot is not None:
            self._apply_card_alert_styles(self._last_snapshot)

    def action_dim_ui(self) -> None:
        self.set_ui_brightness(self._ui_brightness - 10.0)

    def action_brighten_ui(self) -> None:
        self.set_ui_brightness(self._ui_brightness + 10.0)

    def _apply_base_chrome(self) -> None:
        self.screen.styles.background = Color.parse(self._palette_colour("screen", "background"))
        self.query_one("#status_bar", Static).styles.background = Color.parse(self._palette_colour("status", "background"))

    def _pulse_colour(self, low: str, high: str, *, period: float = 2.4) -> str:
        phase = (math.sin((time.monotonic() * math.tau) / period) + 1.0) / 2.0
        # Keep the lower portion of the breath comparatively calm.
        phase = 0.20 + (0.80 * phase)
        return self._lerp_hex(low, high, phase)

    def _set_card_style(self, card_name: str, *, alert: bool) -> None:
        card = self.query_one(f"#{card_name}", DitherCard)
        if alert:
            border = self._pulse_colour("#741b25", "#ff5a52", period=2.2)
            background = self._palette_mix("#070204", "#1c090d")
            title = self._pulse_colour("#8e2730", "#ff7566", period=2.2)
            texture = self._palette_mix("#0b0205", "#28080e")
        else:
            border = self._palette_colour(card_name, "border")
            background = self._palette_colour(card_name, "background")
            title = self._palette_colour(card_name, "title")
            texture = self._palette_colour(card_name, "texture")
        card.styles.border = ("round", Color.parse(border))
        card.styles.background = Color.parse(background)
        card.styles.border_title_color = Color.parse(title)
        card.styles.border_title_background = Color.parse(background)
        card.set_texture_colour(texture)
        card.set_class(alert, "card-alert")

    def _apply_card_alert_styles(self, s: TelemetrySnapshot | None) -> None:
        """Apply family palette plus a slow breathing red edge for real alerts."""
        if s is None:
            for name in ("efficiency_card", "mood_card", "driver_card", "thermal_system_card", "pressure_card", "flow_card"):
                self._set_card_style(name, alert=False)
            return
        eff_key = efficiency_state(
            s.efficiencyScore,
            driving_state=s.drivingState,
            thermal_state=s.thermalState,
            dpf_status=s.dpfStatus,
            battery_v=s.batteryV,
        )
        mood_key = mood_state(
            s.moodScore,
            driving_state=s.drivingState,
            thermal_state=s.thermalState,
            obd_connection=s.obdConnection,
            battery_v=s.batteryV,
        )
        alerts = {
            "efficiency_card": eff_key in {"THERMAL", "DPF PRESS", "LOW VOLT"},
            "mood_card": mood_key in {"hot", "low volts", "no OBD"},
            "driver_card": False,
            "thermal_system_card": (
                isinstance(s.coolant, (int, float)) and float(s.coolant) > 105
            ) or (
                isinstance(s.fuelTemp, (int, float)) and float(s.fuelTemp) > 75
            ) or (
                isinstance(s.intakeTemp, (int, float)) and float(s.intakeTemp) > 75
            ),
            "pressure_card": self._abs_over(s.railErrorProxy, 160) or self._abs_over(s.boostErrorProxy, 220),
            "flow_card": (
                self._abs_over(s.airFlowError, 20)
                or self._abs_over(s.egrError, 20)
                or self._abs_over(s.airMixerError, 20)
            ),
        }
        for name, active in alerts.items():
            self._set_card_style(name, alert=active)

    def action_toggle_debug(self) -> None:
        self.show_debug = not self.show_debug
        debug = self.query_one("#debug_card", Static)
        debug.set_class(not self.show_debug, "hidden")
        self.refresh_dashboard()

    def refresh_dashboard(self) -> None:
        state = self.state_store.latest()
        snap = state.telemetry
        self._last_snapshot = snap
        trend_key = (snap.sample, snap.timestamp)
        if trend_key != self._last_trend_key:
            self.trends.update(snap)
            self._last_trend_key = trend_key
        self.query_one("#status_bar", Static).update(self.render_status(snap))
        feedback = self.query_one("#feedback_bar", Static)
        feedback.update(self.render_feedback(snap))
        self._apply_feedback_style(feedback, snap)
        dpf = self.query_one("#dpf_bar", Static)
        dpf.update(self.render_dpf(snap))
        self._apply_dpf_temperature_style(dpf, snap.fapTemp)
        self._update_score_border_titles(snap)
        self.query_one("#efficiency_card", DitherCard).update(self.render_efficiency(snap))
        self.query_one("#mood_card", DitherCard).update(self.render_mood(snap))
        self.query_one("#driver_card", DitherCard).update(self.render_driver(snap))
        self.query_one("#thermal_system_card", DitherCard).update(self.render_thermal_system(snap))
        self.query_one("#pressure_card", DitherCard).update(self.render_pressure(snap))
        self.query_one("#flow_card", DitherCard).update(self.render_flow_exhaust(snap))
        self._apply_card_alert_styles(snap)
        if self.show_debug:
            self.query_one("#debug_card", Static).update(self.render_debug(snap, state.environment))

    def _animated_value(self, key: str, target: Any) -> Any:
        if not isinstance(target, (int, float)):
            return target
        t = float(target)
        prev = self._bar_visuals.get(key)
        if prev is None:
            self._bar_visuals[key] = t
            return t
        if abs(t - prev) < 0.01:
            self._bar_visuals[key] = t
            return t
        current = prev + (t - prev) * self._bar_alpha
        self._bar_visuals[key] = current
        return current

    def _score_bar(self, key: str, value: Any, *, width: int = 10, muted: bool = False) -> Text:
        visual = self._animated_value(f"score:{key}", value)
        return score_bar(visual, width=width, style_value=value, muted=muted)

    def _hero_score_bar(
        self,
        key: str,
        value: Any,
        *,
        width: int = 22,
        track_colour: str = "#142a35",
    ) -> Text:
        """Single-row visual anchor for the primary Efficiency/Mood result.

        The raw score and state remain immediate. Only the filled length is
        visually interpolated, exactly like the compact bars elsewhere. The
        dark card-family track is static, so the active fill grows *inside* a
        stable rail rather than appearing to float in the card.
        """
        visual = self._animated_value(f"hero-score:{key}", value)
        return hero_score_bar(
            visual,
            width=width,
            style_value=value,
            track_colour=track_colour,
        )

    def _metric_bar(
        self,
        key: str,
        value: Any,
        *,
        lo: float,
        hi: float,
        width: int = 5,
        kind: str = "",
        absolute: bool = False,
        scale: str | None = None,
    ) -> Text:
        if isinstance(value, (int, float)) and absolute:
            target = abs(float(value))
        else:
            target = value
        visual = self._animated_value(f"metric:{key}", target)
        return range_bar(visual, lo=lo, hi=hi, width=width, kind=kind, style_value=value, scale=scale)

    def _obd_status_kind(self, s: TelemetrySnapshot) -> str:
        """Classify live transport health for the tiny status heartbeat."""
        source = (s.obdConnection or "").lower()
        stale = float(s.lastUpdateAge_s) if isinstance(s.lastUpdateAge_s, (int, float)) else 0.0
        if any(word in source for word in ("missing", "failed", "offline", "no obd", "error", "disconnect")):
            return "lost"
        if not source:
            return "lost"
        if stale > 2.0 or any(word in source for word in ("wait", "sweep", "reconnect", "pending")):
            return "watch"
        if any(word in source for word in ("replay", "sim", "live", "connected", "ok")):
            return "healthy"
        return "watch"

    def _obd_label(self, s: TelemetrySnapshot) -> str:
        source = (s.obdConnection or "").lower()
        if "replay" in source:
            return "OBD REPLAY"
        if "sim" in source:
            return "OBD SIM"
        if "sweep" in source or "connect" in source:
            return "OBD WAIT"
        if any(word in source for word in ["live", "connected", "ok"]):
            return "OBD OK"
        if self._obd_status_kind(s) == "lost":
            return "OBD LOST"
        return f"OBD {s.obdConnection.upper()}" if s.obdConnection else "OBD LOST"

    def _obd_status_colour(self, s: TelemetrySnapshot) -> str:
        kind = self._obd_status_kind(s)
        if kind == "healthy":
            return self._pulse_colour("#1d7649", "#79f0a4", period=3.4)
        if kind == "watch":
            return self._pulse_colour("#804800", "#ffb000", period=2.0)
        return self._pulse_colour("#7d1e27", "#ff645b", period=1.3)

    def _miles_compact(self, value: Any) -> str:
        if isinstance(value, (int, float)):
            v = float(value)
            if abs(v) >= 1000:
                return f"{v/1000:.1f}k mi"
            return f"{v:.0f}mi"
        return "--"

    def _regen_remaining_text(self, s: TelemetrySnapshot) -> str:
        """Estimated distance until next regen from avg interval minus last regen.

        This is a practical display estimate, not ECU prophecy. Tiny dashboard,
        tiny fortune teller, all suitably suspicious.
        """
        if isinstance(s.avg10Regen_mi, (int, float)) and isinstance(s.lastRegen_mi, (int, float)):
            remaining = float(s.avg10Regen_mi) - float(s.lastRegen_mi)
            if remaining <= 25:
                return "soon"
            return f"~{remaining:.0f}mi"
        return "--"

    _DPF_THERMAL_STOPS: tuple[tuple[float, str, str], ...] = (
        # Cold begins neutral/slate, not blue. The DPF is a hot exhaust system,
        # not a little aquarium just because temperature is low.
        (0.0, "#66717c", "#0a1017"),
        (100.0, "#777766", "#11110e"),
        (150.0, "#8d7b54", "#15120d"),
        (200.0, "#ad8740", "#191309"),
        (250.0, "#cf9425", "#1d1107"),
        (300.0, "#e5a419", "#211005"),
        (350.0, "#ffb000", "#241004"),
        (400.0, "#ff8800", "#280d04"),
        (450.0, "#ff6100", "#2b0905"),
        (500.0, "#f23a2d", "#2c070a"),
    )

    @staticmethod
    def _lerp_hex(start: str, end: str, amount: float) -> str:
        """Blend two RGB hex colours for terminal-safe stepped thermal light."""
        amount = max(0.0, min(1.0, amount))
        a = tuple(int(start[index:index + 2], 16) for index in (1, 3, 5))
        b = tuple(int(end[index:index + 2], 16) for index in (1, 3, 5))
        blended = tuple(round(left + ((right - left) * amount)) for left, right in zip(a, b))
        return "#%02x%02x%02x" % blended

    def _dpf_colours(self, temp_c: float | None) -> tuple[str, str]:
        """Return interpolated edge and tint colours across DPF heat bands."""
        if temp_c is None:
            return self._DPF_THERMAL_STOPS[0][1], self._DPF_THERMAL_STOPS[0][2]
        stops = self._DPF_THERMAL_STOPS
        if temp_c <= stops[0][0]:
            return stops[0][1], stops[0][2]
        for index in range(1, len(stops)):
            upper_t, upper_border, upper_bg = stops[index]
            lower_t, lower_border, lower_bg = stops[index - 1]
            if temp_c <= upper_t:
                amount = (temp_c - lower_t) / max(1.0, upper_t - lower_t)
                return (
                    self._lerp_hex(lower_border, upper_border, amount),
                    self._lerp_hex(lower_bg, upper_bg, amount),
                )
        return stops[-1][1], stops[-1][2]

    def _apply_dpf_temperature_style(self, widget: Static, temp_c: Any) -> None:
        """Gently ease the DPF edge/tint through its thermal palette.

        The numerical temperature and DPF status remain immediate. Only the
        decorative edge lighting is interpolated, just like the visual bars.
        """
        target = float(temp_c) if isinstance(temp_c, (int, float)) else None
        if target is None:
            visual = self._dpf_visual_temp
        elif self._dpf_visual_temp is None:
            visual = target
        else:
            visual = self._dpf_visual_temp + ((target - self._dpf_visual_temp) * self._dpf_thermal_alpha)
        self._dpf_visual_temp = visual
        border_colour, background_colour = self._dpf_colours(visual)
        # DPF red means genuinely hot, not merely normal burning. Only the
        # red end of its own thermal scale gets the same quiet breathing edge.
        if isinstance(visual, (int, float)) and visual >= 500:
            border_colour = self._pulse_colour("#852026", "#ff645b", period=2.2)
        widget.styles.border = ("round", Color.parse(border_colour))
        widget.styles.background = Color.parse(background_colour)

    def _update_score_border_titles(self, s: TelemetrySnapshot) -> None:
        """Put the live Efficiency/Mood verdicts into their card borders."""
        efficiency_key = efficiency_state(
            s.efficiencyScore,
            driving_state=s.drivingState,
            thermal_state=s.thermalState,
            dpf_status=s.dpfStatus,
            battery_v=s.batteryV,
        )
        mood_key = mood_state(
            s.moodScore,
            driving_state=s.drivingState,
            thermal_state=s.thermalState,
            obd_connection=s.obdConnection,
            battery_v=s.batteryV,
        )
        _face, mood_label = self.ui_text.mood(mood_key)
        self.query_one("#efficiency_card", DitherCard).border_title = f" {self.ui_text.efficiency_label(efficiency_key)} "
        self.query_one("#mood_card", DitherCard).border_title = f" {mood_label} "

    def _feedback(self, s: TelemetrySnapshot) -> tuple[str, str]:
        """Choose the one driver-facing cue from the shared interpretation.

        This deliberately does not re-diagnose RPM/pedal locally. The engine
        publishes drive state and guidance reason once; UI and LEDs merely
        explain/render it rather than starting a second argument.
        """
        obd = (s.obdConnection or "").lower()
        thermal = (s.thermalState or "").lower()
        dpf = (s.dpfStatus or "").lower()
        guidance = (s.guidanceReason or "").lower()
        drive = (s.drivingState or "").lower()
        batt = float(s.batteryV) if isinstance(s.batteryV, (int, float)) else None

        if any(word in obd for word in ("disconnect", "failed", "missing", "offline", "no obd", "error")):
            return "obd_wait", "alert"
        if not s.telemetryValid:
            return "telemetry_incomplete", "caution"
        if batt is not None and batt < 11.8:
            return "low_voltage", "alert"
        if "hot" in thermal:
            return "thermal_hot", "alert"
        if "pressure" in dpf:
            return "dpf_pressure", "alert"
        if "lugging" in drive or guidance in {"low_rpm_high_demand", "low_rpm_demand", "reverse_low_rpm_load"}:
            return "lugging", "caution"
        if "cold" in thermal:
            return "cold", "caution"
        if guidance in {"high_rpm_low_gear", "high_rpm_low_load", "engine_braking"}:
            return "high_rpm", "caution"
        if guidance == "coasting_ok":
            return "coasting", "good"
        if (isinstance(s.efficiencyScore, (int, float)) and s.efficiencyScore >= 78 and
                isinstance(s.moodScore, (int, float)) and s.moodScore >= 72):
            return "happy_path", "good"
        return "default", "good"

    def _apply_feedback_style(self, widget: Static, s: TelemetrySnapshot) -> None:
        _message, severity = self._feedback(s)
        active = f"feedback-{severity}"
        for css_class in self._feedback_classes:
            widget.set_class(css_class == active, css_class)

    def render_feedback(self, s: TelemetrySnapshot) -> RenderableType:
        key, severity = self._feedback(s)
        message = self.ui_text.tip(key)
        style = {
            "good": f"bold {CYAN}",
            "caution": "bold yellow",
            "alert": "bold red",
        }.get(severity, "bold white")
        return Align.center(Text(message, style=style))

    def render_status(self, s: TelemetrySnapshot) -> RenderableType:
        text = Text()
        text.append(f"{fmt(s.batteryV, 1)}V", style="bold cyan")
        if isinstance(s.airCPress_bar, (int, float)):
            text.append(" | ", style="dim")
            text.append("A/C ", style="dim")
            text.append(f"{fmt_sig(s.airCPress_bar)}bar", style="bold white")
            if isinstance(s.airCPressSessionMin_bar, (int, float)) and isinstance(s.airCPressSessionMax_bar, (int, float)):
                text.append(" ", style="dim")
                text.append(f"{fmt_sig(s.airCPressSessionMin_bar)}–{fmt_sig(s.airCPressSessionMax_bar)}", style="bold cyan")
        text.append(" | ", style="dim")
        text.append(self._obd_label(s), style=f"bold {self._obd_status_colour(s)}")
        if self.show_frame_counter:
            text.append(" | ", style="dim")
            text.append(f"#{s.sample}", style="dim")
        return text

    def render_dpf(self, s: TelemetrySnapshot) -> RenderableType:
        # One-line footer. It is deliberately compact rather than a marquee: the
        # driver gets a stable emissions summary without text wandering about.
        text = Text()
        text.append("DPF ", style="bold cyan")
        text.append(f"{fmt(s.dpfSoot, 3)}g ", style="bold white")
        text.append(f"{s.dpfStatus}{s.dpfTrendArrow}", style=state_style(s.dpfStatus))
        text.append(" | Δ ", style="dim")
        text.append(f"{fmt_pressure(s.dpfDiffProxy)}mbar", style="bold white")
        text.append(" | ", style="dim")
        text.append(f"{fmt(s.fapTemp, 0)}°C", style="bold white")
        text.append(" | Regen in ", style="dim")
        text.append(self._regen_remaining_text(s), style="bold white")
        text.append(" | Eolys ", style="dim")
        if isinstance(s.fapAdditivePercent, (int, float)):
            text.append(f"~{s.fapAdditivePercent:.0f}%", style="bold cyan")
        elif isinstance(s.fapAdditiveRemain, (int, float)):
            text.append(f"{s.fapAdditiveRemain:.0f}mL", style="bold cyan")
        else:
            text.append("--", style="dim")
        if isinstance(s.fapLifeLeft_mi, (int, float)):
            text.append(" (", style="dim")
            text.append(self._miles_compact(s.fapLifeLeft_mi), style="bold white")
            text.append(")", style="dim")
        return text

    def _three_line_hero(
        self,
        art_lines: list[str],
        *,
        state_key: str,
        bar_key: str,
        score: Any,
        track_colour: str,
    ) -> Table:
        """Three-line badge/blob beside a single prominent main score rail.

        The primary rail is left-aligned within its own column so enlarging it
        grows into the free space on the right, never back over the badge.
        """
        hero = Table.grid(expand=True, padding=0)
        hero.add_column(width=8, justify="center", no_wrap=True, overflow="crop")
        hero.add_column(ratio=1, no_wrap=True, overflow="crop")

        art = Text(
            "\n".join(art_lines[:3] + [""] * max(0, 3 - len(art_lines))),
            style=state_style(state_key),
        )
        hero.add_row(
            Align.center(art, vertical="middle"),
            Align.left(
                self._hero_score_bar(
                    bar_key,
                    score,
                    width=26,
                    track_colour=track_colour,
                ),
                vertical="middle",
                height=3,
            ),
        )
        return hero

    def _compact_score_pairs(
        self,
        pairs: list[tuple[str, str, Any]],
        *,
        width: int = 8,
    ) -> Table:
        """Two compact sub-score groups per line beneath the main bar."""
        table = Table.grid(expand=True)
        table.add_column(width=8, style="dim", no_wrap=True, overflow="crop")
        table.add_column(width=width, no_wrap=True, overflow="crop")
        table.add_column(width=2)
        table.add_column(width=9, style="dim", no_wrap=True, overflow="crop")
        table.add_column(width=width, no_wrap=True, overflow="crop")
        # Pairs are intentionally arranged 2 per line so they read as
        # subordinate diagnostics rather than four competing headline bars.
        for left, right in ((pairs[0], pairs[1]), (pairs[2], pairs[3])):
            left_label, left_key, left_value = left
            right_label, right_key, right_value = right
            table.add_row(
                Text(left_label, style="dim #75818c"),
                self._score_bar(left_key, left_value, width=width, muted=True),
                "",
                Text(right_label, style="dim #75818c"),
                self._score_bar(right_key, right_value, width=width, muted=True),
            )
        return table

    def render_efficiency(self, s: TelemetrySnapshot) -> RenderableType:
        state_key = efficiency_state(
            s.efficiencyScore,
            driving_state=s.drivingState,
            thermal_state=s.thermalState,
            dpf_status=s.dpfStatus,
            battery_v=s.batteryV,
        )
        table = Table.grid(expand=True, padding=0)
        # The status badge itself contains the human-facing state wording.
        # Avoid duplicating it beside the bar; the score card is already doing
        # enough work without shouting the same verdict twice.
        table.add_row(self._three_line_hero(
            self.ui_text.efficiency_badge(state_key),
            state_key=state_key,
            bar_key="efficiencyScore",
            score=s.efficiencyScore,
            track_colour="#0b2b37",
        ))
        table.add_row(self._compact_score_pairs([
            ("Zone", "effOperatingZone", s.effOperatingZone),
            ("Load", "effLoad", s.effLoad),
            ("Thermal", "effThermal", s.effThermal),
            ("Flow", "effFlow", s.effFlow),
        ]))
        return table

    def render_mood(self, s: TelemetrySnapshot) -> RenderableType:
        state_key = mood_state(
            s.moodScore,
            driving_state=s.drivingState,
            thermal_state=s.thermalState,
            obd_connection=s.obdConnection,
            battery_v=s.batteryV,
        )
        table = Table.grid(expand=True, padding=0)
        table.add_row(self._three_line_hero(
            self.ui_text.mood_art(state_key),
            state_key=state_key,
            bar_key="moodScore",
            score=s.moodScore,
            track_colour="#300f35",
        ))
        table.add_row(self._compact_score_pairs([
            ("Thermal", "moodThermalComfort", s.moodThermalComfort),
            ("Strain", "moodStrain", s.moodStrain),
            ("Delivery", "moodDelivery", s.moodDelivery),
            ("Electrical", "moodElectrical", s.moodElectrical),
        ]))
        return table

    def render_driver(self, s: TelemetrySnapshot) -> RenderableType:
        """Driver inputs with bars only on RPM and speed.

        Gear and acceleration deliberately span the *entire* right-hand area
        that RPM/Speed normally divide into value + bar columns. They are
        centred in that merged space, not parked in the normal value column.
        """
        if isinstance(s.relativeAccelSessionMin_mps2, (int, float)) and isinstance(s.relativeAccelSessionMax_mps2, (int, float)):
            accel_range = f"{s.relativeAccelSessionMin_mps2:+.1f}/{s.relativeAccelSessionMax_mps2:+.1f}"
        else:
            accel_range = "--/--"

        def live_row(value: str, bar: RenderableType) -> Table:
            row = Table.grid(expand=True)
            row.add_column(width=10, justify="right", style="bold white", no_wrap=True, overflow="crop")
            row.add_column(ratio=1, no_wrap=True, overflow="crop")
            row.add_row(value, bar)
            return row

        table = Table.grid(expand=True)
        table.add_column(width=13, style="dim", no_wrap=True, overflow="crop")
        # This is intentionally one *merged* content area. RPM/Speed create
        # their own internal value+bar columns; Gear/Acceleration use all of it.
        table.add_column(ratio=1, no_wrap=True, overflow="crop")

        table.add_row(
            "RPM",
            live_row(
                fmt_sig(s.rpm),
                bar_with_trend(
                    self._metric_bar("rpm", s.rpm, lo=800, hi=4000, kind="rpm", width=14),
                    self.trends.arrow("rpm"),
                ),
            ),
        )
        table.add_row(
            "Speed",
            live_row(
                fmt_sig(s.speed_mph),
                bar_with_trend(
                    self._metric_bar("speed_mph", s.speed_mph, lo=0, hi=80, kind="speed", width=14),
                    self.trends.arrow("speed_mph"),
                ),
            ),
        )
        table.add_row("Gear", Align.center(Text(str(s.gear or "--"), style="bold white")))
        table.add_row("Acceleration", Align.center(Text(accel_range, style="bold white")))
        return table

    def render_pressure(self, s: TelemetrySnapshot) -> RenderableType:
        table = Table.grid(expand=True)
        table.add_row(metric_table(
            ("Rail", value_delta_text(fmt_pressure(s.railProxy), s.railErrorProxy, delta_kind="rail_delta"), bar_with_trend(self._metric_bar("railProxy", s.railProxy, lo=200, hi=1700, kind="rail", width=14), self.trends.arrow("railProxy"))),
            ("Boost", value_delta_text(fmt_pressure(s.boostProxy), s.boostErrorProxy, delta_kind="boost_delta"), bar_with_trend(self._metric_bar("boostProxy", s.boostProxy, lo=0, hi=1500, kind="boost", width=14), self.trends.arrow("boostProxy"))),
            ("MAP", fmt_pressure(s.mapProxy), bar_with_trend(self._metric_bar("mapProxy", s.mapProxy, lo=900, hi=2600, kind="map", width=14), self.trends.arrow("mapProxy"))),
            label_width=7, value_width=14, bar_width=18,
        ))
        return table

    def render_flow_exhaust(self, s: TelemetrySnapshot) -> RenderableType:
        table = Table.grid(expand=True)
        table.add_row(metric_table(
            ("Air", value_delta_text(fmt_sig(s.airFlowMeasured), s.airFlowError, delta_kind="air_delta"), bar_with_trend(self._metric_bar("airFlowMeasured", s.airFlowMeasured, lo=100, hi=900, kind="airflow", width=14), self.trends.arrow("airFlowMeasured"))),
            ("EGR", value_delta_percent_text(f"{fmt_sig(s.egrActual)}%", s.egrError, delta_kind="egr_delta"), bar_with_trend(self._metric_bar("egrActual", s.egrActual, lo=0, hi=100, kind="egr", width=14), self.trends.arrow("egrActual"))),
            ("AirMix", value_delta_percent_text(f"{fmt_sig(s.airMixerActual)}%", s.airMixerError, delta_kind="airmixer_delta"), bar_with_trend(self._metric_bar("airMixerActual", s.airMixerActual, lo=0, hi=100, kind="airmixer", width=14), self.trends.arrow("airMixerActual"))),
            label_width=7, value_width=14, bar_width=18,
        ))
        return table

    def render_thermal_system(self, s: TelemetrySnapshot) -> RenderableType:
        table = Table.grid(expand=True)
        table.add_row(metric_table(
            ("Cool", f"{fmt_sig(s.coolant)}°", bar_with_trend(self._metric_bar("coolant", s.coolant, lo=40, hi=110, kind="coolant", width=18), self.trends.arrow("coolant"))),
            ("Fuel", f"{fmt_sig(s.fuelTemp)}°", bar_with_trend(self._metric_bar("fuelTemp", s.fuelTemp, lo=0, hi=80, kind="fuel_temp", width=18), self.trends.arrow("fuelTemp"))),
            ("Intake", f"{fmt_sig(s.intakeTemp)}°", bar_with_trend(self._metric_bar("intakeTemp", s.intakeTemp, lo=0, hi=80, kind="intake_temp", width=18), self.trends.arrow("intakeTemp"))),
            ("Ambient", f"{fmt_sig(s.ambientTemp)}°", bar_with_trend(self._metric_bar("ambientTemp", s.ambientTemp, lo=-10, hi=40, kind="ambient_temp", width=18), self.trends.arrow("ambientTemp"))),
            label_width=8, value_width=8, bar_width=22,
        ))
        return table

    def render_debug(self, s: TelemetrySnapshot, environment: EnvironmentSnapshot) -> RenderableType:
        table = Table.grid(expand=True)
        table.add_column(ratio=1, style="dim")
        table.add_column(ratio=3)
        fields: list[tuple[str, Any]] = [
            ("Keys", "q quit | d debug | [ dim | ] brighten"),
            ("Source", self.state_store.latest().source_name),
            ("Adapter", s.adapterState),
            ("Protocol", s.protocol),
            ("Poll", f"{s.pollHealth} @ {fmt(s.sampleRateHz, 2)} Hz"),
            ("Timestamp", s.timestamp),
            ("Session", f"{s.sessionId or '--'} | boot {s.bootId[:8] if s.bootId else '--'}"),
            ("Score", f"{fmt(s.scoreConfidence, 0)}% | {s.scoreReason}"),
            ("Drive", f"{s.drivingState} | {signed(s.guidanceCorrection, 2)} {s.guidanceReason}"),
            (
                "Light",
                f"raw {fmt(environment.ambient_lux_raw, 1)} lx | "
                f"filtered {fmt(environment.ambient_lux_filtered, 1)} lx | "
                f"{environment.light_state} "
                f"(i2c-{environment.sensor_bus if environment.sensor_bus is not None else '--'} "
                f"{f'0x{environment.sensor_address:02X}' if environment.sensor_address is not None else '--'})"
                f"{f' | {environment.sensor_error}' if environment.sensor_error else ''}",
            ),
            ("A/C", f"{fmt_sig(s.airCPress_bar)} bar | session {fmt_sig(s.airCPressSessionMin_bar)}–{fmt_sig(s.airCPressSessionMax_bar)}"),
            ("Boost", f"{fmt_pressure(s.boostProxy)} target {fmt_pressure(s.boostTargetProxy)} delta {signed_pressure(s.boostErrorProxy)}"),
            ("Rail", f"{fmt_pressure(s.railProxy)} target {fmt_pressure(s.railTargetProxy)} delta {signed_pressure(s.railErrorProxy)}"),
            ("Air", f"{fmt(s.airFlowMeasured)}/{fmt(s.airFlowSetting)} err {signed(s.airFlowError)}"),
            ("EGR", f"{fmt(s.egrActual)}/{fmt(s.egrTarget)} err {signed(s.egrError)}"),
            ("Mixer", f"{fmt(s.airMixerActual)}/{fmt(s.airMixerTarget)} err {signed(s.airMixerError)}"),
            ("FAP", f"{fmt(s.dpfSoot, 3)}g {s.dpfStatus}{s.dpfTrendArrow} temp {fmt(s.fapTemp)}°C diff {fmt_pressure(s.dpfDiffProxy)}"),
            ("Regen", f"last {fmt(s.lastRegen_mi)} mi | avg {fmt(s.avg10Regen_mi)} mi | life {fmt(s.fapLifeLeft_mi)} mi"),
            ("Additive", f"used {fmt(s.fapAdditiveVol)} mL | remain {fmt(s.fapAdditiveRemain)} mL | est {fmt(s.fapAdditivePercent)}%"),
            ("Inj corr", f"{signed(s.inj1FlowCorr,2)} {signed(s.inj2FlowCorr,2)} {signed(s.inj3FlowCorr,2)} {signed(s.inj4FlowCorr,2)}"),
        ]
        for label, value in fields:
            table.add_row(label, str(value))
        return table
