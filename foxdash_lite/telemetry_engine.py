from __future__ import annotations

"""Log conversion and replay helpers for FoxDash.

This file deliberately keeps old decoded-log -> UI-display conversion separate
from the Textual app. The UI should never learn how to interpret PSA decoded CSV
columns directly. It consumes TelemetrySnapshot, like a civilised little parasite.
"""

import csv
import datetime as dt
import math
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Deque, Iterable

from .telemetry import DISPLAY_FIELD_NAMES, TelemetrySnapshot

KM_TO_MPS = 0.44704
G = 9.80665


def is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not (isinstance(value, float) and math.isnan(value))


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    s = str(value).strip()
    if not s or s in {"--", "None", "nan", "NaN"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_int(value: Any, default: int = 0) -> int:
    v = parse_float(value)
    return default if v is None else int(round(v))


def parse_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    s = str(value).strip()
    return s if s else default


def parse_timestamp_seconds(value: Any, fallback: float) -> float:
    s = parse_str(value)
    if not s:
        return fallback
    try:
        # Handles offsets like +01:00 from the Pi logger.
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return fallback


def weighted_average(parts: Iterable[tuple[float | None, float]], default: float = 0.0) -> float:
    total = 0.0
    weight = 0.0
    for value, w in parts:
        if value is None or w <= 0:
            continue
        total += clamp(float(value), 0.0, 100.0) * w
        weight += w
    if weight <= 0:
        return default
    return total / weight


def norm(value: Any, lo: float, hi: float) -> float | None:
    v = parse_float(value)
    if v is None or hi <= lo:
        return None
    return clamp((v - lo) * 100.0 / (hi - lo), 0.0, 100.0)


@dataclass
class TelemetryRollingState:
    """Per-runtime/session state. Nothing here survives a new FoxDash session."""

    start_s: float | None = None
    last_sample_s: float | None = None
    prev_sample_s: float | None = None
    prev_speed_mph: float | None = None
    prev_pedal_proxy: float | None = None
    prev_boost_proxy: float | None = None
    prev_rail_proxy: float | None = None
    relative_accel_mps2: float | None = None
    relative_accel_g: float | None = None
    soot_window: Deque[tuple[float, float]] = None  # type: ignore[assignment]
    poll_window: Deque[tuple[float, bool]] = None  # type: ignore[assignment]
    session_extrema: dict[str, tuple[float, float]] = None  # type: ignore[assignment]
    lugging_candidate_since_s: float | None = None
    first_valid_sample_at: str = ""

    def __post_init__(self) -> None:
        if self.soot_window is None:
            self.soot_window = deque()
        if self.poll_window is None:
            self.poll_window = deque()
        if self.session_extrema is None:
            self.session_extrema = {}

    def mark_poll(self, now_s: float, ok: bool = True) -> None:
        self.poll_window.append((now_s, ok))
        while self.poll_window and now_s - self.poll_window[0][0] > 30.0:
            self.poll_window.popleft()

    def poll_success_percent(self) -> float | None:
        if not self.poll_window:
            return None
        good = sum(1 for _t, ok in self.poll_window if ok)
        return good * 100.0 / len(self.poll_window)

    def observe_session_extrema(self, key: str, value: Any) -> tuple[float | None, float | None]:
        """Update and return min/max for this runtime session only."""
        parsed = parse_float(value)
        if parsed is None:
            return self.session_extrema.get(key, (None, None))
        old = self.session_extrema.get(key)
        if old is None:
            current = (parsed, parsed)
        else:
            current = (min(old[0], parsed), max(old[1], parsed))
        self.session_extrema[key] = current
        return current

    def session_extrema_for(self, key: str) -> tuple[float | None, float | None]:
        return self.session_extrema.get(key, (None, None))


def get(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and str(row[name]).strip() != "":
            return row[name]
    return None


def canonical_from_decoded_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise old/new decoded-core CSV rows into canonical display inputs."""
    c: dict[str, Any] = {}
    for key, value in row.items():
        if key in {"timestamp", "sample", "gear"}:
            c[key] = parse_str(value)
        else:
            c[key] = parse_float(value)

    # Old log aliases from pre-Pi-display conversion.
    c["inj1FlowCorr"] = parse_float(get(row, "inj1FlowCorr", "inj1FlowCorr_pct"))
    c["inj2FlowCorr"] = parse_float(get(row, "inj2FlowCorr", "inj2FlowCorr_pct"))
    c["inj3FlowCorr"] = parse_float(get(row, "inj3FlowCorr", "inj3FlowCorr_pct"))
    c["inj4FlowCorr"] = parse_float(get(row, "inj4FlowCorr", "inj4FlowCorr_pct"))

    # If boost is not present, derive from absolute turbo pressure and baro.
    turbo = parse_float(get(row, "turboMeasured"))
    atm = parse_float(get(row, "atmospheric"))
    if parse_float(get(row, "boost_mbar")) is None and turbo is not None and atm is not None:
        c["boost_mbar"] = turbo - atm

    return c


def update_rolling_derived(canonical: dict[str, Any], rolling: TelemetryRollingState, now_s: float) -> dict[str, Any]:
    speed = parse_float(canonical.get("speed_mph"))
    soot = parse_float(canonical.get("fapSoot"))

    if rolling.start_s is None:
        rolling.start_s = now_s
    rolling.prev_sample_s = rolling.last_sample_s
    rolling.last_sample_s = now_s

    if speed is not None and rolling.prev_speed_mph is not None and rolling.prev_sample_s is not None:
        dt_s = max(0.05, now_s - rolling.prev_sample_s)
        rolling.relative_accel_mps2 = ((speed - rolling.prev_speed_mph) * KM_TO_MPS) / dt_s
        rolling.relative_accel_g = rolling.relative_accel_mps2 / G
    rolling.prev_speed_mph = speed

    if soot is not None:
        rolling.soot_window.append((now_s, soot))
        while rolling.soot_window and now_s - rolling.soot_window[0][0] > 600.0:
            rolling.soot_window.popleft()

    accel_min, accel_max = rolling.observe_session_extrema("relativeAccel_mps2", rolling.relative_accel_mps2)
    return {
        "relativeAccel_mps2": rolling.relative_accel_mps2,
        "relativeAccel_g": rolling.relative_accel_g,
        "relativeAccelSessionMin_mps2": accel_min,
        "relativeAccelSessionMax_mps2": accel_max,
    }


def derive_relative_accel_state(accel_mps2: Any) -> str:
    a = parse_float(accel_mps2)
    if a is None:
        return "unknown"
    if a > 0.7:
        return "pulling"
    if a > 0.15:
        return "gaining"
    if a < -0.7:
        return "braking/down"
    if a < -0.15:
        return "easing"
    return "steady"


LUGGING_PERSISTENCE_S = 0.75


def normalise_gear(value: Any) -> str:
    gear = parse_str(value, "").upper()
    return gear if gear else "--"


def gear_number(gear: str) -> int | None:
    try:
        number = int(gear)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 8 else None


def gear_is_engaged(gear: str) -> bool:
    return gear == "R" or gear_number(gear) is not None


def low_rpm_threshold(gear: str) -> float:
    """Gear-aware low-RPM threshold used by the shared interpretation layer."""
    if gear == "R":
        return 1400.0
    return {
        1: 1100.0,
        2: 1250.0,
        3: 1350.0,
        4: 1450.0,
        5: 1500.0,
        6: 1550.0,
    }.get(gear_number(gear), 1450.0)


def reset_lugging_candidate(rolling: TelemetryRollingState) -> None:
    rolling.lugging_candidate_since_s = None


def persisted_lugging_candidate(rolling: TelemetryRollingState, candidate: bool, now_s: float) -> bool:
    if not candidate:
        reset_lugging_candidate(rolling)
        return False
    if rolling.lugging_candidate_since_s is None:
        rolling.lugging_candidate_since_s = now_s
        return False
    return now_s - rolling.lugging_candidate_since_s >= LUGGING_PERSISTENCE_S


def derive_driving_state(values: dict[str, Any], rolling: TelemetryRollingState, now_s: float) -> str:
    """Interpret drivetrain state before scoring.

    Lugging requires an engaged drivetrain, a meaningful torque request, actual
    load support, low RPM for the *current* gear, and a short time persistence.
    Pedal-free coasting and engine braking are resolved first so gravity cannot
    falsely trigger the diesel's panic button.
    """
    rpm = parse_float(values.get("rpm"))
    speed = parse_float(values.get("speed_mph"))
    pedal = parse_float(values.get("pedalProxy"))
    load = parse_float(values.get("loadProxy"))
    abs_load = parse_float(values.get("absLoadProxy"))
    gear = normalise_gear(values.get("gear"))
    clutch = parse_float(values.get("CL")) or 0.0
    brake = parse_float(values.get("BR")) or 0.0
    accel = parse_float(values.get("relativeAccel_mps2"))

    # Without core inputs we have no right to assign a confident dynamic state.
    if rpm is None or speed is None or pedal is None:
        reset_lugging_candidate(rolling)
        return "unknown"

    engaged = gear_is_engaged(gear)
    reverse = gear == "R"
    moving = speed > (0.15 if reverse else 1.0)
    stationary = speed < 1.0

    if brake > 0.5:
        reset_lugging_candidate(rolling)
        return "braking"
    if clutch > 0.5:
        reset_lugging_candidate(rolling)
        return "clutch"

    if not engaged:
        reset_lugging_candidate(rolling)
        if stationary and rpm > 500:
            return "idle"
        return "neutral-roll" if moving else "neutral"

    # Zero pedal in a real gear is a coast/overrun state, never lugging.  A
    # downhill can maintain or even gain speed; throttle request remains the
    # useful discriminator rather than acceleration sign alone.
    if moving and pedal <= 7.0:
        reset_lugging_candidate(rolling)
        if rpm >= 1250.0 and accel is not None and accel < -0.12:
            return "engine-braking"
        return "coasting"

    demand_present = pedal >= 20.0 and (load is None or load >= 24.0)
    actual_load_present = abs_load is None or abs_load >= 18.0
    low_rpm = rpm < low_rpm_threshold(gear)
    lugging_candidate = moving and demand_present and actual_load_present and low_rpm
    lugging_persisted = persisted_lugging_candidate(rolling, lugging_candidate, now_s)

    if reverse:
        if lugging_persisted:
            return "reverse-lugging"
        if demand_present:
            return "reverse-load"
        return "reversing"

    if stationary and rpm > 500:
        return "idle"
    if lugging_persisted:
        return "lugging"
    if lugging_candidate:
        return "low-rpm-demand"
    if accel is not None and accel > 0.5:
        return "accelerating"
    if accel is not None and accel < -0.5:
        return "decelerating"
    if pedal > 60.0:
        return "high-demand"
    return "cruise"


def derive_drive_state_confidence(values: dict[str, Any]) -> float:
    required = ("rpm", "speed_mph", "pedalProxy")
    present = sum(parse_float(values.get(key)) is not None for key in required)
    gear_ok = normalise_gear(values.get("gear")) not in {"--", "?"}
    if present < len(required):
        return (present / len(required)) * 55.0
    return 100.0 if gear_ok else 70.0


def derive_score_confidence(values: dict[str, Any]) -> tuple[float, bool, str]:
    missing = [
        label for key, label in (("rpm", "RPM"), ("speed_mph", "speed"), ("pedalProxy", "pedal"))
        if parse_float(values.get(key)) is None
    ]
    gear = normalise_gear(values.get("gear"))
    if missing:
        return 0.0, False, "missing " + ", ".join(missing)
    if gear in {"--", "?"}:
        return 70.0, False, "gear unavailable"
    return 100.0, True, "ok"


def derive_guidance(values: dict[str, Any]) -> tuple[float | None, str]:
    """Return one signed driver correction from the same interpreted state.

    -1 means less engine speed / shift up / ease. +1 means more engine speed /
    downshift. This is the only source the LED renderer may use for position.
    """
    confidence, valid, _reason = derive_score_confidence(values)
    if not valid or confidence < 85.0:
        return None, "telemetry_incomplete"

    rpm = parse_float(values.get("rpm"))
    pedal = parse_float(values.get("pedalProxy")) or 0.0
    load = parse_float(values.get("loadProxy")) or 0.0
    gear = normalise_gear(values.get("gear"))
    state = str(values.get("drivingState") or "unknown").lower()
    if rpm is None:
        return None, "telemetry_incomplete"

    if state in {"lugging", "reverse-lugging"}:
        return 1.0, "reverse_low_rpm_load" if state == "reverse-lugging" else "low_rpm_high_demand"
    if state in {"low-rpm-demand", "reverse-load"}:
        return 0.68, "reverse_low_rpm_load" if state == "reverse-load" else "low_rpm_demand"
    if state == "engine-braking":
        return (-0.48, "engine_braking") if rpm > 2300.0 else (0.0, "engine_braking")
    if state == "coasting":
        return 0.0, "coasting_ok"
    if state.startswith("neutral") or state in {"idle", "clutch", "braking", "unknown"}:
        return 0.0, state

    numeric_gear = gear_number(gear)
    if numeric_gear is not None and numeric_gear <= 2 and rpm > 2850.0:
        return -min(0.95, 0.45 + (rpm - 2850.0) / 1450.0), "high_rpm_low_gear"
    if rpm > 3450.0 and max(pedal, load) < 60.0:
        return -min(0.85, 0.42 + (rpm - 3450.0) / 1300.0), "high_rpm_low_load"
    if rpm > 2950.0 and max(pedal, load) < 35.0:
        return -0.45, "high_rpm_low_load"
    return 0.0, "matched"


def derive_thermal_state(engine_temp: Any, heat_soak: Any) -> str:
    t = parse_float(engine_temp)
    hs = parse_float(heat_soak)
    if t is None:
        return "unknown"
    if t < 55:
        return "cold"
    if t < 75:
        return "warming"
    if t > 105:
        return "hot"
    if hs is not None and hs > 30:
        return "heat-soak"
    return "normal"


def derive_dpf_state(canonical: dict[str, Any], rolling: TelemetryRollingState) -> tuple[str, str]:
    soot = parse_float(canonical.get("fapSoot"))
    ftemp = parse_float(canonical.get("fapTemp"))
    fpress = parse_float(canonical.get("fapDiffPressure"))
    rpm = parse_float(canonical.get("rpm"))

    if soot is None or ftemp is None:
        return "UNKNOWN", "·"

    trend = "flat"
    if len(rolling.soot_window) >= 2:
        first_t, first_v = rolling.soot_window[0]
        last_t, last_v = rolling.soot_window[-1]
        dt_min = max(0.001, (last_t - first_t) / 60.0)
        if dt_min >= 1.0:
            slope = (last_v - first_v) / dt_min
            if slope > 0.002:
                trend = "up"
            elif slope < -0.002:
                trend = "down"

    arrow = {"up": "↑", "down": "↓", "flat": "→"}.get(trend, "·")

    if fpress is not None and rpm is not None:
        if (rpm < 2200 and fpress > 180) or fpress > 230:
            return "PRESSURE", arrow

    # REGEN is intentionally not inferred from hot exhaust or falling soot.
    # The currently mapped raw byte is not trusted enough yet, so confirmed
    # regeneration stays disabled until the flag is proven against Diagbox/FAP.

    if ftemp < 180:
        return "COLD", "·"
    if trend == "down" and ftemp > 300:
        return "BURNING", "↓"
    if trend == "up":
        return "CLIMBING", "↑"
    if ftemp > 430:
        return "HOT", arrow
    return "STABLE", arrow

def build_proxy_values(canonical: dict[str, Any], rolling: TelemetryRollingState, now_s: float) -> dict[str, Any]:
    derived = update_rolling_derived(canonical, rolling, now_s)

    pedal = parse_float(canonical.get("accelPedal"))
    driver_c9 = parse_float(canonical.get("DriverDemandMirror_C9"))
    driver_ca = parse_float(canonical.get("DriverDemandMirror_CA"))
    pedal_proxy = pedal if pedal is not None else (driver_c9 if driver_c9 is not None else driver_ca)

    turbo = parse_float(canonical.get("turboMeasured"))
    turbo_target = parse_float(canonical.get("turboTarget"))
    baro = parse_float(canonical.get("atmospheric"))
    rail = parse_float(canonical.get("fuelRailMeasured_bar"))
    rail_target = parse_float(canonical.get("fuelRailTarget_bar"))
    air_set = parse_float(canonical.get("airFlowSetting"))
    air_meas = parse_float(canonical.get("airFlowMeasured"))
    inj_flow = parse_float(canonical.get("injFlow"))
    fuel_flow_reg = parse_float(canonical.get("fuelFlowReg"))
    rpm = parse_float(canonical.get("rpm"))

    boost = turbo - baro if turbo is not None and baro is not None else parse_float(canonical.get("boost_mbar"))
    boost_target = turbo_target - baro if turbo_target is not None and baro is not None else None
    boost_error = turbo - turbo_target if turbo is not None and turbo_target is not None else None
    rail_error = rail - rail_target if rail is not None and rail_target is not None else None
    air_flow_error = air_meas - air_set if air_meas is not None and air_set is not None else None

    egr_t = parse_float(canonical.get("egrTarget"))
    egr_a = parse_float(canonical.get("egrRepeat"))
    air_mixer_t = parse_float(canonical.get("airMixerTarget"))
    air_mixer_a = parse_float(canonical.get("airMixer"))
    turbo_geo_t = parse_float(canonical.get("turboGeomTarget"))
    turbo_geo_a = parse_float(canonical.get("turboGeom"))
    ac_press = parse_float(canonical.get("airCPress_bar"))
    ac_session_min, ac_session_max = rolling.observe_session_extrema("airCPress_bar", ac_press)

    engine_temp = parse_float(canonical.get("oilTemp"))
    if engine_temp is None:
        engine_temp = parse_float(canonical.get("coolant"))
    intake_temp = parse_float(canonical.get("airManifoldTemp"))
    if intake_temp is None:
        intake_temp = parse_float(canonical.get("airFlowSensorTemp"))
    ambient_temp = parse_float(canonical.get("externalTemp"))
    heat_soak = intake_temp - ambient_temp if intake_temp is not None and ambient_temp is not None else None
    thermal_candidates = [
        parse_float(canonical.get("coolant")),
        parse_float(canonical.get("fuelTemp")),
        parse_float(canonical.get("airFlowSensorTemp")),
        parse_float(canonical.get("airManifoldTemp")),
    ]
    thermal_max = max([v for v in thermal_candidates if v is not None], default=None)

    requested_boost_norm = norm(boost_target, 0.0, 1500.0)
    actual_boost_norm = norm(boost, 0.0, 1500.0)
    rail_target_norm = norm(rail_target, 200.0, 1700.0)
    rail_actual_norm = norm(rail, 200.0, 1700.0)
    inj_norm = norm(inj_flow, 0.0, 65.0)
    fuel_reg_norm = norm(fuel_flow_reg, 0.0, 100.0)
    air_norm = norm(air_meas, 100.0, 900.0)
    rpm_effort_norm = norm(rpm, 800.0, 3800.0)

    load_proxy = weighted_average([
        (pedal_proxy, 0.35),
        (inj_norm, 0.25),
        (fuel_reg_norm, 0.15),
        (requested_boost_norm, 0.15),
        (rail_target_norm, 0.10),
    ], default=0.0)

    abs_load_proxy = weighted_average([
        (air_norm, 0.30),
        (actual_boost_norm, 0.25),
        (inj_norm, 0.25),
        (rail_actual_norm, 0.10),
        (rpm_effort_norm, 0.10),
    ], default=0.0)

    dpf_status, dpf_arrow = derive_dpf_state(canonical, rolling)
    relative_accel = derived.get("relativeAccel_mps2")

    out: dict[str, Any] = {
        "rpm": canonical.get("rpm"),
        "speed_mph": canonical.get("speed_mph"),
        "gear": canonical.get("gear"),
        "CL": canonical.get("CL"),
        "BR": canonical.get("BR"),
        "pedalProxy": pedal_proxy,
        "loadProxy": load_proxy,
        "absLoadProxy": abs_load_proxy,
        "engineTempProxy": engine_temp,
        "intakeTemp": intake_temp,
        "ambientTemp": ambient_temp,
        "heatSoakProxy": heat_soak,
        "thermalMaxProxy": thermal_max,
        "thermalState": derive_thermal_state(engine_temp, heat_soak),
        "mapProxy": turbo,
        "boostProxy": boost,
        "boostTargetProxy": boost_target,
        "boostErrorProxy": boost_error,
        "railProxy": rail,
        "railTargetProxy": rail_target,
        "railErrorProxy": rail_error,
        "baroProxy": baro,
        "dpfDiffProxy": canonical.get("fapDiffPressure"),
        "relativeAccel_mps2": relative_accel,
        "relativeAccel_g": derived.get("relativeAccel_g"),
        "relativeAccelState": derive_relative_accel_state(relative_accel),
        "relativeAccelSessionMin_mps2": derived.get("relativeAccelSessionMin_mps2"),
        "relativeAccelSessionMax_mps2": derived.get("relativeAccelSessionMax_mps2"),
        "injFlow": inj_flow,
        "fuelFlowReg": fuel_flow_reg,
        "airFlowSetting": air_set,
        "airFlowMeasured": air_meas,
        "airFlowError": air_flow_error,
        "egrTarget": egr_t,
        "egrActual": egr_a,
        "egrError": egr_a - egr_t if egr_a is not None and egr_t is not None else None,
        "airMixerTarget": air_mixer_t,
        "airMixerActual": air_mixer_a,
        "airMixerError": air_mixer_a - air_mixer_t if air_mixer_a is not None and air_mixer_t is not None else None,
        "turboGeomTarget": turbo_geo_t,
        "turboGeomActual": turbo_geo_a,
        "turboGeomError": turbo_geo_a - turbo_geo_t if turbo_geo_a is not None and turbo_geo_t is not None else None,
        "dpfSoot": canonical.get("fapSoot"),
        "dpfStatus": dpf_status,
        "dpfTrendArrow": dpf_arrow,
        "coolant": canonical.get("coolant"),
        "oilTemp": canonical.get("oilTemp", None),
        "fuelTemp": canonical.get("fuelTemp"),
        "airFlowSensorTemp": canonical.get("airFlowSensorTemp"),
        "airManifoldTemp": canonical.get("airManifoldTemp"),
        "externalTemp": canonical.get("externalTemp"),
        "batteryV": canonical.get("batteryV"),
        "airCPress_bar": ac_press,
        "airCPressSessionMin_bar": ac_session_min,
        "airCPressSessionMax_bar": ac_session_max,
        "fapTemp": canonical.get("fapTemp"),
        "fapDiffPressure": canonical.get("fapDiffPressure"),
        "lastRegen_mi": canonical.get("lastRegen_mi"),
        "avg10Regen_mi": canonical.get("avg10Regen_mi"),
        "fapLifeLeft_mi": canonical.get("fapLifeLeft_mi"),
        "inj1FlowCorr": canonical.get("inj1FlowCorr"),
        "inj2FlowCorr": canonical.get("inj2FlowCorr"),
        "inj3FlowCorr": canonical.get("inj3FlowCorr"),
        "inj4FlowCorr": canonical.get("inj4FlowCorr"),
    }
    out["drivingState"] = derive_driving_state(out, rolling, now_s)
    out["driveStateConfidence"] = derive_drive_state_confidence(out)
    out["guidanceCorrection"], out["guidanceReason"] = derive_guidance(out)
    return out


def score_operating_zone(v: dict[str, Any]) -> float:
    """Fuel-economy RPM suitability for the 1.6 HDi, separate from comfort."""
    rpm = parse_float(v.get("rpm"))
    speed = parse_float(v.get("speed_mph"))
    pedal = parse_float(v.get("pedalProxy")) or 0.0
    gear = normalise_gear(v.get("gear"))
    load = parse_float(v.get("loadProxy")) or 0.0
    state = str(v.get("drivingState") or "").lower()

    if rpm is None:
        return 0.0

    moving = speed is not None and speed > 3.0
    stationary = speed is not None and speed < 1.0
    neutral = gear in {"N", "--", "?"}
    reverse = gear == "R"

    if stationary and rpm > 500:
        score = 60.0
        if pedal > 8 or load > 12:
            score -= min(22.0, pedal * 0.45 + max(0.0, load - 12.0) * 0.45)
        return clamp(score, 0.0, 78.0)

    if neutral:
        score = 72.0 if moving else 65.0
        if moving and (pedal > 8 or load > 14):
            score = 48.0
        return clamp(score, 0.0, 78.0)

    # Overrun and true coasting can be fuel-efficient even at a higher RPM.
    # Mood/strain still narrows the operating band and guidance nudges the
    # driver down the revs if appropriate, but efficiency does not lie about
    # zero/near-zero fuel demand merely because engine speed is high.
    if state in {"coasting", "engine-braking"} and pedal <= 7.0:
        return 88.0 if state == "coasting" else 84.0

    if reverse:
        # Reverse is a manoeuvre, not a driving-economy contest. It can never
        # wear the white crown, but normal gentle reversing is not condemned.
        if pedal > 35 or load > 45:
            return 54.0
        return 72.0

    if load < 20:
        ideal_lo, ideal_hi = 1350.0, 2200.0
    elif load < 45:
        ideal_lo, ideal_hi = 1500.0, 2500.0
    else:
        ideal_lo, ideal_hi = 1750.0, 2800.0

    if ideal_lo <= rpm <= ideal_hi:
        score = 94.0
    elif rpm < ideal_lo:
        score = 94.0 - min(42.0, (ideal_lo - rpm) / 16.0)
    else:
        score = 94.0 - min(48.0, (rpm - ideal_hi) / 25.0)

    if load > 55 and rpm < 1750:
        score -= min(22.0, (1750.0 - rpm) / 26.0 + (load - 55.0) * 0.18)
    elif load > 35 and rpm < 1500:
        score -= min(18.0, (1500.0 - rpm) / 28.0 + (load - 35.0) * 0.15)

    # A lower gear at busy RPM is more fuel-hungry even while acceleration is
    # legitimate. The penalty is moderate during genuine demand, not excused.
    numeric_gear = gear_number(gear)
    if numeric_gear == 1 and rpm > 2800:
        score -= min(28.0, (rpm - 2800.0) / 33.0)
    elif numeric_gear == 2 and rpm > 2600:
        score -= min(32.0, (rpm - 2600.0) / 26.0)
    elif numeric_gear == 3 and rpm > 3000:
        score -= min(22.0, (rpm - 3000.0) / 32.0)

    if rpm > 4000:
        score -= 35.0
    elif rpm > 3600:
        score -= 18.0 + min(18.0, (rpm - 3600.0) / 30.0)
    elif rpm > 3300:
        score -= 9.0 if load >= 55 else 17.0
    elif rpm > 2800 and load < 35:
        score -= min(20.0, (rpm - 2800.0) / 35.0)

    if "lugging" in state:
        score = min(score, 62.0)
    elif state == "low-rpm-demand":
        score = min(score, 70.0)

    return clamp(score, 0.0, 100.0)


def score_load_efficiency(v: dict[str, Any]) -> float:
    """Fuel-economy demand score.

    This is deliberately not a mechanical judgement. High demand may be valid
    and even fun, but it is not good fuel economy. Speed limiter/no-accel and
    fake-proxy mismatch penalties stay out until we have a firmer foothold in
    reality. Tiny miracle: restraint.
    """
    load = parse_float(v.get("loadProxy"))
    pedal = parse_float(v.get("pedalProxy"))
    inj = parse_float(v.get("injFlow"))
    fuel_reg = parse_float(v.get("fuelFlowReg"))
    ac_press = parse_float(v.get("airCPress_bar"))
    speed = parse_float(v.get("speed_mph"))
    gear = str(v.get("gear") or "").upper()

    parts: list[tuple[float | None, float]] = []
    if load is not None:
        parts.append((load, 0.45))
    if pedal is not None:
        parts.append((pedal, 0.25))
    if inj is not None:
        parts.append((norm(inj, 0.0, 65.0), 0.20))
    if fuel_reg is not None:
        parts.append((fuel_reg, 0.10))
    if ac_press is not None:
        # A/C is not "forgiven" by the economy score. Comfort is valid, but
        # the compressor still costs energy, so it joins the demand blend as a
        # small accessory-load contributor rather than a direct punishment.
        parts.append((norm(ac_press, 2.0, 20.0), 0.08))
    effort = weighted_average(parts, default=load if load is not None else 40.0)

    # Low effort/coasting is good economy. Moderate demand is normal. Heavy
    # demand drops quickly because the score is now fuel economy, not heroism.
    if effort <= 8:
        score = 96.0
    elif effort <= 22:
        score = 96.0 - (effort - 8.0) * 0.55
    elif effort <= 42:
        score = 88.0 - (effort - 22.0) * 0.90
    elif effort <= 65:
        score = 70.0 - (effort - 42.0) * 0.85
    else:
        score = 50.0 - (effort - 65.0) * 0.80

    # Stationary/neutral caps. Neutral moving can be harmless, but on a modern
    # diesel overrun-in-gear can use less fuel than neutral idle, so it doesn't
    # get to wear the little crown.
    if speed is not None and speed < 1.0:
        score = min(score, 62.0)
    if gear == "N" and speed is not None and speed > 3.0:
        score = min(score, 78.0)

    return clamp(score, 0.0, 100.0)

def score_thermal_efficiency(v: dict[str, Any]) -> float:
    """Fuel-economy thermal score.

    Cold engines are inefficient. Warm-but-not-silly-hot is good. That is the
    whole sermon; temperatures are boring and therefore useful.
    """
    engine_temp = parse_float(v.get("engineTempProxy"))
    heat_soak = parse_float(v.get("heatSoakProxy"))
    intake = parse_float(v.get("intakeTemp"))

    if engine_temp is None:
        score = 58.0
    elif engine_temp < 45.0:
        score = 42.0 + engine_temp * 0.40
    elif engine_temp < 55.0:
        score = 60.0 + (engine_temp - 45.0) * 1.0
    elif engine_temp < 75.0:
        score = 70.0 + (engine_temp - 55.0) * 0.9
    elif engine_temp <= 95.0:
        score = 92.0
    elif engine_temp <= 100.0:
        score = 90.0 - (engine_temp - 95.0) * 1.2
    elif engine_temp <= 105.0:
        score = 82.0 - (engine_temp - 100.0) * 3.0
    else:
        score = 55.0 - (engine_temp - 105.0) * 4.0

    if heat_soak is not None and heat_soak > 12:
        score -= min(14.0, (heat_soak - 12.0) * 0.45)
    if intake is not None and intake > 38:
        score -= min(10.0, (intake - 38.0) * 0.25)

    return clamp(score, 0.0, 100.0)

def score_flow_efficiency(v: dict[str, Any]) -> float:
    boost_err = parse_float(v.get("boostErrorProxy"))
    rail_err = parse_float(v.get("railErrorProxy"))
    air_err = parse_float(v.get("airFlowError"))
    boost_target = parse_float(v.get("boostTargetProxy"))
    rail_target = parse_float(v.get("railTargetProxy"))
    air_target = parse_float(v.get("airFlowSetting"))
    egr_err = parse_float(v.get("egrError"))
    egr_target = parse_float(v.get("egrTarget"))
    mix_err = parse_float(v.get("airMixerError"))
    mix_target = parse_float(v.get("airMixerTarget"))
    rpm = parse_float(v.get("rpm"))
    load = parse_float(v.get("loadProxy")) or 0.0
    pedal = parse_float(v.get("pedalProxy")) or 0.0

    score = 84.0
    demand = max(load, pedal)
    if demand < 10:
        demand_factor = 1.7
    elif demand < 25:
        demand_factor = 1.25
    else:
        demand_factor = 1.0

    if boost_err is not None:
        allowed = max(80.0, abs(boost_target or 0.0) * 0.12) * demand_factor
        over = max(0.0, abs(boost_err) - allowed)
        score -= min(28.0, over / 12.0)
        if over == 0 and demand > 20:
            score += 4.0
    if rail_err is not None:
        allowed = max(50.0, abs(rail_target or 0.0) * 0.05) * demand_factor
        over = max(0.0, abs(rail_err) - allowed)
        score -= min(24.0, over / 8.0)
        if over == 0 and demand > 20:
            score += 3.0
    if air_err is not None:
        allowed = max(60.0, abs(air_target or 0.0) * 0.12) * demand_factor
        over = max(0.0, abs(air_err) - allowed)
        score -= min(18.0, over / 12.0)
        if over == 0 and demand > 20:
            score += 2.0

    # EGR and air mixer tracking are air-path health signals. They are useful,
    # but not allowed to dominate fuel-economy scoring because emissions control
    # can move these around legitimately. Tiny leash, useful bite.
    if rpm is not None and rpm > 1000 and demand > 15:
        if egr_err is not None:
            allowed = max(8.0, abs(egr_target or 0.0) * 0.10) * demand_factor
            over = max(0.0, abs(egr_err) - allowed)
            score -= min(6.0, over / 4.0)
        if mix_err is not None:
            allowed = max(8.0, abs(mix_target or 0.0) * 0.10) * demand_factor
            over = max(0.0, abs(mix_err) - allowed)
            score -= min(6.0, over / 4.0)

    return clamp(score, 0.0, 100.0)


def score_thermal_comfort(v: dict[str, Any]) -> float:
    """Mechanical comfort thermal score for mood.

    Mood cares whether the engine feels comfortable, not whether the current
    state is economical. Cold is uncomfortable, warming is okay, hot gets the
    red carpet of shame.
    """
    engine_temp = parse_float(v.get("engineTempProxy"))
    heat_soak = parse_float(v.get("heatSoakProxy"))
    thermal_max = parse_float(v.get("thermalMaxProxy"))
    dpf_temp = parse_float(v.get("fapTemp"))
    dpf_status = v.get("dpfStatus")

    if engine_temp is None:
        score = 55.0
    elif engine_temp < 45.0:
        score = 38.0 + engine_temp * 0.45
    elif engine_temp < 55.0:
        score = 58.0 + (engine_temp - 45.0) * 1.2
    elif engine_temp < 75.0:
        score = 70.0 + (engine_temp - 55.0) * 0.9
    elif engine_temp <= 98.0:
        score = 96.0
    elif engine_temp <= 103.0:
        score = 90.0 - (engine_temp - 98.0) * 2.0
    else:
        score = 70.0 - (engine_temp - 103.0) * 4.0

    if heat_soak is not None and heat_soak > 22:
        score -= min(20.0, (heat_soak - 22.0) * 0.75)
    if thermal_max is not None and thermal_max > 98:
        score -= min(20.0, (thermal_max - 98.0) * 0.75)
    if dpf_temp is not None and dpf_temp > 520 and dpf_status not in {"REGEN", "BURNING"}:
        score -= 16.0
    return clamp(score, 0.0, 100.0)

def score_strain(v: dict[str, Any]) -> float:
    """Mechanical comfort / inverse strain score for mood."""
    rpm = parse_float(v.get("rpm"))
    pedal = parse_float(v.get("pedalProxy")) or 0.0
    load = parse_float(v.get("loadProxy")) or 0.0
    boost = parse_float(v.get("boostProxy"))
    dpf_diff = parse_float(v.get("dpfDiffProxy"))
    speed = parse_float(v.get("speed_mph")) or 0.0
    state = str(v.get("drivingState") or "").lower()
    gear = normalise_gear(v.get("gear"))

    score = 92.0
    if state == "coasting" and speed > 8:
        score += 5.0

    threshold = low_rpm_threshold(gear)
    if rpm is not None and state in {"lugging", "reverse-lugging", "low-rpm-demand", "reverse-load"}:
        score -= min(42.0, max(0.0, threshold - rpm) / 10.0 + max(0.0, load - 25.0) * 0.36)
        if "lugging" in state:
            score -= 8.0

    # High RPM during engine braking is not fuel waste, but it is still a
    # narrower, noisier operating point. Keep the mechanical penalty modest.
    if rpm is not None and state == "engine-braking" and rpm > 2800:
        score -= min(18.0, 5.0 + (rpm - 2800.0) / 65.0)
    elif rpm is not None and rpm > 3600:
        score -= min(38.0, 18.0 + (rpm - 3600.0) / 22.0)
    elif rpm is not None and rpm > 3300 and load < 45:
        score -= min(22.0, (rpm - 3300.0) / 30.0)

    score -= max(0.0, load - 52.0) * 0.65
    score -= max(0.0, pedal - 70.0) * 0.35
    if boost is not None and boost > 900:
        score -= min(20.0, (boost - 900.0) / 35.0)
    if dpf_diff is not None and dpf_diff > 150:
        score -= min(24.0, (dpf_diff - 150.0) / 5.0)
    if state in {"lugging", "reverse-lugging"}:
        score = min(score, 48.0)
    return clamp(score, 0.0, 100.0)


def score_delivery(v: dict[str, Any], rolling: TelemetryRollingState) -> float:
    """Delivery quality for mood.

    This is physical response/clean tracking, not fuel economy. It should stay
    high when the car is delivering calmly, then fall when boost/rail/air are
    noticeably missing their marks under demand.
    """
    boost_err = parse_float(v.get("boostErrorProxy"))
    rail_err = parse_float(v.get("railErrorProxy"))
    air_err = parse_float(v.get("airFlowError"))
    boost_target = parse_float(v.get("boostTargetProxy"))
    rail_target = parse_float(v.get("railTargetProxy"))
    air_target = parse_float(v.get("airFlowSetting"))
    egr_err = parse_float(v.get("egrError"))
    egr_target = parse_float(v.get("egrTarget"))
    mix_err = parse_float(v.get("airMixerError"))
    mix_target = parse_float(v.get("airMixerTarget"))
    rpm = parse_float(v.get("rpm"))
    load = parse_float(v.get("loadProxy")) or 0.0
    pedal = parse_float(v.get("pedalProxy")) or 0.0
    state = str(v.get("drivingState") or "").lower()

    score = 90.0
    demand = max(load, pedal)
    if demand < 10:
        demand_factor = 1.5
    elif demand < 25:
        demand_factor = 1.2
    else:
        demand_factor = 1.0

    if boost_err is not None:
        allowed = max(90.0, abs(boost_target or 0.0) * 0.13) * demand_factor
        score -= min(24.0, max(0.0, abs(boost_err) - allowed) / 11.0)
    if rail_err is not None:
        allowed = max(55.0, abs(rail_target or 0.0) * 0.055) * demand_factor
        score -= min(20.0, max(0.0, abs(rail_err) - allowed) / 8.5)
    if air_err is not None:
        allowed = max(65.0, abs(air_target or 0.0) * 0.13) * demand_factor
        score -= min(16.0, max(0.0, abs(air_err) - allowed) / 13.0)

    # Delivery is allowed to care a little more about air-path mismatch than
    # pure economy. If EGR/mixer are not tracking under demand, the car may feel
    # lazy or constrained even when the main pressure numbers are mostly sane.
    if rpm is not None and rpm > 1000 and demand > 15:
        if egr_err is not None:
            allowed = max(8.0, abs(egr_target or 0.0) * 0.12) * demand_factor
            over = max(0.0, abs(egr_err) - allowed)
            score -= min(8.0, over / 3.5)
        if mix_err is not None:
            allowed = max(8.0, abs(mix_target or 0.0) * 0.12) * demand_factor
            over = max(0.0, abs(mix_err) - allowed)
            score -= min(8.0, over / 3.5)

    if state == "lugging":
        score = min(score, 66.0)
    return clamp(score, 0.0, 100.0)

def score_electrical(v: dict[str, Any], rolling: TelemetryRollingState) -> float:
    batt = parse_float(v.get("batteryV"))
    poll = rolling.poll_success_percent()
    score = 96.0
    if batt is not None:
        if batt < 11.8:
            score -= 45.0
        elif batt < 12.4:
            score -= 20.0
        elif batt > 15.0:
            score -= 30.0
    else:
        score -= 20.0
    if poll is not None:
        score -= max(0.0, 100.0 - poll) * 0.6
    return clamp(score, 0.0, 100.0)


def derive_mood_state(mood_score: float | None, v: dict[str, Any]) -> str:
    if not bool(v.get("telemetryValid", True)):
        return "unknown"
    if v.get("dpfStatus") == "PRESSURE":
        return "DPF pressure"
    thermal = str(v.get("thermalState") or "").lower()
    if "hot" in thermal:
        return "hot"
    if "lugging" in str(v.get("drivingState") or "").lower():
        return "lugging"
    if "cold" in thermal:
        return "grumbling"
    if "warming" in thermal:
        return "fine"
    if mood_score is None:
        return "unknown"
    if mood_score >= 96:
        return "happy"
    if mood_score >= 86:
        return "smug"
    if mood_score >= 72:
        return "fine"
    if mood_score >= 55:
        return "grumbling"
    if mood_score >= 35:
        return "strained"
    if mood_score >= 20:
        return "sulking"
    return "upset"


def canonical_from_ui_display_row(row: dict[str, Any]) -> dict[str, Any]:
    """Reverse-map a UI display row into engine input values for full-train replay.

    Old UI CSVs are not raw decoder logs, but they contain enough actual,
    target and environmental values to re-run the current proxy/score layer.
    That keeps replay behind the same engine boundary as live data instead
    of allowing UI replay to secretly bypass the brain.
    """
    c: dict[str, Any] = {}
    for key, value in row.items():
        c[key] = parse_str(value) if key in {"timestamp", "gear"} else parse_float(value)

    c["rpm"] = parse_float(get(row, "rpm"))
    c["speed_mph"] = parse_float(get(row, "speed_mph"))
    c["gear"] = parse_str(get(row, "gear"), "N")
    c["CL"] = parse_float(get(row, "CL"))
    c["BR"] = parse_float(get(row, "BR"))
    c["accelPedal"] = parse_float(get(row, "pedalProxy", "accelPedal"))
    c["batteryV"] = parse_float(get(row, "batteryV"))
    c["coolant"] = parse_float(get(row, "coolant"))
    c["airCPress_bar"] = parse_float(get(row, "airCPress_bar"))
    c["fuelTemp"] = parse_float(get(row, "fuelTemp"))
    c["fuelFlowReg"] = parse_float(get(row, "fuelFlowReg"))
    c["fuelRailTarget_bar"] = parse_float(get(row, "railTargetProxy"))
    c["fuelRailMeasured_bar"] = parse_float(get(row, "railProxy"))
    c["injFlow"] = parse_float(get(row, "injFlow"))
    c["airFlowSetting"] = parse_float(get(row, "airFlowSetting"))
    c["airFlowMeasured"] = parse_float(get(row, "airFlowMeasured"))
    c["airFlowSensorTemp"] = parse_float(get(row, "airFlowSensorTemp"))
    c["airManifoldTemp"] = parse_float(get(row, "airManifoldTemp", "intakeTemp"))
    c["externalTemp"] = parse_float(get(row, "externalTemp", "ambientTemp"))
    c["atmospheric"] = parse_float(get(row, "baroProxy"))
    c["turboMeasured"] = parse_float(get(row, "mapProxy"))
    target_boost = parse_float(get(row, "boostTargetProxy"))
    baro = c["atmospheric"]
    c["turboTarget"] = (target_boost + baro) if target_boost is not None and baro is not None else None
    c["egrTarget"] = parse_float(get(row, "egrTarget"))
    c["egrRepeat"] = parse_float(get(row, "egrActual", "egrRepeat"))
    c["airMixerTarget"] = parse_float(get(row, "airMixerTarget"))
    c["airMixer"] = parse_float(get(row, "airMixerActual", "airMixer"))
    c["turboGeomTarget"] = parse_float(get(row, "turboGeomTarget"))
    c["turboGeom"] = parse_float(get(row, "turboGeomActual", "turboGeom"))
    c["fapSoot"] = parse_float(get(row, "dpfSoot", "fapSoot"))
    c["fapTemp"] = parse_float(get(row, "fapTemp"))
    c["fapDiffPressure"] = parse_float(get(row, "fapDiffPressure", "dpfDiffProxy"))
    c["lastRegen_mi"] = parse_float(get(row, "lastRegen_mi"))
    c["avg10Regen_mi"] = parse_float(get(row, "avg10Regen_mi"))
    c["fapLifeLeft_mi"] = parse_float(get(row, "fapLifeLeft_mi"))
    c["fapAdditiveVol"] = parse_float(get(row, "fapAdditiveVol"))
    c["fapAdditiveRemain"] = parse_float(get(row, "fapAdditiveRemain"))
    for name in ("inj1FlowCorr", "inj2FlowCorr", "inj3FlowCorr", "inj4FlowCorr"):
        c[name] = parse_float(get(row, name))
    return c


class TelemetryEngine:
    """Owns rolling derived values, DPF state and all scoring.

    Both live polling and replay submit canonical input dictionaries here.
    The engine returns one display-ready ``TelemetrySnapshot``. It knows
    nothing about serial, CSV handles, Textual, LEDs or I²C hardware.
    """

    def __init__(self, *, session_id: str = "", boot_id: str = "", session_started_at: str = "") -> None:
        self.rolling = TelemetryRollingState()
        self.session_id = session_id
        self.boot_id = boot_id
        self.session_started_at = session_started_at

    def process(
        self,
        canonical: dict[str, Any],
        *,
        timestamp: str | None = None,
        sample: int = 0,
        obd_connection: str = "live",
        adapter_state: str = "connected",
        ecu_session_state: str = "engine/SID807",
        protocol: str = "PSA UDS raw header",
        poll_health: str = "--",
        poll_ok: bool = True,
        last_update_age_s: float = 0.0,
        source_time_s: float | None = None,
    ) -> TelemetrySnapshot:
        canonical = dict(canonical)
        out_timestamp = timestamp or parse_str(canonical.get("timestamp"), dt.datetime.now().astimezone().isoformat(timespec="milliseconds"))
        now_s = source_time_s if source_time_s is not None else time.monotonic()
        self.rolling.mark_poll(now_s, poll_ok)
        values = build_proxy_values(canonical, self.rolling, now_s)

        score_confidence, telemetry_valid, score_reason = derive_score_confidence(values)
        values["scoreConfidence"] = score_confidence
        values["telemetryValid"] = telemetry_valid
        values["scoreReason"] = score_reason
        if telemetry_valid and not self.rolling.first_valid_sample_at:
            self.rolling.first_valid_sample_at = out_timestamp

        # Carry slow maintenance values through the same refined snapshot.
        additive_vol = parse_float(canonical.get("fapAdditiveVol"))
        additive_remain = parse_float(canonical.get("fapAdditiveRemain"))
        additive_percent = None
        if additive_vol is not None and additive_remain is not None and additive_vol + additive_remain > 0:
            additive_percent = additive_remain * 100.0 / (additive_vol + additive_remain)
        values["fapAdditiveVol"] = additive_vol
        values["fapAdditiveRemain"] = additive_remain
        values["fapAdditivePercent"] = additive_percent

        if telemetry_valid:
            eff_operating = score_operating_zone(values)
            eff_load = score_load_efficiency(values)
            eff_thermal = score_thermal_efficiency(values)
            eff_flow = score_flow_efficiency(values)
            efficiency = weighted_average([
                (eff_operating, 1.2),
                (eff_load, 1.5),
                (eff_thermal, 0.9),
                (eff_flow, 0.7),
            ], default=0.0)

            mood_thermal = score_thermal_comfort(values)
            mood_strain = score_strain(values)
            mood_delivery = score_delivery(values, self.rolling)
            mood_electrical = score_electrical(values, self.rolling)
            mood_score = weighted_average([
                (mood_thermal, 1.1),
                (mood_strain, 1.5),
                (mood_delivery, 1.0),
                (mood_electrical, 0.6),
            ], default=0.0)
        else:
            efficiency = None
            eff_operating = eff_load = eff_thermal = eff_flow = None
            mood_score = None
            mood_thermal = mood_strain = mood_delivery = mood_electrical = None

        self.rolling.prev_pedal_proxy = parse_float(values.get("pedalProxy"))
        self.rolling.prev_boost_proxy = parse_float(values.get("boostProxy"))
        self.rolling.prev_rail_proxy = parse_float(values.get("railProxy"))

        sample_rate = None
        if self.rolling.prev_sample_s is not None and self.rolling.last_sample_s is not None:
            dt_s = max(0.001, self.rolling.last_sample_s - self.rolling.prev_sample_s)
            sample_rate = 1.0 / dt_s
        calculated_poll = self.rolling.poll_success_percent()
        resolved_poll_health = poll_health
        if resolved_poll_health in {"", "--"} and calculated_poll is not None:
            resolved_poll_health = f"{calculated_poll:.0f}%"

        snapshot_values = {k: v for k, v in values.items() if k in DISPLAY_FIELD_NAMES}
        snapshot_values.update({
            "timestamp": out_timestamp,
            "sample": sample,
            "sessionId": self.session_id,
            "bootId": self.boot_id,
            "sessionStartedAt": self.session_started_at,
            "firstValidSampleAt": self.rolling.first_valid_sample_at,
            "obdConnection": obd_connection,
            "adapterState": adapter_state,
            "ecuSessionState": ecu_session_state,
            "protocol": protocol,
            "pollHealth": resolved_poll_health,
            "sampleRateHz": sample_rate,
            "lastUpdateAge_s": last_update_age_s,
            "telemetryValid": telemetry_valid,
            "scoreConfidence": score_confidence,
            "scoreReason": score_reason,
            "efficiencyScore": efficiency,
            "effOperatingZone": eff_operating,
            "effLoad": eff_load,
            "effThermal": eff_thermal,
            "effFlow": eff_flow,
            "moodScore": mood_score,
            "moodThermalComfort": mood_thermal,
            "moodStrain": mood_strain,
            "moodDelivery": mood_delivery,
            "moodElectrical": mood_electrical,
            "moodState": derive_mood_state(mood_score, values),
        })
        return TelemetrySnapshot(**snapshot_values)
