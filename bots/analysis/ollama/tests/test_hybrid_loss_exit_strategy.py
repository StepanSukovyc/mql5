"""Focused safety tests for the hybrid internal loss-exit strategy."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import hybrid_loss_exit_strategy as hybrid


class HybridLossExitStrategyTests(unittest.TestCase):
	def setUp(self) -> None:
		hybrid._LAST_EVALUATED_AT = None

	@patch.dict(os.environ, {"HYBRID_EXIT_TIMEZONE": "Europe/Prague"}, clear=False)
	def test_friday_position_has_24_active_hours_on_monday(self) -> None:
		opened_at = datetime(2026, 8, 7, 20, 0, tzinfo=timezone.utc)  # Friday 22:00 Prague
		now_utc = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)  # Monday 22:00 Prague
		calendar_hours, closed_hours, active_hours = hybrid.calculate_active_market_age(opened_at, now_utc)

		self.assertEqual(calendar_hours, 72.0)
		self.assertEqual(closed_hours, 48.0)
		self.assertEqual(active_hours, 24.0)

	@patch.dict(os.environ, {"HYBRID_EXIT_ANALYSIS_START_DATE": "2026-08-01", "HYBRID_EXIT_TIMEZONE": "Europe/Prague"}, clear=False)
	def test_position_before_start_date_is_legacy(self) -> None:
		position = SimpleNamespace(ticket=1, symbol="EURUSD_ecn", type=0, volume=0.01, magic=234200, profit=-1.0, swap=0.0, time=int(datetime(2026, 7, 31, 21, 59, tzinfo=timezone.utc).timestamp()))
		candidate = hybrid._build_candidate(position, datetime(2026, 8, 4, tzinfo=timezone.utc), hybrid._get_analysis_start_utc())

		self.assertTrue(candidate.legacy)
		self.assertEqual(candidate.decision, "legacy_excluded")

	def test_free_margin_percent_prefers_actual_account_values(self) -> None:
		self.assertEqual(
			hybrid._get_free_margin_percent(
				{"balance": 7000.0, "margin_free": 500.0, "raw_balance": 10000.0, "raw_margin_free": 400.0}
			),
			4.0,
		)

	@patch.dict(os.environ, {"HYBRID_EXIT_ENABLED": "true", "HYBRID_EXIT_MAX_FREE_MARGIN_PERCENT": "5"}, clear=False)
	@patch("hybrid_loss_exit_strategy.mt5.positions_get")
	def test_high_free_margin_skips_evaluation_without_consuming_interval(self, mock_positions) -> None:
		hybrid.run_hybrid_loss_exit_strategy_if_due({"raw_balance": 10000.0, "raw_margin_free": 500.0})

		mock_positions.assert_not_called()
		self.assertIsNone(hybrid._LAST_EVALUATED_AT)

	@patch.dict(os.environ, {"HYBRID_EXIT_ENABLED": "true", "HYBRID_EXIT_DRY_RUN": "true", "HYBRID_EXIT_ANALYSIS_START_DATE": "2026-08-01", "HYBRID_EXIT_TIMEZONE": "Europe/Prague", "HYBRID_EXIT_CHECK_INTERVAL_MINUTES": "1"}, clear=False)
	@patch("hybrid_loss_exit_strategy.close_position_by_ticket")
	@patch("hybrid_loss_exit_strategy._market_reason_codes", return_value=(["h1_new_low", "h1_close_below_ema20"], {}))
	@patch("hybrid_loss_exit_strategy.mt5.positions_get")
	@patch("hybrid_loss_exit_strategy.get_swap_block_window")
	def test_dry_run_never_sends_close(self, mock_window, mock_positions, _mock_market, mock_close) -> None:
		mock_window.return_value = SimpleNamespace(contains=lambda _now: False)
		mock_positions.return_value = (SimpleNamespace(ticket=2, symbol="EURUSD_ecn", type=0, volume=0.01, magic=234200, profit=-5.0, swap=0.0, time=int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())),)

		hybrid.run_hybrid_loss_exit_strategy_if_due()

		mock_close.assert_not_called()

	@patch.dict(os.environ, {"HYBRID_EXIT_ENABLED": "true", "HYBRID_EXIT_DRY_RUN": "true", "HYBRID_EXIT_ANALYSIS_START_DATE": "2026-08-01", "HYBRID_EXIT_TIMEZONE": "Europe/Prague", "HYBRID_EXIT_CHECK_INTERVAL_MINUTES": "1", "HYBRID_EXIT_EMERGENCY_ACCOUNT_LOSS_PERCENT": "1"}, clear=False)
	@patch("hybrid_loss_exit_strategy._log_candidate")
	@patch("hybrid_loss_exit_strategy._market_reason_codes")
	@patch("hybrid_loss_exit_strategy.mt5.positions_get")
	@patch("hybrid_loss_exit_strategy.get_swap_block_window")
	def test_emergency_loss_is_selected_without_market_data(self, mock_window, mock_positions, mock_market, mock_log) -> None:
		mock_window.return_value = SimpleNamespace(contains=lambda _now: False)
		mock_positions.return_value = (SimpleNamespace(ticket=3, symbol="EURUSD_ecn", type=0, volume=0.01, magic=234200, profit=-20.0, swap=0.0, time=int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())),)

		hybrid.run_hybrid_loss_exit_strategy_if_due({"equity": 1000.0})

		mock_market.assert_not_called()
		self.assertEqual(mock_log.call_args.args[2].decision, "exit_emergency")


if __name__ == "__main__":
	unittest.main()