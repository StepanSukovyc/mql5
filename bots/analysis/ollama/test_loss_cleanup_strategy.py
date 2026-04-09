"""Unit tests for hourly loss-cleanup safety guards."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from loss_cleanup_strategy import (
	_calculate_effective_profit_budget,
	_get_balance_buffer_percent,
	_get_balance_buffer_ratio,
	_get_previous_prague_day_bounds_utc,
	_is_after_daily_run_time,
	_find_cleanup_candidate,
	_would_keep_realized_profit_non_negative,
)


class LossCleanupStrategyTests(unittest.TestCase):
	def test_non_negative_realized_profit_guard_allows_zero_remainder(self) -> None:
		self.assertTrue(_would_keep_realized_profit_non_negative(121.99, 121.99))

	def test_non_negative_realized_profit_guard_rejects_negative_remainder(self) -> None:
		self.assertFalse(_would_keep_realized_profit_non_negative(121.98, 121.99))

	def test_effective_profit_budget_includes_negative_open_profit_only(self) -> None:
		self.assertEqual(_calculate_effective_profit_budget(90.67, -129.26), -38.59)
		self.assertEqual(_calculate_effective_profit_budget(90.67, 25.00), 90.67)

	@patch("loss_cleanup_strategy._load_dotenv_value", return_value=None)
	def test_balance_buffer_percent_defaults_to_two_percent(self, _mock_load_dotenv_value) -> None:
		self.assertEqual(_get_balance_buffer_percent(), 2.0)
		self.assertEqual(_get_balance_buffer_ratio(), 0.02)

	@patch("loss_cleanup_strategy._load_dotenv_value", return_value="2.5")
	def test_balance_buffer_percent_reads_env_value(self, _mock_load_dotenv_value) -> None:
		self.assertEqual(_get_balance_buffer_percent(), 2.5)
		self.assertEqual(_get_balance_buffer_ratio(), 0.025)

	def test_previous_prague_day_bounds_respect_timezone(self) -> None:
		now_utc = datetime(2026, 4, 8, 10, 45, tzinfo=timezone.utc)
		reference_day, start_utc, end_utc = _get_previous_prague_day_bounds_utc(now_utc)

		self.assertEqual(str(reference_day), "2026-04-07")
		self.assertEqual(start_utc, datetime(2026, 4, 6, 22, 0, tzinfo=timezone.utc))
		self.assertEqual(end_utc, datetime(2026, 4, 7, 22, 0, tzinfo=timezone.utc))

	def test_after_daily_run_time_allows_late_same_day_run(self) -> None:
		now_prague = datetime(2026, 4, 8, 13, 0, tzinfo=timezone(timedelta(hours=2)))
		self.assertTrue(_is_after_daily_run_time(now_prague, 12, 45))
		self.assertFalse(_is_after_daily_run_time(now_prague, 13, 1))

	@patch("loss_cleanup_strategy.mt5.positions_get")
	def test_candidate_is_rejected_when_close_would_make_realized_profit_negative(self, mock_positions_get) -> None:
		now_utc = datetime(2026, 4, 8, 3, 45, tzinfo=timezone.utc)
		opened_at = int((now_utc - timedelta(days=8)).timestamp())
		mock_positions_get.return_value = (
			SimpleNamespace(
				ticket=26912003,
				symbol="USDSEK_ecn",
				type=0,
				volume=0.05,
				time=opened_at,
				profit=-122.25,
				swap=0.76,
			),
		)

		candidate = _find_cleanup_candidate(
			z_limit=132.22,
			now_utc=now_utc,
			effective_profit_budget=121.98,
		)

		self.assertIsNone(candidate)

	@patch("loss_cleanup_strategy.mt5.positions_get")
	def test_candidate_is_selected_when_close_keeps_realized_profit_non_negative(self, mock_positions_get) -> None:
		now_utc = datetime(2026, 4, 8, 3, 45, tzinfo=timezone.utc)
		opened_at = int((now_utc - timedelta(days=8)).timestamp())
		mock_positions_get.return_value = (
			SimpleNamespace(
				ticket=26912003,
				symbol="USDSEK_ecn",
				type=0,
				volume=0.05,
				time=opened_at,
				profit=-122.25,
				swap=0.76,
			),
		)

		candidate = _find_cleanup_candidate(
			z_limit=132.22,
			now_utc=now_utc,
			effective_profit_budget=121.99,
		)

		self.assertIsNotNone(candidate)
		assert candidate is not None
		self.assertEqual(candidate.ticket, 26912003)
		self.assertEqual(candidate.loss_amount, 121.99)

	@patch("loss_cleanup_strategy.mt5.positions_get")
	def test_candidate_is_rejected_when_account_is_currently_in_open_loss(self, mock_positions_get) -> None:
		now_utc = datetime(2026, 4, 8, 4, 45, tzinfo=timezone.utc)
		opened_at = int((now_utc - timedelta(days=173)).timestamp())
		mock_positions_get.return_value = (
			SimpleNamespace(
				ticket=26864451,
				symbol="CHFZAR_ecn",
				type=0,
				volume=0.01,
				time=opened_at,
				profit=-45.66,
				swap=-3.12,
			),
		)

		effective_profit_budget = _calculate_effective_profit_budget(90.67, -129.26)
		candidate = _find_cleanup_candidate(
			z_limit=-71.36,
			now_utc=now_utc,
			effective_profit_budget=effective_profit_budget,
		)

		self.assertIsNone(candidate)

	@patch("loss_cleanup_strategy.mt5.positions_get")
	def test_largest_safe_candidate_is_selected_from_multiple_positions(self, mock_positions_get) -> None:
		now_utc = datetime(2026, 4, 8, 3, 45, tzinfo=timezone.utc)
		opened_at = int((now_utc - timedelta(days=8)).timestamp())
		mock_positions_get.return_value = (
			SimpleNamespace(
				ticket=11111111,
				symbol="TOO_BIG_ecn",
				type=0,
				volume=0.05,
				time=opened_at,
				profit=-129.00,
				swap=0.00,
			),
			SimpleNamespace(
				ticket=22222222,
				symbol="BEST_SAFE_ecn",
				type=0,
				volume=0.05,
				time=opened_at,
				profit=-121.25,
				swap=0.00,
			),
			SimpleNamespace(
				ticket=33333333,
				symbol="SMALLER_SAFE_ecn",
				type=0,
				volume=0.05,
				time=opened_at,
				profit=-80.00,
				swap=0.00,
			),
		)

		candidate = _find_cleanup_candidate(
			z_limit=132.22,
			now_utc=now_utc,
			effective_profit_budget=121.75,
		)

		self.assertIsNotNone(candidate)
		assert candidate is not None
		self.assertEqual(candidate.ticket, 22222222)
		self.assertEqual(candidate.symbol, "BEST_SAFE_ecn")
		self.assertEqual(candidate.loss_amount, 121.75)

	@patch("loss_cleanup_strategy.mt5.positions_get")
	def test_first_candidate_wins_when_safe_loss_amount_ties(self, mock_positions_get) -> None:
		now_utc = datetime(2026, 4, 8, 3, 45, tzinfo=timezone.utc)
		opened_at = int((now_utc - timedelta(days=8)).timestamp())
		mock_positions_get.return_value = (
			SimpleNamespace(
				ticket=44444444,
				symbol="FIRST_TIE_ecn",
				type=0,
				volume=0.05,
				time=opened_at,
				profit=-121.25,
				swap=0.00,
			),
			SimpleNamespace(
				ticket=55555555,
				symbol="SECOND_TIE_ecn",
				type=0,
				volume=0.05,
				time=opened_at,
				profit=-121.25,
				swap=0.00,
			),
		)

		candidate = _find_cleanup_candidate(
			z_limit=132.22,
			now_utc=now_utc,
			effective_profit_budget=121.75,
		)

		self.assertIsNotNone(candidate)
		assert candidate is not None
		self.assertEqual(candidate.ticket, 44444444)
		self.assertEqual(candidate.symbol, "FIRST_TIE_ecn")
		self.assertEqual(candidate.loss_amount, 121.75)


if __name__ == "__main__":
	unittest.main()