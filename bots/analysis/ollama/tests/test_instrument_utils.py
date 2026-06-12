from __future__ import annotations

import unittest
from unittest.mock import patch

from instrument_utils import get_symbol_news_currencies, is_cfd_symbol, is_index_symbol, is_secondary_strategy_symbol_allowed


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

	@patch("instrument_utils.get_symbol_info")
	@patch("instrument_utils.mt5")
	def test_index_symbol_is_detected_from_calc_mode(self, mock_mt5, mock_get_symbol_info) -> None:
		mock_mt5.SYMBOL_CALC_MODE_CFDINDEX = 43
		mock_get_symbol_info.return_value = type(
			"SymbolInfo",
			(),
			{
				"trade_calc_mode": 43,
				"path": "Indices\\US",
				"description": "US index contract",
			},
		)()

		self.assertTrue(is_index_symbol("US100"))

	@patch.dict("os.environ", {"INDEX_STRATEGY_SYMBOL_WHITELIST": "US100_ecn,US500_ecn"}, clear=False)
	@patch("instrument_utils.get_symbol_info")
	def test_index_symbol_can_fallback_to_index_whitelist(self, mock_get_symbol_info) -> None:
		mock_get_symbol_info.return_value = None

		self.assertTrue(is_index_symbol("US100_ecn"))

	def test_secondary_strategy_allows_forex_when_whitelist_empty(self) -> None:
		self.assertTrue(is_secondary_strategy_symbol_allowed("AUDJPY_ecn", []))

	@patch("instrument_utils.get_symbol_info")
	@patch("instrument_utils.mt5")
	def test_secondary_strategy_allows_cfd_when_whitelist_empty(self, mock_mt5, mock_get_symbol_info) -> None:
		mock_mt5.SYMBOL_CALC_MODE_CFD = 42
		mock_mt5.SYMBOL_CALC_MODE_CFDINDEX = 43
		mock_mt5.SYMBOL_CALC_MODE_CFDLEVERAGE = 44
		mock_get_symbol_info.return_value = type(
			"SymbolInfo",
			(),
			{
				"trade_calc_mode": 42,
				"path": "CFD\\Metals",
				"description": "Metal CFD",
			},
		)()

		self.assertTrue(is_secondary_strategy_symbol_allowed("XAUUSD", []))

	def test_secondary_strategy_blocks_crypto_when_whitelist_empty(self) -> None:
		self.assertFalse(is_secondary_strategy_symbol_allowed("BTCUSD", []))

	def test_fx_symbol_news_currencies_are_inferred_from_symbol(self) -> None:
		self.assertEqual(get_symbol_news_currencies("EURUSD_ecn"), ["EUR", "USD"])

	@patch.dict("os.environ", {"NEWS_FILTER_SYMBOL_CURRENCIES": "US100_ecn:USD,GER40_ecn:EUR"}, clear=False)
	def test_explicit_news_currency_mapping_overrides_inference(self) -> None:
		self.assertEqual(get_symbol_news_currencies("US100_ecn"), ["USD"])


if __name__ == "__main__":
	unittest.main()