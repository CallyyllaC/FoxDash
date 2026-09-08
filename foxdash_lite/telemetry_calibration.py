from __future__ import annotations

"""Data-backed score calibration layered on top of the deterministic engine.

Pass 1 owns physical/deterministic interpretation such as braking, clutch,
coasting, lugging persistence and confirmed DPF regeneration. This module owns
how that interpreted snapshot becomes economy, mechanical-comfort and guidance
scores. Keeping the layers separate makes later regime learning additive rather
than letting an unsupervised model rewrite guardrails.
"""

import time
from dataclasses import replace

from .telemetry import TelemetrySnapshot
from .telemetry_engine import TelemetryEngine, clamp, gear_number, low_rpm_threshold, norm, parse_float, weighted_average


MOOD_STATE_DWELL_S = 1.0
MOOD_UP_HYSTERESIS = 1.0
MOOD_DOWN_HYSTERESIS = 2.0

MOOD_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("happy", 94.0),
    ("smug", 86.0),
    ("fine", 72.0),
    ("grumbling", 55.0),
    ("strained", 35.0),
    ("sulking", 20.0),
    ("upset", 0.0),
)


def _value(snapshot: TelemetrySnapshot, name: str) -> float | None:
    return parse_float(getattr(snapshot, name, None))


def derive_engine_demand(snapshot: TelemetrySnapshot) -> float | None:
    """Estimate engine effort without accelerator-pedal position.

    The speed limiter can leave pedal position near its cap while delivered
    torque is ordinary cruise. Fuel, pressure targets and actual-load evidence
    therefore define this proxy; pedal remains available separately as driver
    intent rather than being allowed to impersonate engine workload.
    """
    return weighted_average(
        [
            (norm(_value(snapshot, "injFlow"), 0.0, 65.0), 0.30),
            (norm(_value(snapshot, "boostTargetProxy"), 0.0, 1500.0), 0.25),
            (norm(_value(snapshot, "railTargetProxy"), 200.0, 1700.0), 0.20),
            (_value(snapshot, "fuelFlowReg"), 0.15),
            (_value(snapshot, "absLoadProxy"), 0.10),
        ],
        default=0.0,
    )


def score_operating_zone(snapshot: TelemetrySnapshot, engine_demand: float) -> float:
    rpm = _value(snapshot, "rpm")
    speed = _value(snapshot, "speed_mph")
    gear = (snapshot.gear or "--").upper()
    state = (snapshot.drivingState or "unknown").lower()
    if rpm is None:
        return 0.0

    stationary = speed is not None and speed < 1.0
    neutral = gear in {"N", "--", "?"}
    reverse = gear == "R"

    if stationary and rpm > 500.0:
        score = 62.0 - min(22.0, max(0.0, engine_demand - 12.0) * 0.8)
        return clamp(score, 0.0, 78.0)

    if neutral:
        score = 72.0 if speed is not None and speed > 3.0 else 65.0
        if speed is not None and speed > 3.0 and engine_demand > 18.0:
            score = 50.0
        return clamp(score, 0.0, 78.0)

    if state in {"coasting", "engine-braking"}:
        return 94.0 if state == "coasting" else 90.0

    if reverse:
        return 58.0 if engine_demand > 45.0 else 74.0

    if engine_demand < 15.0:
        ideal_lo, ideal_hi = 1250.0, 2100.0
    elif engine_demand < 35.0:
        ideal_lo, ideal_hi = 1400.0, 2300.0
    elif engine_demand < 55.0:
        ideal_lo, ideal_hi = 1600.0, 2550.0
    else:
        ideal_lo, ideal_hi = 1800.0, 2850.0

    if ideal_lo <= rpm <= ideal_hi:
        score = 97.0
    elif rpm < ideal_lo:
        score = 97.0 - min(50.0, (ideal_lo - rpm) / 15.0)
    else:
        score = 97.0 - min(52.0, (rpm - ideal_hi) / 28.0)

    if engine_demand > 55.0 and rpm < 1750.0:
        score -= min(25.0, (1750.0 - rpm) / 22.0 + (engine_demand - 55.0) * 0.25)
    elif engine_demand > 35.0 and rpm < 1450.0:
        score -= min(15.0, (1450.0 - rpm) / 28.0 + (engine_demand - 35.0) * 0.12)

    numeric_gear = gear_number(gear)
    if numeric_gear == 1 and rpm > 3000.0:
        score -= min(28.0, (rpm - 3000.0) / 35.0)
    elif numeric_gear == 2 and rpm > 2850.0:
        score -= min(26.0, (rpm - 2850.0) / 32.0)
    elif numeric_gear == 3 and rpm > 3200.0:
        score -= min(20.0, (rpm - 3200.0) / 35.0)

    if rpm > 4000.0:
        score -= 35.0
    elif rpm > 3500.0 and engine_demand < 55.0:
        score -= min(28.0, 12.0 + (rpm - 3500.0) / 35.0)
    elif rpm > 3000.0 and engine_demand < 25.0:
        score -= min(18.0, (rpm - 3000.0) / 40.0)

    if state in {"lugging", "reverse-lugging"}:
        score = min(score, 58.0)
    elif state == "low-rpm-demand":
        score = min(score, 70.0)
    return clamp(score, 0.0, 100.0)


def score_load_efficiency(snapshot: TelemetrySnapshot, engine_demand: float) -> float:
    """Economy cost of actual/requested engine effort, not pedal position."""
    effort = clamp(engine_demand, 0.0, 100.0)
    if effort <= 8.0:
        score = 98.0
    elif effort <= 20.0:
        score = 98.0 - (effort - 8.0) * 0.50
    elif effort <= 35.0:
        score = 92.0 - (effort - 20.0) * 0.80
    elif effort <= 50.0:
        score = 80.0 - (effort - 35.0) * 1.20
    elif effort <= 70.0:
        score = 62.0 - (effort - 50.0) * 1.50
    else:
        score = 32.0 - (effort - 70.0) * 1.20

    speed = _value(snapshot, "speed_mph")
    gear = (snapshot.gear or "--").upper()
    if speed is not None and speed < 1.0:
        score = min(score, 62.0)
    if gear == "N" and speed is not None and speed > 3.0:
        score = min(score, 76.0)
    return clamp(score, 0.0, 100.0)


def _demand_tolerance_factor(engine_demand: float) -> float:
    if engine_demand < 10.0:
        return 1.70
    if engine_demand < 25.0:
        return 1.35
    if engine_demand < 50.0:
        return 1.10
    return 1.0


def score_flow_efficiency(snapshot: TelemetrySnapshot, engine_demand: float) -> float:
    boost_err = _value(snapshot, "boostErrorProxy")
    rail_err = _value(snapshot, "railErrorProxy")
    air_err = _value(snapshot, "airFlowError")
    boost_target = _value(snapshot, "boostTargetProxy")
    rail_target = _value(snapshot, "railTargetProxy")
    air_target = _value(snapshot, "airFlowSetting")
    egr_err = _value(snapshot, "egrError")
    egr_target = _value(snapshot, "egrTarget")
    mix_err = _value(snapshot, "airMixerError")
    mix_target = _value(snapshot, "airMixerTarget")
    rpm = _value(snapshot, "rpm")

    score = 90.0
    factor = _demand_tolerance_factor(engine_demand)

    if boost_err is not None:
        allowed = max(75.0, abs(boost_target or 0.0) * 0.11) * factor
        over = max(0.0, abs(boost_err) - allowed)
        score -= min(30.0, over / 10.0)
        if over == 0.0 and engine_demand > 20.0:
            score += 3.0
    if rail_err is not None:
        allowed = max(45.0, abs(rail_target or 0.0) * 0.045) * factor
        over = max(0.0, abs(rail_err) - allowed)
        score -= min(26.0, over / 7.0)
        if over == 0.0 and engine_demand > 20.0:
            score += 2.0
    if air_err is not None:
        allowed = max(55.0, abs(air_target or 0.0) * 0.11) * factor
        over = max(0.0, abs(air_err) - allowed)
        score -= min(20.0, over / 11.0)
        if over == 0.0 and engine_demand > 20.0:
            score += 2.0

    if rpm is not None and rpm > 1000.0 and engine_demand > 18.0:
        if egr_err is not None:
            allowed = max(8.0, abs(egr_target or 0.0) * 0.11) * factor
            score -= min(7.0, max(0.0, abs(egr_err) - allowed) / 3.8)
        if mix_err is not None:
            allowed = max(8.0, abs(mix_target or 0.0) * 0.11) * factor
            score -= min(7.0, max(0.0, abs(mix_err) - allowed) / 3.8)
    return clamp(score, 0.0, 100.0)


def score_strain(snapshot: TelemetrySnapshot, engine_demand: float) -> float:
    rpm = _value(snapshot, "rpm")
    abs_load = _value(snapshot, "absLoadProxy") or 0.0
    dpf_diff = _value(snapshot, "dpfDiffProxy")
    speed = _value(snapshot, "speed_mph") or 0.0
    state = (snapshot.drivingState or "unknown").lower()
    gear = (snapshot.gear or "--").upper()

    score = 98.0
    if state == "coasting" and speed > 8.0:
        score += 2.0

    threshold = low_rpm_threshold(gear)
    if rpm is not None and state in {"lugging", "reverse-lugging", "low-rpm-demand", "reverse-load"}:
        score -= min(
            50.0,
            max(0.0, threshold - rpm) / 9.0 + max(0.0, engine_demand - 28.0) * 0.60,
        )
        if state in {"lugging", "reverse-lugging"}:
            score -= 12.0

    score -= max(0.0, engine_demand - 25.0) * 0.28
    score -= max(0.0, abs_load - 65.0) * 0.18

    if rpm is not None and state == "engine-braking" and rpm > 3000.0:
        score -= min(16.0, 4.0 + (rpm - 3000.0) / 80.0)
    elif rpm is not None and rpm > 3700.0:
        score -= min(35.0, 15.0 + (rpm - 3700.0) / 25.0)
    elif rpm is not None and rpm > 3400.0 and engine_demand < 35.0:
        score -= min(18.0, (rpm - 3400.0) / 35.0)

    if dpf_diff is not None and dpf_diff > 150.0:
        score -= min(24.0, (dpf_diff - 150.0) / 5.0)
    if state in {"lugging", "reverse-lugging"}:
        score = min(score, 42.0)
    return clamp(score, 0.0, 100.0)


def score_delivery(snapshot: TelemetrySnapshot, engine_demand: float) -> float:
    boost_err = _value(snapshot, "boostErrorProxy")
    rail_err = _value(snapshot, "railErrorProxy")
    air_err = _value(snapshot, "airFlowError")
    boost_target = _value(snapshot, "boostTargetProxy")
    rail_target = _value(snapshot, "railTargetProxy")
    air_target = _value(snapshot, "airFlowSetting")
    egr_err = _value(snapshot, "egrError")
    egr_target = _value(snapshot, "egrTarget")
    mix_err = _value(snapshot, "airMixerError")
    mix_target = _value(snapshot, "airMixerTarget")
    rpm = _value(snapshot, "rpm")
    state = (snapshot.drivingState or "unknown").lower()

    score = 96.0
    factor = _demand_tolerance_factor(engine_demand)

    if boost_err is not None:
        allowed = max(80.0, abs(boost_target or 0.0) * 0.115) * factor
        score -= min(30.0, max(0.0, abs(boost_err) - allowed) / 8.5)
    if rail_err is not None:
        allowed = max(48.0, abs(rail_target or 0.0) * 0.048) * factor
        score -= min(25.0, max(0.0, abs(rail_err) - allowed) / 6.5)
    if air_err is not None:
        allowed = max(58.0, abs(air_target or 0.0) * 0.115) * factor
        score -= min(21.0, max(0.0, abs(air_err) - allowed) / 10.5)

    if rpm is not None and rpm > 1000.0 and engine_demand > 18.0:
        if egr_err is not None:
            allowed = max(9.0, abs(egr_target or 0.0) * 0.12) * factor
            score -= min(8.0, max(0.0, abs(egr_err) - allowed) / 3.5)
        if mix_err is not None:
            allowed = max(9.0, abs(mix_target or 0.0) * 0.12) * factor
            score -= min(8.0, max(0.0, abs(mix_err) - allowed) / 3.5)

    if state in {"lugging", "reverse-lugging"}:
        score = min(score, 66.0)
    return clamp(score, 0.0, 100.0)


def _target_rpm(engine_demand: float) -> float:
    demand = clamp(engine_demand, 0.0, 100.0)
    if demand <= 15.0:
        return 1450.0 + demand * 4.0
    if demand <= 40.0:
        return 1510.0 + (demand - 15.0) * 12.0
    if demand <= 70.0:
        return 1810.0 + (demand - 40.0) * 14.0
    return min(2470.0, 2230.0 + (demand - 70.0) * 8.0)


def derive_guidance(snapshot: TelemetrySnapshot, engine_demand: float) -> tuple[float | None, str]:
    if not snapshot.telemetryValid or (snapshot.scoreConfidence or 0.0) < 85.0:
        return None, "telemetry_incomplete"

    rpm = _value(snapshot, "rpm")
    speed = _value(snapshot, "speed_mph") or 0.0
    gear = (snapshot.gear or "--").upper()
    state = (snapshot.drivingState or "unknown").lower()
    if rpm is None:
        return None, "telemetry_incomplete"

    if state in {"lugging", "reverse-lugging"}:
        return 1.0, "low_rpm_high_demand"
    if state == "coasting":
        return 0.0, "coasting_ok"
    if state == "engine-braking":
        if rpm <= 2200.0:
            return 0.0, "engine_braking"
        return -min(0.65, (rpm - 2200.0) / 1800.0), "engine_braking"
    if state.startswith("neutral") or state in {"idle", "clutch", "braking", "unknown", "reversing"}:
        return 0.0, state

    numeric_gear = gear_number(gear)
    if numeric_gear is None:
        return 0.0, "gear_unavailable"

    target = _target_rpm(engine_demand)
    correction = clamp((target - rpm) / 900.0, -0.75, 0.75)
    reason = "below_target_band" if correction > 0.035 else "above_target_band" if correction < -0.035 else "matched_band"

    if numeric_gear == 6 and speed > 55.0 and correction < 0.0 and rpm < 2400.0:
        correction *= 0.30
        reason = "top_gear_cruise"

    if numeric_gear <= 2 and rpm > 2850.0:
        correction = -min(0.95, 0.45 + (rpm - 2850.0) / 1450.0)
        reason = "high_rpm_low_gear"
    elif rpm > 3300.0 and engine_demand < 35.0:
        correction = min(correction, -min(0.80, 0.35 + (rpm - 3300.0) / 1600.0))
        reason = "high_rpm_low_load"

    if state in {"low-rpm-demand", "reverse-load"}:
        correction = max(correction, 0.52)
        reason = "low_rpm_demand"

    if abs(correction) < 0.035:
        return 0.0, "matched_band"
    return clamp(correction, -1.0, 1.0), reason


def score_label(mood_score: float) -> str:
    for label, threshold in MOOD_THRESHOLDS:
        if mood_score >= threshold:
            return label
    return "upset"


def _threshold_for(label: str) -> float:
    for state, threshold in MOOD_THRESHOLDS:
        if state == label:
            return threshold
    return 0.0


def _rank(label: str) -> int:
    order = [state for state, _threshold in reversed(MOOD_THRESHOLDS)]
    try:
        return order.index(label)
    except ValueError:
        return -1


def mood_override(snapshot: TelemetrySnapshot) -> str | None:
    if not snapshot.telemetryValid:
        return "unknown"
    if snapshot.dpfStatus == "PRESSURE":
        return "DPF pressure"
    thermal = (snapshot.thermalState or "").lower()
    if "hot" in thermal:
        return "hot"
    if "lugging" in (snapshot.drivingState or "").lower():
        return "lugging"
    if "cold" in thermal:
        return "grumbling"
    if "warming" in thermal:
        return "fine"
    return None


class CalibratedTelemetryEngine(TelemetryEngine):
    """Pass-2 score/guidance calibration over the deterministic telemetry engine."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mood_state: str | None = None
        self._mood_candidate: str | None = None
        self._mood_candidate_since_s: float | None = None
        self._mood_last_s: float | None = None

    def _hysteretic_mood_state(self, mood_score: float, snapshot: TelemetrySnapshot, now_s: float) -> str:
        override = mood_override(snapshot)
        if override is not None:
            self._mood_state = None
            self._mood_candidate = None
            self._mood_candidate_since_s = None
            self._mood_last_s = now_s
            return override

        desired = score_label(mood_score)
        if self._mood_state is None:
            self._mood_state = desired
            self._mood_last_s = now_s
            return desired

        if self._mood_last_s is not None and now_s < self._mood_last_s:
            self._mood_candidate = None
            self._mood_candidate_since_s = None
        self._mood_last_s = now_s

        if desired == self._mood_state:
            self._mood_candidate = None
            self._mood_candidate_since_s = None
            return self._mood_state

        current_rank = _rank(self._mood_state)
        desired_rank = _rank(desired)
        if desired_rank > current_rank:
            permitted = mood_score >= _threshold_for(desired) + MOOD_UP_HYSTERESIS
        else:
            permitted = mood_score < _threshold_for(self._mood_state) - MOOD_DOWN_HYSTERESIS
        if not permitted:
            self._mood_candidate = None
            self._mood_candidate_since_s = None
            return self._mood_state

        if self._mood_candidate != desired:
            self._mood_candidate = desired
            self._mood_candidate_since_s = now_s
            return self._mood_state
        if self._mood_candidate_since_s is None or now_s - self._mood_candidate_since_s < MOOD_STATE_DWELL_S:
            return self._mood_state

        self._mood_state = desired
        self._mood_candidate = None
        self._mood_candidate_since_s = None
        return self._mood_state

    def process(self, canonical: dict[str, object], **kwargs) -> TelemetrySnapshot:
        snapshot = super().process(canonical, **kwargs)
        engine_demand = derive_engine_demand(snapshot)

        if not snapshot.telemetryValid or engine_demand is None:
            return replace(snapshot, engineDemandProxy=engine_demand)

        eff_operating = score_operating_zone(snapshot, engine_demand)
        eff_load = score_load_efficiency(snapshot, engine_demand)
        eff_thermal = snapshot.effThermal
        eff_flow = score_flow_efficiency(snapshot, engine_demand)
        efficiency = weighted_average(
            [
                (eff_operating, 1.0),
                (eff_load, 1.6),
                (eff_thermal, 0.7),
                (eff_flow, 0.8),
            ],
            default=0.0,
        )

        mood_thermal = snapshot.moodThermalComfort
        mood_strain = score_strain(snapshot, engine_demand)
        mood_delivery = score_delivery(snapshot, engine_demand)
        mood_electrical = snapshot.moodElectrical
        mood_score = weighted_average(
            [
                (mood_thermal, 1.0),
                (mood_strain, 1.4),
                (mood_delivery, 1.2),
                (mood_electrical, 0.4),
            ],
            default=0.0,
        )

        guidance, guidance_reason = derive_guidance(snapshot, engine_demand)
        source_time = kwargs.get("source_time_s")
        now_s = float(source_time) if isinstance(source_time, (int, float)) else time.monotonic()
        mood_state = self._hysteretic_mood_state(mood_score, snapshot, now_s)

        return replace(
            snapshot,
            engineDemandProxy=engine_demand,
            efficiencyScore=efficiency,
            effOperatingZone=eff_operating,
            effLoad=eff_load,
            effThermal=eff_thermal,
            effFlow=eff_flow,
            moodScore=mood_score,
            moodThermalComfort=mood_thermal,
            moodStrain=mood_strain,
            moodDelivery=mood_delivery,
            moodElectrical=mood_electrical,
            moodState=mood_state,
            guidanceCorrection=guidance,
            guidanceReason=guidance_reason,
        )
