"""Unit tests for minute profit-cleanup calculations."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile

import pytz

import profit_cleanup_strategy
from profit_cleanup_strategy import calculate_profit_cleanup_metrics, run_profit_cleanup_strategy_if_due


class ProfitCleanupStrategyTests(unittest.TestCase):
	def setUp(self) -> None:
		profit_cleanup_strategy._LAST_EVALUATED_MINUTE_KEY = None

	def test_user_example_meets_fixed_net_profit_threshold(self) -> None:
		metrics = calculate_profit_cleanup_metrics(
			balance=3261.0,
			position_volume=0.06,
			profit=6.34,
			swap=-0.30,
		)

		self.assertEqual(metrics.reference_volume, 0.07)
		self.assertEqual(metrics.fee, 0.60)
		self.assertEqual(metrics.net_profit, 5.44)
		self.assertEqual(metrics.target_profit, 0.50)
		self.assertTrue(metrics.eligible)

	def test_position_can_become_eligible(self) -> None:
		metrics = calculate_profit_cleanup_metrics(
			balance=3261.0,
			position_volume=0.01,
			profit=60.0,
			swap=0.0,
		)

		self.assertEqual(metrics.reference_volume, 0.07)
		self.assertEqual(metrics.fee, 0.10)
		self.assertEqual(metrics.net_profit, 59.90)
		self.assertEqual(metrics.target_profit, 0.50)
		self.assertTrue(metrics.eligible)

	def test_exact_minimum_net_profit_is_eligible(self) -> None:
		metrics = calculate_profit_cleanup_metrics(
			balance=10000.0,
			position_volume=0.01,
			profit=0.60,
			swap=0.0,
		)

		self.assertEqual(metrics.reference_volume, 0.21)
		self.assertEqual(metrics.fee, 0.10)
		self.assertEqual(metrics.net_profit, 0.50)
		self.assertEqual(metrics.target_profit, 0.50)
		self.assertTrue(metrics.eligible)

	def test_negative_swap_and_fee_can_disqualify_position(self) -> None:
		metrics = calculate_profit_cleanup_metrics(
			balance=1500.0,
			position_volume=0.03,
			profit=0.80,
			swap=-0.40,
		)

		self.assertEqual(metrics.reference_volume, 0.04)
		self.assertEqual(metrics.fee, 0.30)
		self.assertEqual(metrics.net_profit, 0.10)
		self.assertEqual(metrics.target_profit, 0.50)
		self.assertFalse(metrics.eligible)

	def test_cleanup_runs_during_daily_prague_window(self) -> None:
		now_utc = pytz.timezone("Europe/Prague").localize(
			datetime(2026, 10, 11, 21, 55)
		).astimezone(timezone.utc)
		position = SimpleNamespace(
			ticket=123,
			symbol="EURUSD",
			type=0,
			volume=0.1,
			profit=100.0,
			swap=0.0,
		)
		with tempfile.TemporaryDirectory() as temp_dir, patch(
			"profit_cleanup_strategy._get_strategy_enabled", return_value=True
		), patch("profit_cleanup_strategy._get_strategy_dry_run", return_value=False), patch(
			"profit_cleanup_strategy._get_service_folder", return_value=Path(temp_dir)
		), patch("profit_cleanup_strategy.get_swap_block_window") as mock_swap_window, patch(
			"profit_cleanup_strategy.mt5.positions_get", return_value=(position,)
		), patch("profit_cleanup_strategy.close_position_by_ticket", return_value=True) as mock_close:
			mock_swap_window.return_value.contains.return_value = False
			run_profit_cleanup_strategy_if_due({"raw_balance": 10000.0}, now_utc=now_utc)

		mock_close.assert_called_once_with(
			position_ticket=123,
			symbol="EURUSD",
			position_type=0,
			volume=0.1,
			comment="Profit cleanup strategy",
		)

	def test_cleanup_does_not_run_at_or_after_2200_prague_time(self) -> None:
		now_utc = pytz.timezone("Europe/Prague").localize(
			datetime(2026, 10, 11, 22, 0)
		).astimezone(timezone.utc)
		with patch("profit_cleanup_strategy._get_strategy_enabled", return_value=True), patch(
			"profit_cleanup_strategy.get_swap_block_window"
		) as mock_swap_window, patch("profit_cleanup_strategy.mt5.positions_get") as mock_positions:
			run_profit_cleanup_strategy_if_due({"raw_balance": 10000.0}, now_utc=now_utc)

		mock_swap_window.assert_not_called()
		mock_positions.assert_not_called()

	def test_cleanup_does_not_run_outside_daily_prague_window(self) -> None:
		now_utc = pytz.timezone("Europe/Prague").localize(
			datetime(2026, 10, 11, 21, 49)
		).astimezone(timezone.utc)
		with patch("profit_cleanup_strategy._get_strategy_enabled", return_value=True), patch(
			"profit_cleanup_strategy.get_swap_block_window"
		) as mock_swap_window, patch("profit_cleanup_strategy.mt5.positions_get") as mock_positions:
			run_profit_cleanup_strategy_if_due({"raw_balance": 10000.0}, now_utc=now_utc)

		mock_swap_window.assert_not_called()
		mock_positions.assert_not_called()


if __name__ == "__main__":
	unittest.main()