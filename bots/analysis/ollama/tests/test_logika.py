"""Tests for prediction-source selection in the trading loop."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logika import _select_prediction_folder


class PredictionSourceSelectionTests(unittest.TestCase):
	def test_cloud_predictions_override_existing_gemini_predictions(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			service_folder = Path(temp_dir)
			existing_predictions = service_folder / "20261116_201500" / "predikce"
			cloud_predictions = service_folder / "ollama_cloud" / "predikce"
			existing_predictions.mkdir(parents=True)
			cloud_predictions.mkdir(parents=True)
			(cloud_predictions / "EURUSD_ecn.json").write_text("{}", encoding="utf-8")

			selected = _select_prediction_folder(
				service_folder,
				existing_predictions,
				cloud_predictions_enabled=True,
			)

		self.assertEqual(selected, cloud_predictions)

	def test_cloud_enabled_does_not_fall_back_to_gemini_predictions(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			service_folder = Path(temp_dir)
			existing_predictions = service_folder / "20261116_201500" / "predikce"
			existing_predictions.mkdir(parents=True)

			selected = _select_prediction_folder(
				service_folder,
				existing_predictions,
				cloud_predictions_enabled=True,
			)

		self.assertIsNone(selected)

	def test_cloud_disabled_keeps_existing_prediction_source(self) -> None:
		existing_predictions = Path("existing-predictions")

		selected = _select_prediction_folder(
			Path("service"),
			existing_predictions,
			cloud_predictions_enabled=False,
		)

		self.assertEqual(selected, existing_predictions)
