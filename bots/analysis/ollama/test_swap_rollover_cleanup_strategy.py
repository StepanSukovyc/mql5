"""Unit tests for swap rollover cleanup calculations."""

from __future__ import annotations

import unittest

from swap_rollover_cleanup_strategy import calculate_swap_rollover_cleanup_metrics


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


if __name__ == "__main__":
	unittest.main()