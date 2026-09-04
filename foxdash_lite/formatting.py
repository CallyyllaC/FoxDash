from __future__ import annotations

import math
from typing import Any

from rich.table import Table
from rich.text import Text


CYAN = "#00d7ff"
FUCHSIA = "#d100ff"
AMBER = "yellow"
RED = "red"
WHITE = "white"
GREEN = "green"
DIM = "dim"
GREY = "#5f6470"

UP = "↑"
DOWN = "↓"
FLAT = "→"


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not (isinstance(value, float) and math.isnan(value))


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def fmt(value: Any, decimals: int = 0, missing: str = "--") -> str:
    if value is None or value == "":
        return missing
    if isinstance(value, float):
        if math.isnan(value):
            return missing
        return f"{value:.{decimals}f}"
    return str(value)


def fmt_sig(value: Any, sig: int = 3, missing: str = "--") -> str:
    """Fixed-point significant-figure formatter for cramped dashboard cells."""
    if not is_number(value):
        return missing
    v = float(value)
    if v == 0:
        return "0"

    sign = "-" if v < 0 else ""
    av = abs(v)
    places = sig - int(math.floor(math.log10(av))) - 1
    rounded = round(av, places)

    if places > 0:
        s = f"{rounded:.{places}f}".rstrip("0").rstrip(".")
    else:
        s = f"{rounded:.0f}"
    return sign + s


def fmt_pressure(value: Any, missing: str = "--") -> str:
    """Compact pressure formatter.

    mbar-ish values below 1000 stay as normal values. 1000+ becomes k-format,
    e.g. 1050 -> 1.05k. The unit is implied by the label/panel because the
    screen is tiny and not interested in typography essays.
    """
    if not is_number(value):
        return missing
    v = float(value)
    av = abs(v)
    if av >= 1000.0:
        return f"{v / 1000.0:.2f}".rstrip("0").rstrip(".") + "k"
    if av >= 100.0:
        return f"{v:.0f}"
    if av >= 10.0:
        return f"{v:.1f}".rstrip("0").rstrip(".")
    return f"{v:.2f}".rstrip("0").rstrip(".")


def signed(value: Any, decimals: int = 0, missing: str = "--") -> str:
    if not is_number(value):
        return missing
    return f"{float(value):+.{decimals}f}"


def signed_sig(value: Any, sig: int = 3, missing: str = "--") -> str:
    if not is_number(value):
        return missing
    v = float(value)
    if v > 0:
        return "+" + fmt_sig(v, sig=sig, missing=missing)
    if v < 0:
        return fmt_sig(v, sig=sig, missing=missing)
    return "0"


def signed_pressure(value: Any, missing: str = "--") -> str:
    if not is_number(value):
        return missing
    v = float(value)
    if v > 0:
        return "+" + fmt_pressure(v, missing=missing)
    if v < 0:
        return "-" + fmt_pressure(abs(v), missing=missing)
    return "0"


def score(value: Any) -> str:
    if not is_number(value):
        return "--"
    return f"{int(round(float(value))):03d}"


def score_style(value: Any) -> str:
    if not is_number(value):
        return DIM
    v = float(value)
    if v >= 85:
        return f"bold {CYAN}"
    if v >= 70:
        return "bold white"
    if v >= 50:
        return "bold yellow"
    return "bold red"


def state_style(state: str) -> str:
    state = (state or "").lower()
    if any(word in state for word in ["pressure", "poor", "upset", "sulking", "hot", "fault", "no obd", "low volt", "low volts"]):
        return "bold red"
    if any(word in state for word in ["inefficient", "grumbling", "burning", "regen", "warming", "strained", "lugging"]):
        return "bold yellow"
    if any(word in state for word in ["optimal", "efficient", "content", "happy", "smug", "stable", "normal", "fine", "steady", "cruise"]):
        return f"bold {CYAN}"
    return "white"


def title_text(title: str, value: str | None = None, style: str = f"bold {CYAN}") -> Text:
    text = Text()
    text.append(title.upper(), style=style)
    if value is not None:
        text.append("  ", style=DIM)
        text.append(value, style="bold white")
    return text


def score_bar_colour(value: Any) -> str:
    """Return the semantic base hue for a 0--100 score."""
    if not is_number(value):
        return GREY
    v = float(value)
    if v >= 88:
        return CYAN
    if v >= 72:
        return WHITE
    if v >= 55:
        return AMBER
    return RED


def score_bar_style(value: Any) -> str:
    """Backward-compatible Rich style wrapper around ``score_bar_colour``."""
    return f"bold {score_bar_colour(value)}" if is_number(value) else DIM


# Every fill has three discrete tonal steps rather than a smooth rainbow.
# That gives the bars a richer dashboard look without pretending a terminal
# has anti-aliased pixels or making good scores look half-red on the left.
_STEP_TONES: dict[str, tuple[str, str, str]] = {
    CYAN: ("#05788f", "#00a9ca", "#53e5ff"),
    FUCHSIA: ("#78108a", "#b31cd0", "#f075ff"),
    WHITE: ("#6b7784", "#aebdcb", "#eefbff"),
    AMBER: ("#8f6400", "#c98d00", "#ffd36a"),
    RED: ("#81232b", "#ca3438", "#ff7566"),
    "yellow": ("#8f6400", "#c98d00", "#ffd36a"),
    "red": ("#81232b", "#ca3438", "#ff7566"),
    "white": ("#6b7784", "#aebdcb", "#eefbff"),
    "green": ("#2e7856", "#48ad7c", "#85e7b4"),
    GREY: ("#3b4652", "#566370", "#7b8895"),
}


def _base_colour(style_or_colour: str) -> str:
    """Extract a colour token from a simple Rich style string."""
    token = (style_or_colour or GREY).strip().split()[-1]
    return token if token in _STEP_TONES else GREY


def _stepped_fill(
    text: Text,
    *,
    filled: int,
    width: int,
    char: str,
    base_style: str,
    track_style: str,
    muted: bool = False,
) -> None:
    """Append a fill in three restrained tonal bands plus a soft track."""
    if filled > 0:
        tones = _STEP_TONES[_base_colour(base_style)]
        for index in range(filled):
            # The fill gets brighter toward its leading edge. It is a tonal
            # gradient inside the active semantic hue, not a traffic-light
            # rainbow painted across a single score.
            tone_index = min(2, (index * 3) // max(1, filled))
            style = tones[tone_index]
            if muted:
                style = f"dim {style}"
            text.append(char, style=style)
    if filled < width:
        text.append("─" * (width - filled), style=track_style)


def score_bar(
    value: Any,
    *,
    width: int = 10,
    show_value: bool = False,
    style_value: Any = None,
    muted: bool = False,
) -> Text:
    """Thin score bar for real 0-100 backend values.

    The score remains honest and linear. The styling now uses a tiny stepped
    tonal fill so the sub-bars look finished while staying subordinate to the
    primary verdict.
    """
    text = Text()
    track_style = "dim #263541" if muted else "dim #465360"
    if not is_number(value):
        text.append("─" * width, style=track_style)
        if show_value:
            text.append("  --", style=DIM)
        return text

    v = clamp(float(value), 0.0, 100.0)
    filled = int(round((v / 100.0) * width))
    filled = max(0, min(width, filled))
    colour_source = v if style_value is None else style_value
    _stepped_fill(
        text,
        filled=filled,
        width=width,
        char="━",
        base_style=score_bar_colour(colour_source),
        track_style=track_style,
        muted=muted,
    )
    if show_value:
        text.append(f"  {score(v)}", style=score_style(v))
    return text


def hero_score_bar(
    value: Any,
    *,
    width: int = 22,
    style_value: Any = None,
    track_colour: str = "#142a35",
) -> Text:
    """Single deliberate primary score rail for Efficiency and Mood.

    The whole width is painted as a very dark card-family track first, then
    the active portion is filled with stepped solid colour blocks. This gives
    the rail a stable silhouette while it animates instead of leaving a bright
    rectangle apparently floating in space.
    """
    text = Text()

    def append_background(count: int, colour: str) -> None:
        if count > 0:
            # Styled spaces are intentional: Rich/ANSI paints their cell
            # background, which gives a proper continuous rail in terminals
            # that do not offer CSS box shadows or a real canvas.
            text.append(" " * count, style=f"on {colour}")

    if not is_number(value):
        append_background(width, track_colour)
        return text

    v = clamp(float(value), 0.0, 100.0)
    filled = int(round((v / 100.0) * width))
    filled = max(0, min(width, filled))
    colour_source = v if style_value is None else style_value

    if filled > 0:
        tones = _STEP_TONES[_base_colour(score_bar_colour(colour_source))]
        for index in range(filled):
            tone_index = min(2, (index * 3) // max(1, filled))
            append_background(1, tones[tone_index])
    append_background(width - filled, track_colour)
    return text

def metric_state_style(kind: str, value: Any) -> str:
    """Colour for real-world values.

    This is intentionally context-aware. A high DPF delta is not the same as a
    high pedal position, because apparently reality refused to be one nice enum.
    """
    if not is_number(value):
        return DIM
    v = float(value)
    kind = (kind or "").lower()

    if kind in {"coolant", "engine_temp"}:
        if v < 55:
            return AMBER
        if v <= 100:
            return CYAN
        if v <= 105:
            return AMBER
        return RED
    if kind in {"fuel_temp", "intake_temp"}:
        if v <= 55:
            return CYAN
        if v <= 75:
            return AMBER
        return RED
    if kind == "ambient_temp":
        return CYAN
    if kind == "heat_soak":
        if abs(v) <= 18:
            return CYAN
        if abs(v) <= 30:
            return AMBER
        return RED
    if kind in {"dpf_diff", "fap_diff"}:
        if v <= 80:
            return CYAN
        if v <= 160:
            return AMBER
        return RED
    if kind == "battery":
        if 12.3 <= v <= 14.8:
            return CYAN
        if 11.8 <= v <= 15.0:
            return AMBER
        return RED
    if kind in {"air_delta", "egr_delta", "airmixer_delta"}:
        av = abs(v)
        if av <= 8:
            return CYAN
        if av <= 20:
            return FUCHSIA if v < 0 else AMBER
        return RED
    if kind == "boost_delta":
        av = abs(v)
        if av <= 100:
            return CYAN
        if av <= 220:
            return FUCHSIA if v < 0 else AMBER
        return RED
    if kind == "rail_delta":
        av = abs(v)
        if av <= 60:
            return CYAN
        if av <= 160:
            return FUCHSIA if v < 0 else AMBER
        return RED
    if kind in {"load", "pedal", "boost", "rail", "map", "rpm", "speed", "accel", "airflow", "egr", "airmixer", "ac_pressure"}:
        # These are intensity, not inherently bad. High values get attention,
        # but not instant judgement unless other metrics complain.
        return AMBER if v >= 75 else CYAN
    return CYAN


def range_bar(
    value: Any,
    *,
    lo: float,
    hi: float,
    width: int = 6,
    kind: str = "",
    invert: bool = False,
    style_value: Any = None,
    scale: str | None = None,
) -> Text:
    """Thin compact bar for real metrics.

    Values are honest; this only controls how much of the tiny terminal bar is
    filled. Pressure-ish values use a sqrt curve by default so low-but-useful
    readings don't vanish into one tragic pixel. The bar is a glance cue, not a
    measuring instrument pretending to be a ruler.
    """
    text = Text()
    if not is_number(value) or hi <= lo:
        text.append("─" * width, style=DIM)
        return text

    raw = float(value)
    t = clamp((raw - lo) / (hi - lo), 0.0, 1.0)
    kind_l = (kind or "").lower()
    if scale is None:
        if kind_l in {"boost", "rail", "map", "dpf_diff", "fap_diff", "boost_delta", "rail_delta"}:
            scale = "sqrt"
        else:
            scale = "linear"
    if scale == "sqrt":
        t = math.sqrt(t)
    elif scale == "log":
        # Mild log-ish compression; currently unused, but left here so we don't
        # reinvent the same tiny wheel when pressure ranges get tuned.
        t = math.log1p(9.0 * t) / math.log1p(9.0)
    if invert:
        t = 1.0 - t

    filled = int(round(t * width))
    filled = max(0, min(width, filled))
    colour_source = raw if style_value is None else style_value
    _stepped_fill(
        text,
        filled=filled,
        width=width,
        char="━",
        base_style=metric_state_style(kind, colour_source),
        track_style="dim #34404b",
    )
    return text


def bar_with_trend(bar: Text | str, arrow: str = "") -> Text:
    """Return a metric bar with a tiny amount of breathing room.

    Rendered shape is: value-column, one space, bar, one space, arrow.
    This keeps the trend verdict attached to the bar without making the value
    and bar look welded together by a deranged terminal goblin.
    """
    text = Text()
    text.append(" ")
    if isinstance(bar, Text):
        text.append(bar)
    else:
        text.append(str(bar))
    if arrow:
        style = f"bold {CYAN}" if arrow == FLAT else "bold yellow"
        text.append(" ")
        text.append(arrow, style=style)
    return text


def trended(value: str, arrow: str = "") -> str:
    arrow = arrow or ""
    return f"{value}{arrow}" if arrow else value


def delta_style(kind: str, delta: Any) -> str:
    return metric_state_style(kind, delta)


def value_delta_text(value: str, delta: Any, *, delta_kind: str = "", missing: str = "--") -> Text:
    """Compact actual-value plus delta text for rows like Rail 1.05k (+12)."""
    text = Text()
    text.append(value if value else missing, style="bold white")
    if is_number(delta):
        text.append(" (")
        text.append(signed_pressure(delta), style=delta_style(delta_kind, delta))
        text.append(")")
    return text


def value_delta_percent_text(value: str, delta: Any, *, delta_kind: str = "", missing: str = "--") -> Text:
    text = Text()
    text.append(value if value else missing, style="bold white")
    if is_number(delta):
        text.append(" (")
        text.append(signed(delta, 0), style=delta_style(delta_kind, delta))
        text.append(")")
    return text


def metric_table(*rows: tuple[str, str | Text, Text | str], label_width: int = 7, value_width: int = 11, bar_width: int = 7) -> Table:
    """Compact three-column metric table: label, value, bar/state.

    Values may be Text so deltas can be coloured independently from the actual
    value. This is the tiny mercy that stops negative deltas looking the same as
    positive ones, because apparently electrons demand nuance.
    """
    table = Table.grid(expand=False)
    table.add_column(width=label_width, style="dim", no_wrap=True, overflow="crop")
    table.add_column(width=value_width, justify="right", style="bold white", no_wrap=True, overflow="crop")
    table.add_column(width=bar_width, no_wrap=True, overflow="crop")
    for label, value, bar in rows:
        table.add_row(label, value, bar)
    return table

def efficiency_state(score_value: Any, *, driving_state: str = "", thermal_state: str = "", dpf_status: str = "", battery_v: Any = None) -> str:
    if "pressure" in (dpf_status or "").lower():
        return "DPF PRESS"
    if is_number(battery_v) and float(battery_v) < 11.8:
        return "LOW VOLT"
    if "hot" in (thermal_state or "").lower():
        return "THERMAL"
    if "lugging" in (driving_state or "").lower():
        return "LUGGING"
    if not is_number(score_value):
        return "UNKNOWN"
    v = float(score_value)
    if v >= 90:
        return "OPTIMAL"
    if v >= 78:
        return "EFFICIENT"
    if v >= 65:
        return "STEADY"
    if v >= 50:
        return "INEFFICIENT"
    return "POOR"


def mood_state(score_value: Any, *, driving_state: str = "", thermal_state: str = "", obd_connection: str = "", battery_v: Any = None) -> str:
    """Return the canonical mood-state key.

    The user-facing face and label now live in ``ui_text.json`` so they can be
    edited without touching scoring or UI code.
    """
    obd = (obd_connection or "").lower()
    if "reconnect" in obd:
        return "reconnecting"
    if any(bad in obd for bad in ["disconnect", "failed", "missing", "error", "offline", "none", "no obd"]):
        return "no OBD"
    if obd and not any(ok in obd for ok in ["sim", "connected", "live", "replay", "sweep"]):
        return "no OBD"
    if is_number(battery_v) and float(battery_v) < 11.8:
        return "low volts"
    thermal = (thermal_state or "").lower()
    if "hot" in thermal:
        return "hot"
    if "cold" in thermal:
        return "grumbling"
    if "warming" in thermal:
        return "fine"
    if "lugging" in (driving_state or "").lower():
        return "lugging"

    if not is_number(score_value):
        return "unknown"
    v = float(score_value)
    if v >= 97:
        return "happy"
    if v >= 88:
        return "smug"
    if v >= 72:
        return "fine"
    if v >= 55:
        return "grumbling"
    if v >= 35:
        return "strained"
    if v >= 20:
        return "sulking"
    return "upset"
