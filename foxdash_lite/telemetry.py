from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Display-facing telemetry snapshot.

    This is deliberately decoded / UI-ready data, not raw OBD bytes. The OBD
    worker owns serial polling, decoding, and logging; the UI and LEDs consume
    this compact, already-interpreted state. There is one score brain, because
    two competing ones would be how a dashboard becomes folklore.
    """

    timestamp: str
    sample: int

    # Runtime/session identity. These are session-scoped, never lifetime totals.
    sessionId: str = ""
    bootId: str = ""
    sessionStartedAt: str = ""
    firstValidSampleAt: str = ""

    obdConnection: str = "simulated"
    adapterState: str = "desktop/demo"
    ecuSessionState: str = "engine/SID807"
    protocol: str = "sim"
    pollHealth: str = "100%"
    sampleRateHz: Optional[float] = None
    lastUpdateAge_s: float = 0.0

    # Interpretation confidence. Scores remain unavailable rather than looking
    # deceptively ordinary when their core inputs are missing.
    telemetryValid: bool = False
    scoreConfidence: Optional[float] = None
    scoreReason: str = "waiting for core telemetry"
    driveStateConfidence: Optional[float] = None

    dpfSoot: Optional[float] = None
    dpfStatus: str = "STABLE"
    dpfTrendArrow: str = "→"

    rpm: Optional[float] = None
    speed_mph: Optional[float] = None
    gear: str = "N"
    drivingState: str = "idle"
    guidanceCorrection: Optional[float] = None  # -1: less engine speed, +1: more
    guidanceReason: str = "unknown"

    coolant: Optional[float] = None
    oilTemp: Optional[float] = None
    fuelTemp: Optional[float] = None
    airFlowSensorTemp: Optional[float] = None
    airManifoldTemp: Optional[float] = None
    externalTemp: Optional[float] = None
    intakeTemp: Optional[float] = None
    ambientTemp: Optional[float] = None
    engineTempProxy: Optional[float] = None
    heatSoakProxy: Optional[float] = None
    thermalMaxProxy: Optional[float] = None
    thermalState: str = "unknown"

    mapProxy: Optional[float] = None
    boostProxy: Optional[float] = None
    boostTargetProxy: Optional[float] = None
    boostErrorProxy: Optional[float] = None
    railProxy: Optional[float] = None
    railTargetProxy: Optional[float] = None
    railErrorProxy: Optional[float] = None
    baroProxy: Optional[float] = None
    dpfDiffProxy: Optional[float] = None

    pedalProxy: Optional[float] = None
    loadProxy: Optional[float] = None
    absLoadProxy: Optional[float] = None
    relativeAccel_mps2: Optional[float] = None
    relativeAccel_g: Optional[float] = None
    relativeAccelState: str = "steady"
    relativeAccelSessionMin_mps2: Optional[float] = None
    relativeAccelSessionMax_mps2: Optional[float] = None

    injFlow: Optional[float] = None
    fuelFlowReg: Optional[float] = None
    airFlowSetting: Optional[float] = None
    airFlowMeasured: Optional[float] = None
    airFlowError: Optional[float] = None
    egrTarget: Optional[float] = None
    egrActual: Optional[float] = None
    egrError: Optional[float] = None
    airMixerTarget: Optional[float] = None
    airMixerActual: Optional[float] = None
    airMixerError: Optional[float] = None
    turboGeomTarget: Optional[float] = None
    turboGeomActual: Optional[float] = None
    turboGeomError: Optional[float] = None

    batteryV: Optional[float] = None
    airCPress_bar: Optional[float] = None
    airCPressSessionMin_bar: Optional[float] = None
    airCPressSessionMax_bar: Optional[float] = None
    fapTemp: Optional[float] = None
    fapDiffPressure: Optional[float] = None
    lastRegen_mi: Optional[float] = None
    avg10Regen_mi: Optional[float] = None
    fapLifeLeft_mi: Optional[float] = None
    fapAdditiveVol: Optional[float] = None
    fapAdditiveRemain: Optional[float] = None
    fapAdditivePercent: Optional[float] = None

    inj1FlowCorr: Optional[float] = None
    inj2FlowCorr: Optional[float] = None
    inj3FlowCorr: Optional[float] = None
    inj4FlowCorr: Optional[float] = None

    efficiencyScore: Optional[float] = None
    effOperatingZone: Optional[float] = None
    effLoad: Optional[float] = None
    effThermal: Optional[float] = None
    effFlow: Optional[float] = None

    moodScore: Optional[float] = None
    moodThermalComfort: Optional[float] = None
    moodStrain: Optional[float] = None
    moodDelivery: Optional[float] = None
    moodElectrical: Optional[float] = None
    moodState: str = "content"

    # Low-rate BH1750 environment readings, copied into each UI telemetry row
    # for convenient correlation with a drive. The authoritative every-sample
    # record remains the separate ambient-light CSV.
    ambientLuxRaw: Optional[float] = None
    ambientLuxFiltered: Optional[float] = None
    ambientLightSensorOk: bool = False
    ambientLightState: str = "unavailable"


DISPLAY_FIELD_NAMES = [
    "timestamp", "sample",
    "sessionId", "bootId", "sessionStartedAt", "firstValidSampleAt",
    "obdConnection", "adapterState", "ecuSessionState", "protocol", "pollHealth", "sampleRateHz", "lastUpdateAge_s",
    "telemetryValid", "scoreConfidence", "scoreReason", "driveStateConfidence",
    "dpfSoot", "dpfStatus", "dpfTrendArrow",
    "rpm", "speed_mph", "gear", "drivingState", "guidanceCorrection", "guidanceReason",
    "coolant", "oilTemp", "fuelTemp", "airFlowSensorTemp", "airManifoldTemp", "externalTemp", "intakeTemp", "ambientTemp", "engineTempProxy", "heatSoakProxy", "thermalMaxProxy", "thermalState",
    "mapProxy", "boostProxy", "boostTargetProxy", "boostErrorProxy", "railProxy", "railTargetProxy", "railErrorProxy", "baroProxy", "dpfDiffProxy",
    "pedalProxy", "loadProxy", "absLoadProxy", "relativeAccel_mps2", "relativeAccel_g", "relativeAccelState", "relativeAccelSessionMin_mps2", "relativeAccelSessionMax_mps2",
    "injFlow", "fuelFlowReg", "airFlowSetting", "airFlowMeasured", "airFlowError", "egrTarget", "egrActual", "egrError", "airMixerTarget", "airMixerActual", "airMixerError", "turboGeomTarget", "turboGeomActual", "turboGeomError",
    "batteryV", "airCPress_bar", "airCPressSessionMin_bar", "airCPressSessionMax_bar", "fapTemp", "fapDiffPressure", "lastRegen_mi", "avg10Regen_mi", "fapLifeLeft_mi", "fapAdditiveVol", "fapAdditiveRemain", "fapAdditivePercent",
    "inj1FlowCorr", "inj2FlowCorr", "inj3FlowCorr", "inj4FlowCorr",
    "efficiencyScore", "effOperatingZone", "effLoad", "effThermal", "effFlow",
    "moodScore", "moodThermalComfort", "moodStrain", "moodDelivery", "moodElectrical", "moodState",
    "ambientLuxRaw", "ambientLuxFiltered", "ambientLightSensorOk", "ambientLightState",
]
