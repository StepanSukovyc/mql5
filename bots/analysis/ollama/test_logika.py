from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from logika import find_predictions_folder_for_current_hour, run_strategy_branch, Config


class LogikaTests(unittest.TestCase):
	def test_find_predictions_folder_returns_none_when_service_folder_is_missing(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			missing = Path(temp_dir) / "does-not-exist"
			self.assertIsNone(find_predictions_folder_for_current_hour(missing))

	@patch("logika.make_final_trading_decision")
	@patch("logika.run_trading_logic")
	@patch("logika.run_cycle")
	@patch("logika.find_predictions_folder_for_current_hour")
	def test_run_strategy_branch_creates_branch_folder_before_use(
		self,
		mock_find_predictions_folder,
		mock_run_cycle,
		mock_run_trading_logic,
		mock_make_final_trading_decision,
	) -> None:
		mock_find_predictions_folder.return_value = None
		mock_run_trading_logic.return_value = (False, None)
		with tempfile.TemporaryDirectory() as temp_dir:
			cfg = Config(
				service_dest_folder=Path(temp_dir) / "service",
				symbol_suffix="_ecn",
				symbol_blacklist=[],
				symbol_whitelist=[],
				lookback_periods=30,
				economy_mode_enabled=True,
				economy_mode_interval_seconds=300,
				run_interval_seconds=3600,
				rsi_period=14,
				ma_period=20,
				pretty_json=True,
				mt5_login=None,
				mt5_password=None,
				mt5_server=None,
			)
			profile = SimpleNamespace(
				label="Parallel AI index strategy",
				strategy_id="gemini_indices",
				service_subdir="indices_strategy",
				allowed_symbols=("US100_ecn",),
			)

			run_strategy_branch(cfg, profile)

			self.assertTrue((cfg.service_dest_folder / "indices_strategy").exists())
			mock_run_cycle.assert_called_once()
			mock_make_final_trading_decision.assert_not_called()


if __name__ == "__main__":
	unittest.main()
