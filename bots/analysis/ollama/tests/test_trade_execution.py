from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from trade_execution import TradeExecutionResult, execute_trade


class TradeExecutionPositionLimitTests(unittest.TestCase):
	@patch.dict(os.environ, {}, clear=True)
	@patch("trade_execution.mt5.order_send")
	@patch("trade_execution.mt5.positions_get", return_value=[object()] * 17)
	def test_default_limit_blocks_eighteenth_position(self, _mock_positions_get, mock_order_send) -> None:
		result = execute_trade("EURUSD_ecn", "BUY", 0.01, return_execution_result=True)

		self.assertIsInstance(result, TradeExecutionResult)
		self.assertFalse(result.success)
		self.assertIn("17/17", result.error_message)
		mock_order_send.assert_not_called()

	@patch.dict(os.environ, {"MT5_MAX_OPEN_POSITIONS": "3"}, clear=True)
	@patch("trade_execution.mt5.order_send")
	@patch("trade_execution.mt5.positions_get", return_value=[object()] * 3)
	def test_configured_limit_is_enforced(self, _mock_positions_get, mock_order_send) -> None:
		result = execute_trade("EURUSD_ecn", "BUY", 0.01, return_execution_result=True)

		self.assertFalse(result.success)
		self.assertIn("3/3", result.error_message)
		mock_order_send.assert_not_called()

	@patch("trade_execution.mt5.last_error", return_value=(1, "terminal unavailable"))
	@patch("trade_execution.mt5.order_send")
	@patch("trade_execution.mt5.positions_get", return_value=None)
	def test_position_read_failure_blocks_trade(self, _mock_positions_get, mock_order_send, _mock_last_error) -> None:
		result = execute_trade("EURUSD_ecn", "BUY", 0.01, return_execution_result=True)

		self.assertFalse(result.success)
		self.assertIn("Failed to verify open position limit", result.error_message)
		mock_order_send.assert_not_called()


if __name__ == "__main__":
	unittest.main()