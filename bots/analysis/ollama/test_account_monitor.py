"""Unit tests for account monitor trading trigger logic."""

from __future__ import annotations

import unittest

from account_monitor import check_stop_condition


class AccountMonitorTests(unittest.TestCase):
	def test_trigger_uses_raw_free_margin_against_capped_balance(self) -> None:
		account_info = {
			"balance": 5000.0,
			"margin_free": 872.51,
			"raw_margin_free": 1071.81,
		}

		self.assertTrue(check_stop_condition(account_info))

	def test_trigger_stays_blocked_below_threshold(self) -> None:
		account_info = {
			"balance": 5000.0,
			"margin_free": 872.51,
			"raw_margin_free": 999.99,
		}

		self.assertFalse(check_stop_condition(account_info))


if __name__ == "__main__":
	unittest.main()