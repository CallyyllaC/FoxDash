from __future__ import annotations

"""PSA SID807 raw-memory protocol definitions and decoder.

This module intentionally knows how bytes become named values. It does *not*
open serial ports and it does *not* write session files. Those jobs belong to
``obd_reader`` and ``telemetry_logger`` respectively, because one creature
should not be allowed to be the serial driver, accountant and dashboard god.
"""

import csv
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .log_format import CompactDictWriter

# =============================================================================
# FIELD MAP
# =============================================================================

KM_TO_MILES = 0.621371

GEAR_MAP = {
    0: "N",
    1: "1",
    2: "2",
    3: "3",
    4: "4",
    5: "5",
    6: "6",
    9: "R",
}


class Confidence:
    """Stable confidence labels written to forensic CSV output."""

    CONFIRMED = "confirmed"
    CONFIRMED_IDENTITY = "confirmed_identity"
    PROVISIONAL = "provisional"
    CANDIDATE = "candidate"
    UNKNOWN = "unknown"


REGEN_STATUS_MAP = {
    0x00: "engine stopped, startup or status inactive/unavailable",
    0x10: "engine running, no active regeneration",
    0x1C: "active DPF regeneration",
}


ENGINE_FUELLING_MODE_MAP = {
    0: "unknown transient",
    1: "no fuelling / engine stopped",
    2: "overrun, fuel-cut or low-fuelling transition",
    3: "normal fuelling",
    4: "high-load fuelling",
    5: "engine-start or cranking handover",
}


@dataclass(frozen=True)
class FieldDef:
    name: str
    command: str
    offset: int
    length: int
    raw_type: str
    scale: float = 1.0
    add: float = 0.0
    unit: str = ""
    confidence: str = "confirmed"
    fap_label: str = ""
    description: str = ""
    bit_index: Optional[int] = None
    mask: Optional[int] = None
    any_mask: bool = False
    enum_map: Optional[Dict[int, str]] = None
    equals_value: Optional[int] = None
    unavailable_raw_values: Tuple[int, ...] = ()
    diagnostic_format: str = ""


FIELD_MAP: List[FieldDef] = [
    # -------------------------------------------------------------------------
    # 21C98001: fuel / rail / injection block
    # -------------------------------------------------------------------------
    FieldDef("RPM", "21C98001", 0, 2, "u16be", unit="rpm", fap_label="Revs"),
    FieldDef("Speed", "21C98001", 2, 1, "u8", scale=KM_TO_MILES, unit="mph", fap_label="Speed"),
    FieldDef("DriverDemandMirror_C9", "21C98001", 3, 1, "u8", unit="%", confidence=Confidence.CONFIRMED, fap_label="Driver demand mirror C9", description="Confirmed redundant accelerator-demand representation; canonical pedal is 21CC8001 offset 8."),
    FieldDef("EngineFuellingModeRaw", "21C98001", 4, 1, "u8", confidence=Confidence.CONFIRMED_IDENTITY, fap_label="Engine fuelling mode raw", description="Raw engine-fuelling mode identity; official PSA state meanings are not established."),
    FieldDef("EngineFuellingMode", "21C98001", 4, 1, "enum", confidence=Confidence.PROVISIONAL, fap_label="Engine fuelling mode (provisional)", enum_map=ENGINE_FUELLING_MODE_MAP, description="Observed working interpretations only. Mode 2 alone must not be used to derive fuel cut."),
    FieldDef("FuelFlowReg", "21C98001", 9, 1, "u8", unit="%", fap_label="FuelFlowReg"),
    FieldDef("FuelPressInstr", "21C98001", 10, 2, "u16be", unit="bar", fap_label="FuelPressInstr"),
    FieldDef("FuelPress", "21C98001", 12, 2, "u16be", unit="bar", fap_label="FuelPress"),
    FieldDef("InjectedFlowMeasured", "21C98001", 14, 2, "u16be", scale=0.1, unit="mg/str?", confidence="confirmed", fap_label="InjFlow"),
    # Injector flow correction values matched against FAP idle display scale.
    # Stored by ECU as unsigned 16-bit values centred on 0x8000.
    # Formula locked for final Windows debug test:
    #   display = (raw_u16 - 32768) / 512
    # Unit remains intentionally blank: this is FAP-display-equivalent correction, not proven %.
    FieldDef("Inj1FlowCorr", "21C98001", 16, 2, "u16be", scale=1.0 / 512.0, add=-32768.0 / 512.0, unit="", confidence=Confidence.CONFIRMED_IDENTITY, fap_label="Injector 1 flow correction", description="Relative cylinder correction centred at 0x8000 and scaled /512; exact physical unit is unproven."),
    FieldDef("Inj2FlowCorr", "21C98001", 18, 2, "u16be", scale=1.0 / 512.0, add=-32768.0 / 512.0, unit="", confidence=Confidence.CONFIRMED_IDENTITY, fap_label="Injector 2 flow correction", description="Relative cylinder correction centred at 0x8000 and scaled /512; exact physical unit is unproven."),
    FieldDef("Inj3FlowCorr", "21C98001", 20, 2, "u16be", scale=1.0 / 512.0, add=-32768.0 / 512.0, unit="", confidence=Confidence.CONFIRMED_IDENTITY, fap_label="Injector 3 flow correction", description="Relative cylinder correction centred at 0x8000 and scaled /512; exact physical unit is unproven."),
    FieldDef("Inj4FlowCorr", "21C98001", 22, 2, "u16be", scale=1.0 / 512.0, add=-32768.0 / 512.0, unit="", confidence=Confidence.CONFIRMED_IDENTITY, fap_label="Injector 4 flow correction", description="Relative cylinder correction centred at 0x8000 and scaled /512; exact physical unit is unproven."),
    FieldDef("FuelTemp", "21C98001", 28, 1, "u8", add=-50.0, unit="°C", fap_label="FuelTemp"),
    FieldDef("InjCntrlV", "21C98001", 29, 1, "u8", unit="V-ish", confidence=Confidence.CANDIDATE, fap_label="InjCntrlV"),
    # Forum/Diagbox names these as injection time fields at AG:AH, AI:AJ, AK:AL, AM:AN.
    # The cylinder order in the source is deliberately 1, 3, 4, 2. Captured
    # idle/load behaviour establishes milliseconds with raw / 1000 scaling.
    FieldDef("InjectionDurationCyl1", "21C98001", 32, 2, "u16be", scale=0.001, unit="ms", confidence=Confidence.CONFIRMED, fap_label="Injection duration cylinder 1"),
    FieldDef("InjectionDurationCyl3", "21C98001", 34, 2, "u16be", scale=0.001, unit="ms", confidence=Confidence.CONFIRMED, fap_label="Injection duration cylinder 3"),
    FieldDef("InjectionDurationCyl4", "21C98001", 36, 2, "u16be", scale=0.001, unit="ms", confidence=Confidence.CONFIRMED, fap_label="Injection duration cylinder 4"),
    FieldDef("InjectionDurationCyl2", "21C98001", 38, 2, "u16be", scale=0.001, unit="ms", confidence=Confidence.CONFIRMED, fap_label="Injection duration cylinder 2"),

    # -------------------------------------------------------------------------
    # 21CA8001: air / turbo / EGR block
    # -------------------------------------------------------------------------
    FieldDef("RPM", "21CA8001", 0, 2, "u16be", unit="rpm", fap_label="Revs"),
    FieldDef("Speed", "21CA8001", 2, 1, "u8", scale=KM_TO_MILES, unit="mph", fap_label="Speed"),
    FieldDef("DriverDemandMirror_CA", "21CA8001", 3, 1, "u8", unit="%", confidence=Confidence.CONFIRMED, fap_label="Driver demand mirror CA", description="Confirmed redundant accelerator-demand representation; canonical pedal is 21CC8001 offset 8."),
    FieldDef("AirFlowSensorTemp", "21CA8001", 4, 1, "u8", add=-50.0, unit="°C", fap_label="AirFlowSensor"),
    FieldDef("AirManifoldTemp", "21CA8001", 5, 1, "u8", add=-50.0, unit="°C", fap_label="AirManifold"),
    FieldDef("TurboInstr", "21CA8001", 6, 2, "u16be", unit="mbar", fap_label="TurboInstr"),
    FieldDef("TurboPress", "21CA8001", 8, 2, "u16be", unit="mbar", fap_label="TurboPress"),
    FieldDef("AtmosphPress", "21CA8001", 10, 2, "u16be", unit="mbar", fap_label="AtmosphPress"),
    FieldDef("TurboVaneTarget_pct", "21CA8001", 12, 1, "u8", unit="%", confidence=Confidence.CONFIRMED, fap_label="Turbo vane target"),
    FieldDef("TurboVaneActual_pct", "21CA8001", 13, 1, "u8", unit="%", confidence=Confidence.CONFIRMED, fap_label="Turbo vane feedback"),
    FieldDef("AirMixerTarget_pct", "21CA8001", 16, 1, "u8", unit="%", confidence=Confidence.CONFIRMED, fap_label="Air mixer target"),
    FieldDef("AirMixerActual_pct", "21CA8001", 17, 1, "u8", unit="%", confidence=Confidence.CONFIRMED, fap_label="Air mixer feedback"),
    FieldDef("EGRposInstr", "21CA8001", 20, 1, "u8", unit="%", fap_label="EGRposInstr"),
    FieldDef("EGRpos", "21CA8001", 21, 1, "u8", unit="%", fap_label="EGRpos"),
    FieldDef("EGRControlOutput", "21CA8001", 22, 2, "u16be", scale=0.1, add=-100.0, unit="", confidence=Confidence.CONFIRMED_IDENTITY, fap_label="EGR controller output", description="Signed EGR controller output; not a conventional unsigned PWM duty percentage."),
    FieldDef("EGRCoolerBypassTarget", "21CA8001", 24, 1, "u8", unit="%", confidence=Confidence.CONFIRMED, fap_label="EGR cooler bypass target", description="Observed binary percentage command using 0 and 100."),
    FieldDef("EGRCoolerBypassRepeat", "21CA8001", 25, 1, "u8", unit="%", confidence=Confidence.CANDIDATE, fap_label="EGR cooler bypass repeat", description="Unresolved; remained zero throughout the captured dataset."),
    # Corrected adjacent-pair candidate from the Diagbox/forum air-mixer electrovalve line.
    # Literal AG:AF from the forum table is suspicious; AH:AI / offset 33 behaved plausibly in our logs.
    FieldDef("AirMixerElectrovalve", "21CA8001", 33, 2, "u16be", scale=0.1, add=-100.0, unit="%", confidence=Confidence.CANDIDATE, fap_label="Air mixer electrovalve"),
    FieldDef("ExternalTemp", "21CA8001", 32, 1, "u8", add=-50.0, unit="°C", fap_label="ExternalTemp"),
    FieldDef("TurboValve", "21CA8001", 35, 1, "u8", unit="%", confidence=Confidence.CANDIDATE, fap_label="TurboValve", description="Separate unresolved turbo-control signal; not established as vane target or feedback."),
    FieldDef("AirMassTarget", "21CA8001", 37, 2, "u16be", unit="mg/imp", confidence=Confidence.CONFIRMED, fap_label="Air mass target"),
    FieldDef("AirMassMeasured", "21CA8001", 39, 2, "u16be", unit="mg/imp", confidence=Confidence.CONFIRMED, fap_label="Air mass measured"),

    # -------------------------------------------------------------------------
    # 21CB8001: FAP / DPF / additive / distance block
    # -------------------------------------------------------------------------
    FieldDef("RPM", "21CB8001", 0, 2, "u16be", unit="rpm", fap_label="Revs"),
    FieldDef("Speed", "21CB8001", 2, 1, "u8", scale=KM_TO_MILES, unit="mph", fap_label="Speed"),
    FieldDef("O2mixture", "21CB8001", 4, 2, "u16be", scale=0.001, fap_label="O2mixture"),
    FieldDef("O2heating", "21CB8001", 6, 1, "u8", unit="%", confidence=Confidence.CANDIDATE, fap_label="O2heating"),
    FieldDef("O2SensorSignal_mV", "21CB8001", 7, 2, "u16be", unit="mV", confidence=Confidence.CONFIRMED, fap_label="Oxygen sensor signal", description="Big-endian millivolt signal; separate from O2mixture and not converted to AFR."),
    FieldDef("FAPsoot", "21CB8001", 9, 2, "u16be", scale=0.001, fap_label="FAPsoot"),
    FieldDef("FAPcinder", "21CB8001", 11, 1, "u8", fap_label="FAPcinder"),
    FieldDef("FAPlifeLeft", "21CB8001", 12, 3, "u24be", scale=KM_TO_MILES, unit="mi", fap_label="FAPlifeLeft", description="Corrected to INT24; old offset 13-14 only was incomplete."),
    FieldDef("FAPDiffPressure", "21CB8001", 15, 2, "u16be", unit="mbar", fap_label="FAPpressure"),
    FieldDef("FAPAdditiveVol", "21CB8001", 17, 2, "u16be", unit="mL", fap_label="FAPAdditiveVol"),
    FieldDef("FAPAdditiveRemain", "21CB8001", 19, 2, "u16be", unit="mL", fap_label="FAPAdditiveRemain"),
    FieldDef("FAPdeposits", "21CB8001", 21, 2, "u16be", fap_label="FAPdeposits"),
    FieldDef("FAPTempAlt", "21CB8001", 23, 2, "u16be", unit="°C", confidence=Confidence.CONFIRMED_IDENTITY, fap_label="DPF temperature alternate", description="Alternate DPF temperature identity; physical sensor location is unproven."),
    FieldDef("FAPtemp", "21CB8001", 25, 2, "u16be", unit="°C", fap_label="FAPtemp"),
    FieldDef("RegenStatusABRaw", "21CB8001", 27, 1, "u8", confidence=Confidence.CONFIRMED, fap_label="DPF regeneration status raw", diagnostic_format="hex"),
    FieldDef("RegenStatus", "21CB8001", 27, 1, "enum", confidence=Confidence.CONFIRMED, fap_label="DPF regeneration status", enum_map=REGEN_STATUS_MAP, description="Only observed states 0x00, 0x10 and 0x1C are mapped."),
    FieldDef("ActiveRegeneration", "21CB8001", 27, 1, "equals", confidence=Confidence.CONFIRMED, fap_label="Active DPF regeneration", equals_value=0x1C, description="True only when RegenStatusABRaw equals 0x1C exactly."),
    FieldDef("PowerConsumersRegenAuth", "21CB8001", 27, 1, "bitmask", mask=0x60, any_mask=True, confidence=Confidence.CANDIDATE, fap_label="Power consumers / regen auth bits5&6"),
    FieldDef("LastRegen", "21CB8001", 28, 2, "u16be", scale=KM_TO_MILES, unit="mi", fap_label="LastRegen", description="Corrected to INT16; byte 29 alone is only the low byte."),
    FieldDef("Avg10regen", "21CB8001", 30, 2, "u16be", scale=KM_TO_MILES, unit="mi", fap_label="Avg10regen"),
    FieldDef("ShortTermRegenCapacity", "21CB8001", 32, 1, "u8", unit="%?", confidence="candidate", fap_label="Short term regen capacity"),
    FieldDef("LongTermRegenCapacity", "21CB8001", 33, 1, "u8", unit="%?", confidence="candidate", fap_label="Long term regen capacity"),
    FieldDef("FAPdistance", "21CB8001", 34, 3, "u24be", scale=KM_TO_MILES, unit="mi", fap_label="FAP life / distance"),

    # -------------------------------------------------------------------------
    # 21CC8001: pedals / gear / flags / battery / supply voltages
    # -------------------------------------------------------------------------
    FieldDef("RPM", "21CC8001", 0, 2, "u16be", unit="rpm", fap_label="Revs"),
    FieldDef("Speed", "21CC8001", 2, 1, "u8", scale=KM_TO_MILES, unit="mph", fap_label="Speed"),
    FieldDef("StatusRaw", "21CC8001", 3, 1, "u8", confidence="likely", fap_label="status byte"),
    FieldDef("CL", "21CC8001", 3, 1, "bit", bit_index=0, fap_label="CL"),
    FieldDef("BR", "21CC8001", 3, 1, "bitmask", mask=0x06, any_mask=True, fap_label="BR"),
    FieldDef("PedalSensorV1", "21CC8001", 4, 2, "u16be", scale=0.001, unit="V", fap_label="Accel pedal V1"),
    FieldDef("PedalSensorV2", "21CC8001", 6, 2, "u16be", scale=0.001, unit="V", fap_label="Accel pedal V2"),
    FieldDef("AccelPedalPos", "21CC8001", 8, 1, "u8", unit="%", fap_label="AccelPedalPos"),
    FieldDef("Gear", "21CC8001", 9, 1, "enum", enum_map=GEAR_MAP, fap_label="Gear"),
    FieldDef("GearRaw", "21CC8001", 9, 1, "u8", fap_label="Gear raw"),
    FieldDef("AlternatorProgressiveChargeRef", "21CC8001", 13, 1, "u8", unit="%?", confidence=Confidence.CANDIDATE, fap_label="Alternator charge reference", unavailable_raw_values=(0xFF,), description="Raw 0xFF is an unavailable sentinel, not 255%."),
    FieldDef("Battery", "21CC8001", 15, 1, "u8", scale=0.1, unit="V", fap_label="Battery"),
    FieldDef("Supply5V_1", "21CC8001", 16, 2, "u16be", scale=0.001, unit="V", confidence="confirmed", fap_label="5V supply 1"),
    FieldDef("Supply5V_2", "21CC8001", 18, 2, "u16be", scale=0.001, unit="V", confidence=Confidence.CANDIDATE, fap_label="5V supply 2"),
    FieldDef("Supply5V_3", "21CC8001", 20, 2, "u16be", scale=0.001, unit="V", confidence=Confidence.CANDIDATE, fap_label="5V supply 3"),
    FieldDef("FilteredAmbientTemp_C", "21CC8001", 22, 1, "u8", scale=0.25, unit="°C", confidence=Confidence.PROVISIONAL, fap_label="Filtered ambient temperature", description="Strong likely filtered ambient temperature; identity awaits external or controlled confirmation."),
    FieldDef("OIL", "21CC8001", 23, 1, "bit", bit_index=1, fap_label="OIL"),
    FieldDef("ClutchPedalPosition_pct", "21CC8001", 25, 1, "u8", unit="%", confidence=Confidence.CONFIRMED, fap_label="Clutch pedal position"),
    FieldDef("CoolingThermalStateRaw", "21CC8001", 27, 1, "u8", confidence=Confidence.CONFIRMED_IDENTITY, fap_label="Cooling thermal state raw", diagnostic_format="hex", description="Observed states: 0x01, 0x05, 0x09 and 0x0D; individual meanings remain unresolved."),

    # -------------------------------------------------------------------------
    # 21CD8001: coolant / A/C / oil block
    # -------------------------------------------------------------------------
    FieldDef("RPM", "21CD8001", 0, 2, "u16be", unit="rpm", fap_label="Revs"),
    FieldDef("Speed", "21CD8001", 2, 1, "u8", scale=KM_TO_MILES, unit="mph", fap_label="Speed"),
    FieldDef("Coolant", "21CD8001", 3, 1, "u8", add=-50.0, unit="°C", fap_label="Coolant"),
    FieldDef("CoolingFanDemandRaw", "21CD8001", 4, 1, "u8", confidence=Confidence.CONFIRMED_IDENTITY, fap_label="Cooling fan demand raw", description="Raw demand identity; scale is not yet proven to be percent."),
    FieldDef("CoolingFanStatusRaw", "21CD8001", 5, 1, "u8", confidence=Confidence.CONFIRMED_IDENTITY, fap_label="Cooling fan status raw", diagnostic_format="hex", description="Observed values 0x00 and 0x29; individual bit meanings are unresolved."),
    FieldDef("CoolingFanActive", "21CD8001", 7, 1, "bool", confidence=Confidence.CONFIRMED, fap_label="Cooling fan active", description="Confirmed boolean state; matched status raw 0x29 in aligned captured samples."),
    FieldDef("AirCPress", "21CD8001", 10, 1, "u8", scale=0.1, unit="bar", fap_label="AirCPress"),
    FieldDef("OilPressureSwitch", "21CD8001", 11, 1, "u8", confidence="confirmed", fap_label="OIL"),
    FieldDef("OilLevelSensorV", "21CD8001", 13, 2, "u16be", scale=0.001, unit="V", confidence="candidate", fap_label="Oil level sensor"),
]

# Raw decoded-name aliases for readers and saved forensic configurations that
# predate the evidence-backed terminology. FIELD_MAP remains canonical so
# final definitions and change logs do not duplicate the same byte range.
FIELD_NAME_ALIASES = {
    "InjectionTimeCyl1": "InjectionDurationCyl1",
    "InjectionTimeCyl3": "InjectionDurationCyl3",
    "InjectionTimeCyl4": "InjectionDurationCyl4",
    "InjectionTimeCyl2": "InjectionDurationCyl2",
    "TurboGeomInstr": "TurboVaneTarget_pct",
    "TurboVarGeom": "TurboVaneActual_pct",
    "AirMixerInstr": "AirMixerTarget_pct",
    "AirMixer": "AirMixerActual_pct",
    "EGRCommandDuty": "EGRControlOutput",
    "AirFlowSetting": "AirMassTarget",
    "AirFlowMeasured": "AirMassMeasured",
    "FAPtempA": "FAPTempAlt",
}

# Canonical values to include in the wide decoded CSV and final FAP-compare CSV.
CANONICAL_FIELDS = [
    ("rpm", "21CC8001", "RPM"),
    ("speed_mph", "21CC8001", "Speed"),
    ("gear", "21CC8001", "Gear"),
    ("gearRaw", "21CC8001", "GearRaw"),
    ("CL", "21CC8001", "CL"),
    ("BR", "21CC8001", "BR"),
    ("accelPedal", "21CC8001", "AccelPedalPos"),
    ("driverDemandMirrorC9", "21C98001", "DriverDemandMirror_C9"),
    ("driverDemandMirrorCA", "21CA8001", "DriverDemandMirror_CA"),
    ("pedalV1", "21CC8001", "PedalSensorV1"),
    ("pedalV2", "21CC8001", "PedalSensorV2"),
    ("batteryV", "21CC8001", "Battery"),
    ("alternatorProgressiveChargeRef", "21CC8001", "AlternatorProgressiveChargeRef"),
    ("filteredAmbientTemp_C", "21CC8001", "FilteredAmbientTemp_C"),
    ("clutchPedalPosition_pct", "21CC8001", "ClutchPedalPosition_pct"),
    ("coolingThermalStateRaw", "21CC8001", "CoolingThermalStateRaw"),
    ("coolant", "21CD8001", "Coolant"),
    ("coolingFanDemandRaw", "21CD8001", "CoolingFanDemandRaw"),
    ("coolingFanStatusRaw", "21CD8001", "CoolingFanStatusRaw"),
    ("coolingFanActive", "21CD8001", "CoolingFanActive"),
    ("airCPress_bar", "21CD8001", "AirCPress"),
    ("oilPressureSwitch", "21CD8001", "OilPressureSwitch"),
    ("oilLevelSensorV", "21CD8001", "OilLevelSensorV"),
    ("fuelTemp", "21C98001", "FuelTemp"),
    ("fuelFlowReg", "21C98001", "FuelFlowReg"),
    ("engineFuellingModeRaw", "21C98001", "EngineFuellingModeRaw"),
    ("engineFuellingMode", "21C98001", "EngineFuellingMode"),
    ("fuelRailTarget_bar", "21C98001", "FuelPressInstr"),
    ("fuelRailMeasured_bar", "21C98001", "FuelPress"),
    ("injFlow", "21C98001", "InjectedFlowMeasured"),
    ("inj1FlowCorr", "21C98001", "Inj1FlowCorr"),
    ("inj2FlowCorr", "21C98001", "Inj2FlowCorr"),
    ("inj3FlowCorr", "21C98001", "Inj3FlowCorr"),
    ("inj4FlowCorr", "21C98001", "Inj4FlowCorr"),
    ("injCtrlV", "21C98001", "InjCntrlV"),
    ("injDurationCyl1", "21C98001", "InjectionDurationCyl1"),
    ("injDurationCyl3", "21C98001", "InjectionDurationCyl3"),
    ("injDurationCyl4", "21C98001", "InjectionDurationCyl4"),
    ("injDurationCyl2", "21C98001", "InjectionDurationCyl2"),
    # Legacy decoded CSV aliases retained for existing readers/configurations.
    ("injTimeCyl1", "21C98001", "InjectionDurationCyl1"),
    ("injTimeCyl3", "21C98001", "InjectionDurationCyl3"),
    ("injTimeCyl4", "21C98001", "InjectionDurationCyl4"),
    ("injTimeCyl2", "21C98001", "InjectionDurationCyl2"),
    ("turboTarget", "21CA8001", "TurboInstr"),
    ("turboMeasured", "21CA8001", "TurboPress"),
    ("boost_mbar", "DERIVED", "Boost"),
    ("atmospheric", "21CA8001", "AtmosphPress"),
    ("turboVaneTarget_pct", "21CA8001", "TurboVaneTarget_pct"),
    ("turboVaneActual_pct", "21CA8001", "TurboVaneActual_pct"),
    ("turboGeomTarget", "21CA8001", "TurboVaneTarget_pct"),
    ("turboGeom", "21CA8001", "TurboVaneActual_pct"),
    ("turboValve", "21CA8001", "TurboValve"),
    ("airMassTarget", "21CA8001", "AirMassTarget"),
    ("airMassMeasured", "21CA8001", "AirMassMeasured"),
    ("airFlowSetting", "21CA8001", "AirMassTarget"),
    ("airFlowMeasured", "21CA8001", "AirMassMeasured"),
    ("airFlowSensorTemp", "21CA8001", "AirFlowSensorTemp"),
    ("airManifoldTemp", "21CA8001", "AirManifoldTemp"),
    ("externalTemp", "21CA8001", "ExternalTemp"),
    ("airMixerTarget_pct", "21CA8001", "AirMixerTarget_pct"),
    ("airMixerActual_pct", "21CA8001", "AirMixerActual_pct"),
    ("airMixerTarget", "21CA8001", "AirMixerTarget_pct"),
    ("airMixer", "21CA8001", "AirMixerActual_pct"),
    ("egrTarget", "21CA8001", "EGRposInstr"),
    ("egrRepeat", "21CA8001", "EGRpos"),
    ("egrControlOutput", "21CA8001", "EGRControlOutput"),
    ("egrCommandDuty", "21CA8001", "EGRControlOutput"),
    ("egrCoolerBypassTarget", "21CA8001", "EGRCoolerBypassTarget"),
    ("egrCoolerBypassRepeat", "21CA8001", "EGRCoolerBypassRepeat"),
    ("airMixerElectrovalve", "21CA8001", "AirMixerElectrovalve"),
    ("fapSoot", "21CB8001", "FAPsoot"),
    ("fapTemp", "21CB8001", "FAPtemp"),
    ("fapTempAlt", "21CB8001", "FAPTempAlt"),
    ("o2SensorSignal_mV", "21CB8001", "O2SensorSignal_mV"),
    ("fapDiffPressure", "21CB8001", "FAPDiffPressure"),
    ("fapLifeLeft_mi", "21CB8001", "FAPlifeLeft"),
    ("lastRegen_mi", "21CB8001", "LastRegen"),
    ("avg10Regen_mi", "21CB8001", "Avg10regen"),
    ("fapDistance_mi", "21CB8001", "FAPdistance"),
    ("fapAdditiveVol", "21CB8001", "FAPAdditiveVol"),
    ("fapAdditiveRemain", "21CB8001", "FAPAdditiveRemain"),
    ("fapDeposits", "21CB8001", "FAPdeposits"),
    ("fapCinder", "21CB8001", "FAPcinder"),
    ("regenStatusABRaw", "21CB8001", "RegenStatusABRaw"),
    ("regenStatus", "21CB8001", "RegenStatus"),
    ("activeRegeneration", "21CB8001", "ActiveRegeneration"),
    ("powerConsumersRegenAuth", "21CB8001", "PowerConsumersRegenAuth"),
    ("shortTermRegenCapacity", "21CB8001", "ShortTermRegenCapacity"),
    ("longTermRegenCapacity", "21CB8001", "LongTermRegenCapacity"),
]


# =============================================================================
# UTILS
# =============================================================================

def clean_response_lines(raw: str) -> List[str]:
    lines: List[str] = []
    for line in raw.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line == ">":
            continue
        lines.append(line)
    return lines


def response_clean(lines: Sequence[str]) -> str:
    useful = []
    for line in lines:
        upper = line.upper().strip()
        if upper.startswith("AT") or upper in {"OK", "?"}:
            useful.append(upper)
            continue
        if re.fullmatch(r"[0-9A-F]+", upper):
            useful.append(upper)
    return " | ".join(useful)


# =============================================================================
# ISO-TP PARSING
# =============================================================================

@dataclass
class ParsedPayload:
    payload: Optional[bytes]
    body: Optional[bytes]
    payload_hex: str
    body_hex: str
    parse_status: str


def extract_hex_frames(lines: Sequence[str]) -> List[str]:
    frames: List[str] = []
    for line in lines:
        s = re.sub(r"[^0-9A-Fa-f]", "", line).upper()
        if len(s) >= 5 and re.fullmatch(r"[0-9A-F]+", s):
            # Raw-header mode frames look like 688102E... The first 3 hex chars are the CAN id.
            frames.append(s)
    return frames


def reassemble_isotp_raw_header(frames: Sequence[str]) -> ParsedPayload:
    payload = bytearray()
    expected_len: Optional[int] = None
    saw_any = False

    for frame in frames:
        if len(frame) < 5:
            continue
        data_hex = frame[3:]  # strip 11-bit CAN header as 3 hex nibbles, e.g. 688
        if len(data_hex) % 2 != 0:
            return ParsedPayload(None, None, "", "", f"odd data hex length in frame {frame}")
        try:
            data = bytes.fromhex(data_hex)
        except ValueError:
            return ParsedPayload(None, None, "", "", f"bad hex frame {frame}")
        if not data:
            continue

        saw_any = True
        pci = data[0]
        frame_type = pci >> 4

        if frame_type == 0x0:  # single frame
            length = pci & 0x0F
            payload.extend(data[1:1 + length])
            expected_len = length
        elif frame_type == 0x1:  # first frame
            if len(data) < 2:
                return ParsedPayload(None, None, "", "", "short first frame")
            expected_len = ((pci & 0x0F) << 8) | data[1]
            payload.extend(data[2:])
        elif frame_type == 0x2:  # consecutive frame
            payload.extend(data[1:])
        else:
            return ParsedPayload(None, None, "", "", f"unsupported ISO-TP frame type {frame_type}")

    if not saw_any:
        return ParsedPayload(None, None, "", "", "no hex frames")

    if expected_len is not None:
        payload = payload[:expected_len]

    payload_bytes = bytes(payload)
    if len(payload_bytes) >= 2 and payload_bytes[0] == 0x61 and payload_bytes[1] == 0xFF:
        body = payload_bytes[2:]
        return ParsedPayload(payload_bytes, body, payload_bytes.hex().upper(), body.hex().upper(), "ok")

    return ParsedPayload(payload_bytes, None, payload_bytes.hex().upper(), "", "payload_missing_61ff")


# =============================================================================
# DECODING
# =============================================================================

def read_raw(body: bytes, field_def: FieldDef) -> Optional[int]:
    off = field_def.offset
    length = field_def.length
    if off < 0 or off + length > len(body):
        return None
    raw = body[off:off + length]
    t = field_def.raw_type
    if t in {"u8", "enum", "bool", "equals"}:
        return raw[0]
    if t == "s8":
        v = raw[0]
        return v - 256 if v >= 128 else v
    if t == "u16be":
        return int.from_bytes(raw, "big", signed=False)
    if t == "s16be":
        return int.from_bytes(raw, "big", signed=True)
    if t == "u24be":
        return int.from_bytes(raw, "big", signed=False)
    if t == "u32be":
        return int.from_bytes(raw, "big", signed=False)
    if t == "bit":
        if field_def.bit_index is None:
            return None
        return 1 if (raw[0] & (1 << field_def.bit_index)) else 0
    if t == "bitmask":
        if field_def.mask is None:
            return None
        if field_def.any_mask:
            return 1 if (raw[0] & field_def.mask) != 0 else 0
        return 1 if (raw[0] & field_def.mask) == field_def.mask else 0
    return None


def decode_field(body: bytes, field_def: FieldDef) -> Optional[Any]:
    raw = read_raw(body, field_def)
    if raw is None:
        return None
    if raw in field_def.unavailable_raw_values:
        return None
    if field_def.raw_type == "enum":
        if field_def.enum_map:
            return field_def.enum_map.get(raw, f"?{raw}")
        return str(raw)
    if field_def.raw_type == "bool":
        return bool(raw)
    if field_def.raw_type == "equals":
        return raw == field_def.equals_value
    return raw * field_def.scale + field_def.add


FIELD_BY_COMMAND: Dict[str, List[FieldDef]] = {}
for f in FIELD_MAP:
    FIELD_BY_COMMAND.setdefault(f.command, []).append(f)


def decode_body(command: str, body: bytes) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for f in FIELD_BY_COMMAND.get(command, []):
        value = decode_field(body, f)
        raw = read_raw(body, f)
        out[f.name] = {
            "value": value,
            "raw": raw,
            "unit": f.unit,
            "confidence": f.confidence,
            "fap_label": f.fap_label,
            "field": f,
        }
    for alias, canonical_name in FIELD_NAME_ALIASES.items():
        if canonical_name in out:
            out[alias] = {**out[canonical_name], "alias_for": canonical_name}
    return out


# Candidate and unknown bytes remain available for discovery. Confirmed identity
# and provisional fields have enough evidence to leave the unknown-byte system.
EXCLUDE_FROM_UNKNOWN_CONFIDENCES = {
    Confidence.CONFIRMED,
    Confidence.CONFIRMED_IDENTITY,
    Confidence.PROVISIONAL,
    # Legacy labels retained on definitions outside this evidence review.
    "confirmed-ish",
    "likely",
}
KNOWN_OFFSETS: Dict[str, Dict[int, List[str]]] = {}
for f in FIELD_MAP:
    if f.command == "DERIVED":
        continue
    if f.confidence not in EXCLUDE_FROM_UNKNOWN_CONFIDENCES:
        continue
    for i in range(f.offset, f.offset + f.length):
        KNOWN_OFFSETS.setdefault(f.command, {}).setdefault(i, []).append(f.name)


def build_canonical(decoded_by_command: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    for output_name, command, field_name in CANONICAL_FIELDS:
        if command == "DERIVED":
            continue
        value = decoded_by_command.get(command, {}).get(field_name, {}).get("value")
        row[output_name] = value

    turbo = row.get("turboMeasured")
    atm = row.get("atmospheric")
    if isinstance(turbo, (int, float)) and isinstance(atm, (int, float)):
        row["boost_mbar"] = turbo - atm
    else:
        row["boost_mbar"] = ""

    return row


# =============================================================================
# UNKNOWN BYTE TRACKING
# =============================================================================

@dataclass
class ByteStats:
    command: str
    offset: int
    known_names: List[str] = field(default_factory=list)
    first: Optional[int] = None
    last: Optional[int] = None
    min_value: Optional[int] = None
    max_value: Optional[int] = None
    changes: int = 0
    count: int = 0
    unique_values: set = field(default_factory=set)

    def update(self, value: int) -> None:
        if self.count == 0:
            self.first = value
            self.min_value = value
            self.max_value = value
        else:
            if value != self.last:
                self.changes += 1
            self.min_value = min(self.min_value if self.min_value is not None else value, value)
            self.max_value = max(self.max_value if self.max_value is not None else value, value)
        self.last = value
        self.count += 1
        if len(self.unique_values) < 500:
            self.unique_values.add(value)

    @property
    def is_known(self) -> bool:
        return bool(self.known_names)

    @property
    def classification(self) -> str:
        if self.count == 0:
            return "empty"
        if self.min_value == self.max_value:
            return "static"
        if self.changes <= 3:
            return "rare_change"
        return "moving"


class UnknownTracker:
    def __init__(self) -> None:
        self.stats: Dict[Tuple[str, int], ByteStats] = {}

    def update_body(self, command: str, body: bytes) -> None:
        for offset, value in enumerate(body):
            if offset in KNOWN_OFFSETS.get(command, {}):
                continue
            key = (command, offset)
            if key not in self.stats:
                self.stats[key] = ByteStats(command=command, offset=offset)
            self.stats[key].update(value)

    def unknown_row(self, bodies_by_command: Dict[str, bytes]) -> Dict[str, Any]:
        row: Dict[str, Any] = {}
        for command, body in bodies_by_command.items():
            for offset, value in enumerate(body):
                if offset in KNOWN_OFFSETS.get(command, {}):
                    continue
                row[f"{command}_b{offset:02d}"] = value
        return row

    def write_report(self, path: str) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = CompactDictWriter(f, fieldnames=[
                "command", "offset", "hex_offset", "known", "known_names", "classification",
                "count", "changes", "unique_count", "first", "last", "min", "max", "unique_values_sample",
            ])
            writer.writeheader()
            for key in sorted(self.stats.keys()):
                s = self.stats[key]
                sample_values = sorted(s.unique_values)
                writer.writerow({
                    "command": s.command,
                    "offset": s.offset,
                    "hex_offset": f"0x{s.offset:02X}",
                    "known": "yes" if s.is_known else "no",
                    "known_names": ", ".join(s.known_names),
                    "classification": s.classification,
                    "count": s.count,
                    "changes": s.changes,
                    "unique_count": len(s.unique_values),
                    "first": s.first,
                    "last": s.last,
                    "min": s.min_value,
                    "max": s.max_value,
                    "unique_values_sample": " ".join(f"{v:02X}" for v in sample_values[:64]),
                })


# =============================================================================
# LOGGING OUTPUTS
# =============================================================================

RAW_COLUMNS = [
    "timestamp", "sample", "phase", "profile", "command", "ok", "response_time_ms",
    "clean_lines", "payload_hex", "body_hex", "body_len", "parse_status", "raw",
]

CONNECTION_COLUMNS = [
    "timestamp", "attempt", "port", "port_exists", "port_diagnostics", "baudrate",
    "stage", "ok", "elapsed_ms", "response_clean", "error",
]

DECODE_CHANGE_COLUMNS = [
    "timestamp", "sample", "field_id", "old", "new", "delta", "raw_value",
]

FINAL_COLUMNS = [
    "field", "fap_label", "command", "offset", "length", "raw_type", "formula", "final_value", "unit", "raw_value", "confidence", "description",
]


def formula_text(f: FieldDef) -> str:
    base = f.raw_type
    if f.raw_type == "bit":
        base = f"bit{f.bit_index}"
    elif f.raw_type == "bitmask":
        base = f"mask 0x{f.mask:02X}" if f.mask is not None else "mask"
        if f.any_mask:
            base += " any"
    elif f.raw_type == "enum":
        base = "enum"
    elif f.raw_type == "bool":
        base = "boolean"
    elif f.raw_type == "equals":
        base = f"raw == 0x{f.equals_value:02X}" if f.equals_value is not None else "equals"
    if f.scale != 1.0 or f.add != 0.0:
        base = f"{base} * {f.scale:g} + {f.add:g}"
    if f.unavailable_raw_values:
        sentinels = ", ".join(f"0x{value:X}" for value in f.unavailable_raw_values)
        base += f"; {sentinels} unavailable"
    return base


def diagnostic_value(value: Any, field_def: FieldDef) -> Any:
    if value is None:
        return ""
    if field_def.diagnostic_format == "hex" and isinstance(value, (int, float)):
        return f"0x{int(value):02X}"
    return value


def write_final_values(path: str, last_decoded: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
    rows: List[Dict[str, Any]] = []
    for f in FIELD_MAP:
        if f.command == "DERIVED":
            continue
        item = last_decoded.get(f.command, {}).get(f.name)
        if not item:
            continue
        rows.append({
            "field": f.name,
            "fap_label": f.fap_label,
            "command": f.command,
            "offset": f.offset,
            "length": f.length,
            "raw_type": f.raw_type,
            "formula": formula_text(f),
            "final_value": diagnostic_value(item.get("value"), f),
            "unit": f.unit,
            "raw_value": diagnostic_value(item.get("raw"), f),
            "confidence": f.confidence,
            "description": f.description,
        })
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = CompactDictWriter(f, fieldnames=FINAL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

CANONICAL_COLUMN_NAMES = [name for name, _command, _field in CANONICAL_FIELDS]


_CHANGE_FIELD_IDS: Dict[Tuple[str, str], int] = {
    (field.command, field.name): index
    for index, field in enumerate(FIELD_MAP, 1)
    if field.command != "DERIVED"
}


def change_field_id(command: str, field_name: str) -> int:
    """Stable-within-schema numeric identity for compact change-event rows."""
    try:
        return _CHANGE_FIELD_IDS[(command, field_name)]
    except KeyError as exc:
        raise ValueError(f"No change-log schema entry for {command}/{field_name}") from exc


def change_field_schema() -> list[dict[str, Any]]:
    """Metadata removed from repeated rows and retained once per journey."""
    rows: list[dict[str, Any]] = []
    for field in FIELD_MAP:
        field_id = _CHANGE_FIELD_IDS.get((field.command, field.name))
        if field_id is None:
            continue
        rows.append({
            "field_id": field_id,
            "command": field.command,
            "field": field.name,
            "fap_label": field.fap_label,
            "unit": field.unit,
            "confidence": field.confidence,
            "offset": field.offset,
            "length": field.length,
            "description": field.description,
            "enum_values": field.enum_map or {},
        })
    return rows
