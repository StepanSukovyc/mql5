"""Unit tests for minute profit-cleanup calculations."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import profit_cleanup_strategy
from position_costs import get_position_charged_costs
from profit_cleanup_strategy import calculate_profit_cleanup_metrics, run_profit_cleanup_strategy_if_due


class ProfitCleanupStrategyTests(unittest.TestCase):
	def setUp(self) -> None:
		profit_cleanup_strategy._LAST_EVALUATED_MINUTE_KEY = None

	def test_position_costs_sum_commission_and_fee_from_deal_history(self) -> None:
		position = SimpleNamespace(identifier=456, ticket=123)
		with patch(
			"position_costs.mt5.history_deals_get",
			return_value=(
				SimpleNamespace(commission=-2.0, fee=0.0),
				SimpleNamespace(commission=0.0, fee=-0.25),
			),
		) as mock_history:
			self.assertEqual(get_position_charged_costs(position), -2.25)

		mock_history.assert_called_once_with(position=456)

	def test_position_costs_fail_if_history_is_unavailable_or_empty(self) -> None:
		position = SimpleNamespace(identifier=456, ticket=123)
		for history_result in (None, ()):
			with self.subTest(history_result=history_result), patch(
				"position_costs.mt5.history_deals_get", return_value=history_result
			), patch("position_costs.mt5.last_error", return_value=(-1, "unavailable")):
				with self.assertRaises(RuntimeError):
					get_position_charged_costs(position)

	def test_user_example_is_not_eligible(self) -> None:
		metrics = calculate_profit_cleanup_metrics(
			balance=3261.0,
			position_volume=0.06,
			profit=6.34,
			swap=-0.30,
			charged_costs=-0.60,
		)

		self.assertEqual(metrics.reference_volume, 0.07)
		self.assertEqual(metrics.fee, -0.60)
		self.assertEqual(metrics.net_profit, 5.44)
		self.assertAlmostEqual(metrics.target_profit, 27.9514285714, places=6)
		self.assertFalse(metrics.eligible)

	def test_position_can_become_eligible(self) -> None:
		metrics = calculate_profit_cleanup_metrics(
			balance=3261.0,
			position_volume=0.01,
			profit=60.0,
			swap=0.0,
			charged_costs=-0.10,
		)

		self.assertEqual(metrics.reference_volume, 0.07)
		self.assertEqual(metrics.fee, -0.10)
		self.assertEqual(metrics.net_profit, 59.90)
		self.assertAlmostEqual(metrics.target_profit, 4.6585714286, places=6)
		self.assertTrue(metrics.eligible)

	def test_target_profit_has_minimum_floor(self) -> None:
		metrics = calculate_profit_cleanup_metrics(
			balance=0.10,
			position_volume=0.01,
			profit=0.20,
			swap=-0.10,
			charged_costs=-0.10,
		)

		self.assertEqual(metrics.reference_volume, 0.01)
		self.assertEqual(metrics.fee, -0.10)
		self.assertEqual(metrics.net_profit, 0.00)
		self.assertEqual(metrics.target_profit, 0.005)
		self.assertFalse(metrics.eligible)

	def test_negative_swap_and_fee_can_disqualify_position(self) -> None:
		metrics = calculate_profit_cleanup_metrics(
			balance=1500.0,
			position_volume=0.03,
			profit=0.80,
			swap=-0.40,
			charged_costs=-0.30,
		)

		self.assertEqual(metrics.reference_volume, 0.04)
		self.assertEqual(metrics.fee, -0.30)
		self.assertEqual(metrics.net_profit, 0.10)
		self.assertAlmostEqual(metrics.target_profit, 11.25, places=6)
		self.assertFalse(metrics.eligible)

	def test_minute_cleanup_runs_outside_daily_window(self) -> None:
		now_utc = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
		position = SimpleNamespace(
			ticket=123,
			symbol="EURUSD",
			type=0,
			volume=0.01,
			profit=60.0,
			swap=0.0,
		)
		with tempfile.TemporaryDirectory() as temp_dir, patch(
			"profit_cleanup_strategy._get_strategy_enabled", return_value=True
		), patch("profit_cleanup_strategy._get_strategy_dry_run", return_value=False), patch(
			"profit_cleanup_strategy._get_service_folder", return_value=Path(temp_dir)
		), patch("profit_cleanup_strategy.get_swap_block_window") as mock_swap_window, patch(
			"profit_cleanup_strategy.mt5.positions_get", return_value=(position,)
		), patch(
			"profit_cleanup_strategy.get_position_charged_costs", return_value=-0.10
		), patch("profit_cleanup_strategy.close_position_by_ticket", return_value=True) as mock_close:
			mock_swap_window.return_value.contains.return_value = False
			run_profit_cleanup_strategy_if_due({"raw_balance": 3261.0}, now_utc=now_utc)

		mock_close.assert_called_once_with(
			position_ticket=123,
			symbol="EURUSD",
			position_type=0,
			volume=0.01,
			comment="Profit cleanup strategy",
		)

	def test_daily_cleanup_closes_at_fixed_net_profit_threshold(self) -> None:
		now_utc = datetime(2026, 10, 11, 19, 55, tzinfo=timezone.utc)
		position = SimpleNamespace(
			ticket=123,
			symbol="EURUSD",
			type=0,
			volume=0.01,
			profit=0.60,
			swap=0.0,
		)
		with patch(
			"profit_cleanup_strategy._get_strategy_enabled", return_value=True
		), patch("profit_cleanup_strategy._get_strategy_dry_run", return_value=False), patch(
			"profit_cleanup_strategy.get_swap_block_window"
		) as mock_swap_window, patch(
			"profit_cleanup_strategy.mt5.positions_get", return_value=(position,)
		), patch(
			"profit_cleanup_strategy.get_position_charged_costs", return_value=-0.10
		), patch("profit_cleanup_strategy.close_position_by_ticket", return_value=True) as mock_close:
			mock_swap_window.return_value.contains.return_value = False
			run_profit_cleanup_strategy_if_due({"raw_balance": 10000.0}, now_utc=now_utc)

		mock_close.assert_called_once()
		self.assertEqual(mock_close.call_args.kwargs["position_ticket"], 123)

	def test_daily_threshold_does_not_apply_outside_daily_window(self) -> None:
		now_utc = datetime(2026, 10, 11, 20, 5, tzinfo=timezone.utc)
		position = SimpleNamespace(
			ticket=123,
			symbol="EURUSD",
			type=0,
			volume=0.01,
			profit=0.60,
			swap=0.0,
		)
		with patch(
			"profit_cleanup_strategy._get_strategy_enabled", return_value=True
		), patch("profit_cleanup_strategy.get_swap_block_window") as mock_swap_window, patch(
			"profit_cleanup_strategy.mt5.positions_get", return_value=(position,)
		), patch(
			"profit_cleanup_strategy.get_position_charged_costs", return_value=-0.10
		), patch("profit_cleanup_strategy.close_position_by_ticket") as mock_close:
			mock_swap_window.return_value.contains.return_value = False
			run_profit_cleanup_strategy_if_due({"raw_balance": 10000.0}, now_utc=now_utc)

		mock_close.assert_not_called()

	def test_daily_cleanup_uses_actual_position_costs_for_large_volume(self) -> None:
		now_utc = datetime(2026, 10, 11, 19, 55, tzinfo=timezone.utc)
		position = SimpleNamespace(
			identifier=456,
			ticket=123,
			symbol="VIX_ecn",
			type=0,
			volume=100.0,
			profit=80.0,
			swap=0.0,
		)
		with patch(
			"profit_cleanup_strategy._get_strategy_enabled", return_value=True
		), patch("profit_cleanup_strategy._get_strategy_dry_run", return_value=False), patch(
			"profit_cleanup_strategy.get_swap_block_window"
		) as mock_swap_window, patch(
			"profit_cleanup_strategy.mt5.positions_get", return_value=(position,)
		), patch(
			"profit_cleanup_strategy.get_position_charged_costs", return_value=-2.0
		) as mock_costs, patch(
			"profit_cleanup_strategy.close_position_by_ticket", return_value=True
		) as mock_close:
			mock_swap_window.return_value.contains.return_value = False
			run_profit_cleanup_strategy_if_due({"raw_balance": 10000.0}, now_utc=now_utc)

		mock_costs.assert_called_once_with(position)
		mock_close.assert_called_once()

	def test_cleanup_does_not_close_when_mt5_cost_history_is_unavailable(self) -> None:
		now_utc = datetime(2026, 10, 11, 19, 55, tzinfo=timezone.utc)
		position = SimpleNamespace(
			ticket=123,
			symbol="EURUSD",
			type=0,
			volume=0.01,
			profit=10.0,
			swap=0.0,
		)
		with patch(
			"profit_cleanup_strategy._get_strategy_enabled", return_value=True
		), patch("profit_cleanup_strategy.get_swap_block_window") as mock_swap_window, patch(
			"profit_cleanup_strategy.mt5.positions_get", return_value=(position,)
		), patch(
			"profit_cleanup_strategy.get_position_charged_costs",
			side_effect=RuntimeError("MT5 deal history unavailable"),
		), patch("profit_cleanup_strategy.close_position_by_ticket") as mock_close:
			mock_swap_window.return_value.contains.return_value = False
			run_profit_cleanup_strategy_if_due({"raw_balance": 10000.0}, now_utc=now_utc)

		mock_close.assert_not_called()

	def test_cleanup_skips_the_swap_block_window(self) -> None:
		now_utc = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
		with patch("profit_cleanup_strategy._get_strategy_enabled", return_value=True), patch(
			"profit_cleanup_strategy.get_swap_block_window"
		) as mock_swap_window, patch("profit_cleanup_strategy.mt5.positions_get") as mock_positions:
			mock_swap_window.return_value.contains.return_value = True
			run_profit_cleanup_strategy_if_due({"raw_balance": 3261.0}, now_utc=now_utc)

		mock_swap_window.assert_called_once_with(now_utc=now_utc)
		mock_positions.assert_not_called()


if __name__ == "__main__":
	unittest.main()
