from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Final

from .blinkstick_rgbw import BlinkStickProRgbw, RGBW

from .brightness_policy import BrightnessPolicy
from .runtime_types import DashboardState
from .telemetry import TelemetrySnapshot


LED_COUNT: Final[int] = 24
LED_FPS: Final[float] = 50.0
DEFAULT_MAX_MOOD_WIDTH_FRACTION: Final[float] = 0.50
MIN_MOOD_WIDTH_PIXELS: Final[float] = 3.0
USB_LED_BUDGET_MA: Final[float] = 300.0
ESTIMATED_RGBW_PIXEL_MAX_MA: Final[float] = 50.0
RECONNECT_DELAY_S: Final[float] = 5.0
MISSING_DEVICE_NOTICE_INTERVAL_S: Final[float] = 60.0
FULL_FRAME_RESYNC_INTERVAL_S: Final[float] = 5.0
CONNECT_CLEAR_FRAMES: Final[int] = 2
CONNECT_CLEAR_GAP_S: Final[float] = 0.006

# Output-only smoothing. Telemetry and score/state changes remain immediate.
NORMAL_TRANSITION_TAU_S: Final[float] = 0.22
REGEN_TRANSITION_TAU_S: Final[float] = 0.10
BRIGHTNESS_TRANSITION_TAU_S: Final[float] = 0.30

BLACK: Final[RGBW] = (0, 0, 0, 0)
WHITE: Final[RGBW] = (12, 28, 44, 220)
CYAN: Final[RGBW] = (0, 165, 255, 0)
FUCHSIA: Final[RGBW] = (255, 0, 135, 0)
RED: Final[RGBW] = (255, 0, 0, 0)
DIM_CYAN: Final[RGBW] = (0, 45, 75, 0)
DIM_RED: Final[RGBW] = (85, 0, 0, 0)
AMBER: Final[RGBW] = (255, 92, 0, 0)

Frame = tuple[RGBW, ...]
FloatPixel = tuple[float, float, float, float]
FloatFrame = tuple[FloatPixel, ...]

# One direct economy gradient: white -> cyan -> fuchsia -> red.
# The renderer does no RPM/load inference. It receives the score already made
# meaningful by the shared telemetry engine.
EFFICIENCY_PALETTE: Final[tuple[tuple[float, RGBW], ...]] = (
    (0.0, RED),
    (35.0, RED),
    (55.0, FUCHSIA),
    (76.0, CYAN),
    (90.0, (0, 115, 175, 90)),
    (100.0, WHITE),
)

# Saturated focal colours. Peak economy has a vivid cyan marker against its
# white band so the bar still has a readable centre.
MARKER_PALETTE: Final[tuple[tuple[float, RGBW], ...]] = (
    (0.0, RED),
    (45.0, (255, 0, 95, 0)),
    (60.0, (255, 0, 190, 0)),
    (82.0, (0, 215, 255, 0)),
    (100.0, (0, 225, 255, 0)),
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _scale(colour: RGBW | FloatPixel, amount: float) -> RGBW:
    amount = _clamp(amount)
    return tuple(max(0, min(255, int(round(channel * amount)))) for channel in colour)  # type: ignore[return-value]


def _add(first: RGBW, second: RGBW) -> RGBW:
    return tuple(min(255, a + b) for a, b in zip(first, second))  # type: ignore[return-value]


def _mix(first: RGBW | FloatPixel, second: RGBW | FloatPixel, amount: float) -> RGBW:
    amount = _clamp(amount)
    return tuple(int(round(a + ((b - a) * amount))) for a, b in zip(first, second))  # type: ignore[return-value]


def _lerp_float(first: FloatPixel, second: RGBW | FloatPixel, amount: float) -> FloatPixel:
    amount = _clamp(amount)
    return tuple(a + ((float(b) - a) * amount) for a, b in zip(first, second))  # type: ignore[return-value]


def _frame_to_float(frame: Frame) -> FloatFrame:
    return tuple(tuple(float(channel) for channel in pixel) for pixel in frame)  # type: ignore[return-value]


def _ema_alpha(delta_s: float, tau_s: float) -> float:
    if delta_s <= 0.0:
        return 0.0
    return 1.0 - math.exp(-delta_s / max(0.001, tau_s))


def _palette_colour(score: float, palette: tuple[tuple[float, RGBW], ...]) -> RGBW:
    score = _clamp(score, 0.0, 100.0)
    low_score, low_colour = palette[0]
    for high_score, high_colour in palette[1:]:
        if score <= high_score:
            portion = (score - low_score) / max(0.001, high_score - low_score)
            return _mix(low_colour, high_colour, portion)
        low_score, low_colour = high_score, high_colour
    return palette[-1][1]


@dataclass(frozen=True)
class LedRender:
    """One complete physical target decision, separate from transport."""

    frame: Frame
    brightness: float
    mode: str
    band_width: float = 0.0
    guidance_position: float | None = None


class LedFrameMapper:
    """Map already-interpreted FoxDash outputs into a physical RGBW target.

    Contract:
    - brightness = ambient light only;
    - colour = efficiency score;
    - width = mood score / mechanical comfort;
    - position + marker = signed upstream guidance correction;
    - DPF burning = the one whole-bar visual override.

    This layer must stay deliberately dumb. It never turns RPM, pedal, reverse,
    or load into new opinions. That work belongs in ``TelemetryEngine``.
    """

    def __init__(
        self,
        *,
        led_count: int = LED_COUNT,
        reverse: bool = False,
        max_mood_width_fraction: float = DEFAULT_MAX_MOOD_WIDTH_FRACTION,
        use_ambient_brightness: bool = False,
    ) -> None:
        if led_count <= 0:
            raise ValueError("led_count must be positive")
        if not 0.05 <= max_mood_width_fraction <= 1.0:
            raise ValueError("max_mood_width_fraction must be between 0.05 and 1.0")
        self.led_count = int(led_count)
        self.reverse = bool(reverse)
        self.max_mood_width_fraction = float(max_mood_width_fraction)
        # Keep the existing fixed fallback brightness while we collect real
        # cabin-lux history. Ambient control is opt-in only after calibration.
        self.use_ambient_brightness = bool(use_ambient_brightness)
        self._brightness_policy = BrightnessPolicy()

    def render(self, state: DashboardState, *, now: float) -> LedRender:
        ambient_lux = state.environment.ambient_lux_filtered if self.use_ambient_brightness else None
        levels = self._brightness_policy.resolve(ambient_lux)
        brightness = _clamp(levels.led_percent / 100.0)
        if state.sequence <= 0:
            return LedRender(self._blank(), brightness, mode="off")

        telemetry = state.telemetry
        if self._is_regen(telemetry):
            return LedRender(self._regen_frame(now), brightness, mode="regen")
        if not telemetry.telemetryValid:
            return LedRender(self._status_frame(telemetry), brightness, mode="status")

        frame, width, guidance = self._normal_frame(telemetry)
        return LedRender(frame, brightness, mode="normal", band_width=width, guidance_position=guidance)

    def _blank(self) -> Frame:
        return (BLACK,) * self.led_count

    @staticmethod
    def _is_regen(telemetry: TelemetrySnapshot) -> bool:
        status = (telemetry.dpfStatus or "").upper()
        return "BURNING" in status or "REGEN" in status

    def _status_frame(self, telemetry: TelemetrySnapshot) -> Frame:
        """A subdued incomplete/no-OBD indication, not fake score colours."""
        connection = (telemetry.obdConnection or "").lower()
        colour = DIM_RED if any(word in connection for word in ("reconnect", "lost", "error", "missing", "offline")) else DIM_CYAN
        frame = [BLACK] * self.led_count
        centre = (self.led_count - 1) / 2.0
        for index in range(self.led_count):
            coverage = _clamp(1.0 - abs(index - centre) / 1.2)
            if coverage:
                frame[index] = _scale(colour, coverage)
        if self.reverse:
            frame.reverse()
        return tuple(frame)

    def _normal_frame(self, telemetry: TelemetrySnapshot) -> tuple[Frame, float, float]:
        efficiency = _number(telemetry.efficiencyScore)
        mood = _number(telemetry.moodScore)
        guidance = _number(telemetry.guidanceCorrection)
        if efficiency is None or mood is None or guidance is None:
            return self._status_frame(telemetry), 0.0, (self.led_count - 1) / 2.0

        max_width = max(MIN_MOOD_WIDTH_PIXELS, self.led_count * self.max_mood_width_fraction)
        min_width = min(MIN_MOOD_WIDTH_PIXELS, max_width)
        width = min_width + (_clamp(mood / 100.0) * (max_width - min_width))

        # Guidance is signed upstream: -1 means less engine speed, +1 means more.
        centre = ((1.0 + _clamp(guidance, -1.0, 1.0)) * 0.5) * (self.led_count - 1)
        half_width = max(0.75, width / 2.0)
        # Preserve the whole relaxed/picky window within physical LEDs.
        centre = _clamp(centre, half_width - 0.5, (self.led_count - 1) - (half_width - 0.5))

        band_colour = _palette_colour(efficiency, EFFICIENCY_PALETTE)
        marker_colour = _palette_colour(efficiency, MARKER_PALETTE)
        frame: list[RGBW] = [BLACK] * self.led_count

        for index in range(self.led_count):
            distance = abs(index - centre)
            # Soft bell/triangular hybrid. Centre stays visible, but every edge
            # falls away continuously through the diffuser instead of making a
            # static block.
            falloff = _clamp(1.0 - distance / (half_width + 0.45))
            if falloff <= 0.0:
                continue
            body_energy = 0.10 + (0.52 * (falloff ** 1.35))
            pixel = _scale(band_colour, body_energy)

            # The marker is intentionally bolder and more saturated than the
            # band. It crossfades across about two physical LEDs as it moves.
            marker = _clamp(1.0 - distance / 1.05)
            if marker > 0.0:
                pixel = _add(pixel, _scale(marker_colour, 0.82 * marker))
            frame[index] = pixel

        if self.reverse:
            frame.reverse()
            centre = (self.led_count - 1) - centre
        return tuple(frame), width, centre

    def _regen_frame(self, now: float) -> Frame:
        """Whole-bar orange flame with a gentle breath and local flicker."""
        breath = 0.54 + (0.34 * ((math.sin(now * math.tau / 2.8) + 1.0) * 0.5))
        frame: list[RGBW] = []
        for index in range(self.led_count):
            flicker = 0.72 + 0.19 * math.sin((now * 8.7) + index * 1.71) + 0.09 * math.sin((now * 15.1) - index * 2.37)
            energy = _clamp(breath * flicker, 0.22, 1.0)
            flame = _scale(AMBER, energy)
            if energy > 0.72:
                flame = _add(flame, _scale((85, 55, 0, 0), (energy - 0.72) / 0.28))
            frame.append(flame)
        if self.reverse:
            frame.reverse()
        return tuple(frame)


class LedApp:
    """Live BlinkStick consumer, isolated from telemetry and UI threads."""

    def __init__(
        self,
        state_store,
        *,
        led_count: int = LED_COUNT,
        reverse: bool = False,
        max_mood_width_fraction: float = DEFAULT_MAX_MOOD_WIDTH_FRACTION,
        use_ambient_brightness: bool = False,
    ) -> None:
        self._store = state_store
        self._mapper = LedFrameMapper(
            led_count=led_count,
            reverse=reverse,
            max_mood_width_fraction=max_mood_width_fraction,
            use_ambient_brightness=use_ambient_brightness,
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._transport: BlinkStickProRgbw | None = None
        self._next_connect_at = 0.0
        self._next_missing_device_notice_at = 0.0
        self._next_full_resync_at = 0.0
        self._needs_connect_clear = False
        self._current_frame: FloatFrame | None = None
        self._current_brightness = 0.0
        self._last_tick_at: float | None = None
        self._last_sent: tuple[Frame, float] | None = None
        self._previous_mode = "off"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="foxdash-led", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        frame_period = 1.0 / LED_FPS
        while not self._stop.wait(frame_period):
            now = time.monotonic()
            delta_s = frame_period if self._last_tick_at is None else min(0.25, max(0.0, now - self._last_tick_at))
            self._last_tick_at = now
            target = self._mapper.render(self._store.latest(), now=now)
            frame, brightness = self._animate_toward(target, delta_s)

            if not self._ensure_connected(now):
                continue
            try:
                assert self._transport is not None
                if self._needs_connect_clear:
                    self._flush_after_connect()
                    self._needs_connect_clear = False
                    self._last_sent = None

                output = (frame, round(brightness, 5))
                periodic_resync = now >= self._next_full_resync_at
                if output == self._last_sent and target.mode not in {"regen"} and not periodic_resync:
                    continue

                self._transport.set_brightness(brightness)
                self._transport.set_frame(frame)
                self._transport.show()
                self._last_sent = output
                self._next_full_resync_at = now + FULL_FRAME_RESYNC_INTERVAL_S
            except Exception as exc:
                self._drop_transport(f"output failed: {type(exc).__name__}: {exc}", now)

    def _flush_after_connect(self) -> None:
        """Clear a stale partially latched strip after a fresh USB connection."""
        assert self._transport is not None
        for index in range(CONNECT_CLEAR_FRAMES):
            self._transport.off()
            if index + 1 < CONNECT_CLEAR_FRAMES:
                time.sleep(CONNECT_CLEAR_GAP_S)

    def _animate_toward(self, target: LedRender, delta_s: float) -> tuple[Frame, float]:
        if self._current_frame is None:
            self._current_frame = _frame_to_float((BLACK,) * self._mapper.led_count)
            self._current_brightness = 0.0
        tau = REGEN_TRANSITION_TAU_S if target.mode == "regen" or self._previous_mode == "regen" else NORMAL_TRANSITION_TAU_S
        frame_alpha = _ema_alpha(delta_s, tau)
        brightness_alpha = _ema_alpha(delta_s, BRIGHTNESS_TRANSITION_TAU_S)
        self._current_frame = tuple(_lerp_float(current, desired, frame_alpha) for current, desired in zip(self._current_frame, target.frame))
        self._current_brightness += (target.brightness - self._current_brightness) * brightness_alpha
        self._previous_mode = target.mode
        frame = tuple(tuple(max(0, min(255, int(round(channel)))) for channel in pixel) for pixel in self._current_frame)
        return frame, _clamp(self._current_brightness)

    def _ensure_connected(self, now: float) -> bool:
        if self._transport is not None:
            return True
        if now < self._next_connect_at:
            return False
        try:
            transport = BlinkStickProRgbw(
                self._mapper.led_count,
                fps=LED_FPS,
                usb_ma_budget=USB_LED_BUDGET_MA,
                estimated_pixel_max_ma=ESTIMATED_RGBW_PIXEL_MAX_MA,
            )
            transport.connect()
            self._transport = transport
            self._last_sent = None
            self._needs_connect_clear = True
            self._next_full_resync_at = now
            self._next_missing_device_notice_at = 0.0
            print(f"[FoxDash LED] online: {self._mapper.led_count} RGBW pixels, USB safety cap {transport.usb_limit * 100:.1f}%, output {LED_FPS:.0f} Hz.")
            return True
        except Exception as exc:
            self._next_connect_at = now + RECONNECT_DELAY_S
            if now >= self._next_missing_device_notice_at:
                print(f"[FoxDash LED] unavailable; dashboard continues and will retry every {RECONNECT_DELAY_S:.0f}s: {type(exc).__name__}: {exc}")
                self._next_missing_device_notice_at = now + MISSING_DEVICE_NOTICE_INTERVAL_S
            return False

    def _drop_transport(self, reason: str, now: float) -> None:
        print(f"[FoxDash LED] {reason}; reconnecting in {RECONNECT_DELAY_S:.0f}s.")
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
        self._transport = None
        self._last_sent = None
        self._needs_connect_clear = False
        self._next_full_resync_at = 0.0
        self._next_connect_at = now + RECONNECT_DELAY_S

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
