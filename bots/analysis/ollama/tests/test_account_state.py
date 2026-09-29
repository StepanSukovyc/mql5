"""Tests for dynamic strategy balance-cap calculations."""

from __future__ import annotations

import unittest

from account_state import get_account_balance_cap


class AccountStateTests(unittest.TestCase):
	def test_balance_cap_rounds_down_to_whole_thousand(self) -> None:
		self.assertEqual(get_account_balance_cap(11125.0), 11000.0)
		self.assertEqual(get_account_balance_cap(9999.0), 9000.0)
		self.assertEqual(get_account_balance_cap(10000.0), 10000.0)
