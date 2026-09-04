from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from foxdash_lite.psa_protocol import (
    CANONICAL_COLUMN_NAMES,
    Confidence,
    FIELD_MAP,
    UnknownTracker,
    decode_body,
    diagnostic_value,
)


def field(command: str, name: str):
    return next(item for item in FIELD_MAP if item.command == command and item.name == name)


class PsaProtocolEvidenceTests(unittest.TestCase):
    def test_injection_duration_scaling_order_and_legacy_aliases(self) -> None:
        body = bytearray(40)
        expected = {
            "InjectionDurationCyl1": (32, 1.100),
            "InjectionDurationCyl3": (34, 1.850),
            "InjectionDurationCyl4": (36, 3.470),
            "InjectionDurationCyl2": (38, 1.020),
        }
        for name, (offset, duration) in expected.items():
            body[offset:offset + 2] = round(duration * 1000).to_bytes(2, "big")

        decoded = decode_body("21C98001", bytes(body))

        for name, (offset, duration) in expected.items():
            self.assertEqual(field("21C98001", name).offset, offset)
            self.assertAlmostEqual(decoded[name]["value"], duration)
            self.assertEqual(decoded[name]["unit"], "ms")
            self.assertEqual(decoded[name]["confidence"], Confidence.CONFIRMED)

        for cylinder in (1, 3, 4, 2):
            old_name = f"InjectionTimeCyl{cylinder}"
            new_name = f"InjectionDurationCyl{cylinder}"
            self.assertEqual(decoded[old_name]["value"], decoded[new_name]["value"])
            self.assertEqual(decoded[old_name]["alias_for"], new_name)
            self.assertIn(f"injDurationCyl{cylinder}", CANONICAL_COLUMN_NAMES)
            self.assertIn(f"injTimeCyl{cylinder}", CANONICAL_COLUMN_NAMES)

    def test_oxygen_sensor_signal_is_unsigned_big_endian_millivolts(self) -> None:
        body = bytearray(9)
        body[7:9] = bytes((0x12, 0x34))
        decoded = decode_body("21CB8001", bytes(body))["O2SensorSignal_mV"]
        self.assertEqual(decoded["raw"], 0x1234)
        self.assertEqual(decoded["value"], 0x1234)
        self.assertEqual(decoded["unit"], "mV")

    def test_filtered_ambient_temperature_uses_quarter_degree_scaling(self) -> None:
        body = bytearray(23)
        body[22] = 100
        decoded = decode_body("21CC8001", bytes(body))["FilteredAmbientTemp_C"]
        self.assertEqual(decoded["value"], 25.0)
        self.assertEqual(decoded["confidence"], Confidence.PROVISIONAL)

    def test_cooling_fan_status_and_active_relationship(self) -> None:
        inactive = bytearray(8)
        active = bytearray(8)
        inactive[5], inactive[7] = 0x00, 0
        active[4], active[5], active[7] = 75, 0x29, 1

        inactive_decoded = decode_body("21CD8001", bytes(inactive))
        active_decoded = decode_body("21CD8001", bytes(active))

        self.assertEqual(inactive_decoded["CoolingFanStatusRaw"]["value"], 0x00)
        self.assertIs(inactive_decoded["CoolingFanActive"]["value"], False)
        self.assertEqual(active_decoded["CoolingFanDemandRaw"]["value"], 75)
        self.assertEqual(active_decoded["CoolingFanStatusRaw"]["value"], 0x29)
        self.assertIs(active_decoded["CoolingFanActive"]["value"], True)

    def test_regen_status_uses_only_observed_mappings_and_exact_active_value(self) -> None:
        expectations = {
            0x00: ("engine stopped, startup or status inactive/unavailable", False),
            0x10: ("engine running, no active regeneration", False),
            0x1C: ("active DPF regeneration", True),
        }
        for raw, (status, active) in expectations.items():
            with self.subTest(raw=raw):
                body = bytearray(28)
                body[27] = raw
                decoded = decode_body("21CB8001", bytes(body))
                self.assertEqual(decoded["RegenStatusABRaw"]["value"], raw)
                self.assertEqual(decoded["RegenStatus"]["value"], status)
                self.assertIs(decoded["ActiveRegeneration"]["value"], active)

        unobserved = bytearray(28)
        unobserved[27] = 0x1D
        decoded = decode_body("21CB8001", bytes(unobserved))
        self.assertEqual(decoded["RegenStatus"]["value"], "?29")
        self.assertIs(decoded["ActiveRegeneration"]["value"], False)

    def test_alternator_ff_is_unavailable_but_raw_is_preserved(self) -> None:
        body = bytearray(14)
        body[13] = 0xFF
        decoded = decode_body("21CC8001", bytes(body))["AlternatorProgressiveChargeRef"]
        self.assertIsNone(decoded["value"])
        self.assertEqual(decoded["raw"], 0xFF)

        body[13] = 75
        self.assertEqual(
            decode_body("21CC8001", bytes(body))["AlternatorProgressiveChargeRef"]["value"],
            75,
        )

    def test_engine_fuelling_mode_keeps_raw_and_provisional_interpretation_separate(self) -> None:
        expected = {
            0: "unknown transient",
            1: "no fuelling / engine stopped",
            2: "overrun, fuel-cut or low-fuelling transition",
            3: "normal fuelling",
            4: "high-load fuelling",
            5: "engine-start or cranking handover",
        }
        for raw, interpretation in expected.items():
            with self.subTest(raw=raw):
                body = bytearray(5)
                body[4] = raw
                decoded = decode_body("21C98001", bytes(body))
                self.assertEqual(decoded["EngineFuellingModeRaw"]["value"], raw)
                self.assertEqual(decoded["EngineFuellingMode"]["value"], interpretation)
                self.assertEqual(decoded["EngineFuellingMode"]["confidence"], Confidence.PROVISIONAL)

    def test_clutch_position_retains_zero_to_one_hundred_range(self) -> None:
        for raw in (0, 42, 100):
            with self.subTest(raw=raw):
                body = bytearray(26)
                body[25] = raw
                decoded = decode_body("21CC8001", bytes(body))["ClutchPedalPosition_pct"]
                self.assertEqual(decoded["value"], raw)
                self.assertEqual(decoded["unit"], "%")

    def test_promoted_renames_keep_raw_decoded_aliases(self) -> None:
        aliases = {
            "AirFlowSetting": "AirMassTarget",
            "AirFlowMeasured": "AirMassMeasured",
            "AirMixerInstr": "AirMixerTarget_pct",
            "AirMixer": "AirMixerActual_pct",
            "TurboGeomInstr": "TurboVaneTarget_pct",
            "TurboVarGeom": "TurboVaneActual_pct",
            "EGRCommandDuty": "EGRControlOutput",
            "FAPtempA": "FAPTempAlt",
        }
        for command, size in (("21CA8001", 41), ("21CB8001", 37)):
            decoded = decode_body(command, bytes(size))
            for old_name, new_name in aliases.items():
                if new_name not in decoded:
                    continue
                self.assertIn(old_name, decoded)
                self.assertEqual(decoded[old_name]["alias_for"], new_name)

    def test_resolved_positions_are_absent_from_unknown_outputs(self) -> None:
        resolved = {
            "21C98001": {4},
            "21CB8001": {7, 8},
            "21CC8001": {22, 25, 27},
            "21CD8001": {4, 5, 7},
        }
        bodies = {command: bytes(range(max(offsets) + 2)) for command, offsets in resolved.items()}
        tracker = UnknownTracker()
        for command, body in bodies.items():
            tracker.update_body(command, body)

        row = tracker.unknown_row(bodies)
        for command, offsets in resolved.items():
            for offset in offsets:
                self.assertNotIn(f"{command}_b{offset:02d}", row)
                self.assertNotIn((command, offset), tracker.stats)

        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "unknown.csv"
            tracker.write_report(str(report))
            with report.open(newline="", encoding="utf-8") as handle:
                report_positions = {(item["command"], int(item["offset"])) for item in csv.DictReader(handle)}
            for command, offsets in resolved.items():
                for offset in offsets:
                    self.assertNotIn((command, offset), report_positions)

    def test_cooling_thermal_state_uses_hex_in_diagnostic_output(self) -> None:
        definition = field("21CC8001", "CoolingThermalStateRaw")
        self.assertEqual(diagnostic_value(0x0D, definition), "0x0D")

    def test_promoted_and_unresolved_confidence_metadata(self) -> None:
        confirmed = {
            "DriverDemandMirror_C9",
            "DriverDemandMirror_CA",
            "InjectionDurationCyl1",
            "InjectionDurationCyl2",
            "InjectionDurationCyl3",
            "InjectionDurationCyl4",
            "AirMassTarget",
            "AirMassMeasured",
            "AirMixerTarget_pct",
            "AirMixerActual_pct",
            "TurboVaneTarget_pct",
            "TurboVaneActual_pct",
            "EGRCoolerBypassTarget",
            "RegenStatusABRaw",
            "ClutchPedalPosition_pct",
            "O2SensorSignal_mV",
            "CoolingFanActive",
        }
        confirmed_identity = {
            "Inj1FlowCorr",
            "Inj2FlowCorr",
            "Inj3FlowCorr",
            "Inj4FlowCorr",
            "FAPTempAlt",
            "EGRControlOutput",
            "CoolingThermalStateRaw",
            "EngineFuellingModeRaw",
            "CoolingFanDemandRaw",
            "CoolingFanStatusRaw",
        }
        candidate = {
            "TurboValve",
            "InjCntrlV",
            "O2heating",
            "Supply5V_2",
            "Supply5V_3",
            "ShortTermRegenCapacity",
            "LongTermRegenCapacity",
            "OilLevelSensorV",
            "AirMixerElectrovalve",
            "PowerConsumersRegenAuth",
            "EGRCoolerBypassRepeat",
        }
        by_name = {item.name: item for item in FIELD_MAP}
        for name in confirmed:
            self.assertEqual(by_name[name].confidence, Confidence.CONFIRMED, name)
        for name in confirmed_identity:
            self.assertEqual(by_name[name].confidence, Confidence.CONFIRMED_IDENTITY, name)
        self.assertEqual(by_name["FilteredAmbientTemp_C"].confidence, Confidence.PROVISIONAL)
        self.assertEqual(by_name["EngineFuellingMode"].confidence, Confidence.PROVISIONAL)
        for name in candidate:
            self.assertEqual(by_name[name].confidence, Confidence.CANDIDATE, name)


if __name__ == "__main__":
    unittest.main()
