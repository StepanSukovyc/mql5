from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from trade_execution import close_position_by_ticket


class TradeExecutionTests(unittest.TestCase):
	@patch("trade_execution.get_symbol_tick")
	@patch("trade_execution.mt5")
	def test_close_position_truncates_comment_for_mt5(self, mock_mt5, mock_get_symbol_tick) -> None:
		mock_get_symbol_tick.return_value = SimpleNamespace(bid=1.2345, ask=1.2347)
		mock_mt5.POSITION_TYPE_BUY = 0
		mock_mt5.ORDER_TYPE_SELL = 1
		mock_mt5.TRADE_ACTION_DEAL = 2
		mock_mt5.ORDER_TIME_GTC = 3
		mock_mt5.ORDER_FILLING_IOC = 4
		mock_mt5.TRADE_RETCODE_DONE = 10009
		mock_mt5.order_send.return_value = SimpleNamespace(retcode=10009, price=1.2345)

		result = close_position_by_ticket(
			position_ticket=12345,
			symbol="EURUSD_ecn",
			position_type=0,
			volume=0.01,
			comment="Profit protection [gemini_primary]",
			strategy_id="gemini_primary",
			magic=234000,
		)

		self.assertTrue(result)
		request = mock_mt5.order_send.call_args.args[0]
		self.assertEqual(request["comment"], "pp:gemini_primary")
		self.assertLessEqual(len(request["comment"]), 31)


if __name__ == "__main__":
	unittest.main()
