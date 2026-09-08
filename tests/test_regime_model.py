from __future__ import annotations

import unittest
from pathlib import Path

from foxdash_lite.regime_model import RegimeModel


ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models" / "regime_model_2026-09-observe.json"


class RegimeModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = RegimeModel.load(MODEL)

    def test_observe_model_loads_and_remains_observation_only(self) -> None:
        self.assertTrue(self.model.observation_only)
        self.assertEqual(len(self.model.regimes), 6)
        self.assertEqual(len(self.model.features), 7)

    def test_each_centroid_classifies_back_to_its_own_regime(self) -> None:
        for regime in self.model.regimes:
            values = dict(zip(self.model.features, regime.centroid))
            with self.subTest(regime=regime.regime_id):
                result = self.model.classify(values)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.regime_id, regime.regime_id)
                self.assertAlmostEqual(result.distance, 0.0)
                self.assertGreater(result.confidence, 0.55)

    def test_motorway_reference_is_not_high_load_acceleration(self) -> None:
        values = {
            "rpm": 1970.0,
            "speed_mph": 71.0,
            "absLoadProxy": 41.0,
            "relativeAccel_mps2": 0.0,
            "boostTargetProxy": 480.0,
            "railTargetProxy": 1070.0,
            "airFlowSetting": 485.0,
        }
        result = self.model.classify(values)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.regime_id, "motorway-cruise")

    def test_loaded_acceleration_reference_is_not_motorway_cruise(self) -> None:
        values = {
            "rpm": 1930.0,
            "speed_mph": 59.0,
            "absLoadProxy": 73.0,
            "relativeAccel_mps2": 0.42,
            "boostTargetProxy": 1330.0,
            "railTargetProxy": 1190.0,
            "airFlowSetting": 845.0,
        }
        result = self.model.classify(values)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.regime_id, "high-load-accel")

    def test_missing_feature_refuses_to_guess(self) -> None:
        values = {name: 1.0 for name in self.model.features if name != "rpm"}
        self.assertIsNone(self.model.classify(values))

    def test_model_round_trips_through_dictionary_schema(self) -> None:
        rebuilt = RegimeModel.from_dict(self.model.to_dict())
        self.assertEqual(rebuilt, self.model)

    def test_invalid_scale_is_rejected(self) -> None:
        payload = self.model.to_dict()
        payload["normalisation"]["scale"][0] = 0.0
        with self.assertRaises(ValueError):
            RegimeModel.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
