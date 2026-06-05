"""Unit tests for swap rollover cleanup calculations."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from swap_rollover_cleanup_strategy import _find_candidates, calculate_swap_rollover_cleanup_metrics


class _Position:
	def __init__(self, ticket: int, symbol: str, position_type: int, volume: float, profit: float, swap: float, magic: int = 0, comment: str = "") -> None:
		self.ticket = ticket
		self.symbol = symbol
		self.type = position_type
		self.volume = volume
		self.profit = profit
		self.swap = swap
		self.magic = magic
		self.comment = comment


class SwapRolloverCleanupStrategyTests(unittest.TestCase):
	def test_position_below_threshold_is_not_eligible(self) -> None:
		metrics = calculate_swap_rollover_cleanup_metrics(
			balance=3261.0,
			position_volume=0.06,
			profit=0.70,
			swap=-0.05,
		)

		self.assertEqual(metrics.fee, 0.60)
		self.assertEqual(metrics.net_profit, 0.05)
		self.assertEqual(metrics.target_profit, 0.10)
		self.assertFalse(metrics.eligible)

	def test_position_can_become_eligible(self) -> None:
		metrics = calculate_swap_rollover_cleanup_metrics(
			balance=3261.0,
			position_volume=0.01,
			profit=0.25,
			swap=0.0,
		)

		self.assertEqual(metrics.fee, 0.10)
		self.assertEqual(metrics.net_profit, 0.15)
		self.assertEqual(metrics.target_profit, 0.10)
		self.assertTrue(metrics.eligible)

	def test_exact_threshold_is_eligible(self) -> None:
		metrics = calculate_swap_rollover_cleanup_metrics(
			balance=0.10,
			position_volume=0.01,
			profit=0.20,
			swap=0.0,
		)

		self.assertEqual(metrics.fee, 0.10)
		self.assertEqual(metrics.net_profit, 0.10)
		self.assertEqual(metrics.target_profit, 0.10)
		self.assertTrue(metrics.eligible)

	def test_negative_swap_and_fee_can_disqualify_position(self) -> None:
		metrics = calculate_swap_rollover_cleanup_metrics(
			balance=1500.0,
			position_volume=0.03,
			profit=0.20,
			swap=-0.40,
		)

		self.assertEqual(metrics.fee, 0.30)
		self.assertEqual(metrics.net_profit, -0.50)
		self.assertEqual(metrics.target_profit, 0.10)
		self.assertFalse(metrics.eligible)

	@patch.dict(os.environ, {"PROFIT_PROTECTION_STRATEGY_ENABLED": "true", "INDEX_STRATEGY_ENABLED": "true"}, clear=False)
	@patch("swap_rollover_cleanup_strategy.mt5.positions_get")
	def test_swap_cleanup_skips_positions_managed_by_profit_protection(self, mock_positions_get) -> None:
		mock_positions_get.return_value = [
			_Position(
				ticket=1,
				symbol="US100_ecn",
				position_type=0,
				volume=0.01,
				profit=1.00,
				swap=0.0,
				magic=234100,
				comment="ga:gemini_indices",
			),
			_Position(
				ticket=2,
				symbol="EURUSD_ecn",
				position_type=0,
				volume=0.01,
				profit=0.50,
				swap=0.0,
				magic=999999,
				comment="other",
			),
		]

		candidates = _find_candidates(balance=5000.0)

		self.assertEqual(len(candidates), 1)
		self.assertEqual(candidates[0].ticket, 2)


if __name__ == "__main__":
	unittest.main()