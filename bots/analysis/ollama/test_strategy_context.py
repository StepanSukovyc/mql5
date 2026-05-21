from __future__ import annotations

import unittest
from types import SimpleNamespace

from strategy_context import position_belongs_to_strategy


class StrategyContextTests(unittest.TestCase):
	def test_magic_zero_position_can_be_adopted_when_legacy_is_enabled(self) -> None:
		position = SimpleNamespace(magic=0, comment="", symbol="EURSGD_ecn")

		self.assertTrue(
			position_belongs_to_strategy(
				position,
				strategy_id="gemini_primary",
				magic=234000,
				allow_legacy=True,
			)
		)

	def test_magic_zero_position_is_not_adopted_when_legacy_is_disabled(self) -> None:
		position = SimpleNamespace(magic=0, comment="", symbol="EURSGD_ecn")

		self.assertFalse(
			position_belongs_to_strategy(
				position,
				strategy_id="gemini_primary",
				magic=234000,
				allow_legacy=False,
			)
		)

	def test_position_claimed_by_another_strategy_is_not_adopted_as_legacy(self) -> None:
		position = SimpleNamespace(magic=0, comment="ga:gemini_indices", symbol="US100_ecn")

		self.assertFalse(
			position_belongs_to_strategy(
				position,
				strategy_id="gemini_primary",
				magic=234000,
				allow_legacy=True,
			)
		)


if __name__ == "__main__":
	unittest.main()
