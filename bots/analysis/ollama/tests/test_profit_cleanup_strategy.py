"""Unit tests for minute profit-cleanup calculations."""

from __future__ import annotations

import unittest

from profit_cleanup_strategy import calculate_profit_cleanup_metrics


class ProfitCleanupStrategyTests(unittest.TestCase):
	def test_user_example_is_not_eligible(self) -> None:
		metrics = calculate_profit_cleanup_metrics(
			balance=3261.0,
			position_volume=0.06,
			profit=6.34,
			swap=-0.30,
		)

		self.assertEqual(metrics.reference_volume, 0.07)
		self.assertEqual(metrics.fee, 0.60)
		self.assertEqual(metrics.net_profit, 5.44)
		self.assertAlmostEqual(metrics.target_profit, 27.9514285714, places=6)
		self.assertFalse(metrics.eligible)

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
		self.assertAlmostEqual(metrics.target_profit, 4.6585714286, places=6)
		self.assertTrue(metrics.eligible)

	def test_target_profit_has_minimum_floor(self) -> None:
		metrics = calculate_profit_cleanup_metrics(
			balance=0.10,
			position_volume=0.01,
			profit=0.20,
			swap=-0.10,
		)

		self.assertEqual(metrics.reference_volume, 0.01)
		self.assertEqual(metrics.fee, 0.10)
		self.assertEqual(metrics.net_profit, 0.00)
		self.assertEqual(metrics.target_profit, 0.005)
		self.assertFalse(metrics.eligible)

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
		self.assertAlmostEqual(metrics.target_profit, 11.25, places=6)
		self.assertFalse(metrics.eligible)


if __name__ == "__main__":
	unittest.main()