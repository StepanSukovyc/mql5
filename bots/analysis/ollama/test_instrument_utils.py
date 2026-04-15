from __future__ import annotations

import unittest
from unittest.mock import patch

from instrument_utils import is_cfd_symbol


class InstrumentUtilsTests(unittest.TestCase):
	@patch("instrument_utils.get_symbol_info")
	def test_ecn_suffix_is_never_classified_as_cfd(self, mock_get_symbol_info) -> None:
		mock_get_symbol_info.return_value = type(
			"SymbolInfo",
			(),
			{
				"trade_calc_mode": 1,
				"path": "CFD\\Forex",
				"description": "CFD forex instrument",
			},
		)()

		self.assertFalse(is_cfd_symbol("EURUSD_ecn"))

	@patch("instrument_utils.get_symbol_info")
	@patch("instrument_utils.mt5")
	def test_non_ecn_symbol_can_still_be_classified_as_cfd(self, mock_mt5, mock_get_symbol_info) -> None:
		mock_mt5.SYMBOL_CALC_MODE_CFD = 42
		mock_mt5.SYMBOL_CALC_MODE_CFDINDEX = 43
		mock_mt5.SYMBOL_CALC_MODE_CFDLEVERAGE = 44
		mock_get_symbol_info.return_value = type(
			"SymbolInfo",
			(),
			{
				"trade_calc_mode": 42,
				"path": "Indices",
				"description": "Index contract",
			},
		)()

		self.assertTrue(is_cfd_symbol("US30"))


if __name__ == "__main__":
	unittest.main()