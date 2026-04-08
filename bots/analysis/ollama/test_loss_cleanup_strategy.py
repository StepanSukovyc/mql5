"""Unit tests for hourly loss-cleanup safety guards."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from loss_cleanup_strategy import _find_cleanup_candidate, _would_keep_realized_profit_non_negative


class LossCleanupStrategyTests(unittest.TestCase):
	def test_non_negative_realized_profit_guard_allows_zero_remainder(self) -> None:
		self.assertTrue(_would_keep_realized_profit_non_negative(121.99, 121.99))

	def test_non_negative_realized_profit_guard_rejects_negative_remainder(self) -> None:
		self.assertFalse(_would_keep_realized_profit_non_negative(121.98, 121.99))

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
			daily_realized_profit=121.98,
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
			daily_realized_profit=121.99,
		)

		self.assertIsNotNone(candidate)
		assert candidate is not None
		self.assertEqual(candidate.ticket, 26912003)
		self.assertEqual(candidate.loss_amount, 121.99)

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
			daily_realized_profit=121.75,
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
			daily_realized_profit=121.75,
		)

		self.assertIsNotNone(candidate)
		assert candidate is not None
		self.assertEqual(candidate.ticket, 44444444)
		self.assertEqual(candidate.symbol, "FIRST_TIE_ecn")
		self.assertEqual(candidate.loss_amount, 121.75)


if __name__ == "__main__":
	unittest.main()