from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import MetaTrader5 as mt5

from trade_execution import TradeExecutionResult, _next_margin_sized_lot, execute_trade
from trading_validation import MarginRequirement


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


class TradeExecutionMarginRetryTests(unittest.TestCase):
	@patch(
		"trade_execution.get_symbol_info",
		return_value=SimpleNamespace(volume_min=0.1, volume_step=0.03),
	)
	def test_retry_volume_steps_are_offset_from_broker_minimum(self, _mock_symbol_info) -> None:
		self.assertEqual(_next_margin_sized_lot("ODD_STEP", 0.22, 100.0, 50.0), 0.1)

	@patch("economic_calendar.EconomicCalendarService.evaluate", return_value=SimpleNamespace(allow_new_trade=True))
	@patch("trade_execution.get_symbol_tick", return_value=SimpleNamespace(ask=1.2, bid=1.1))
	@patch("trade_execution.get_symbol_info", return_value=SimpleNamespace(volume_min=0.01, volume_max=10.0, volume_step=0.01))
	@patch("trade_execution.validate_lot_size", side_effect=lambda _symbol, volume: (volume, ""))
	@patch("trade_execution.validate_symbol", return_value=(True, ""))
	@patch("trade_execution._check_open_position_limit", return_value=(True, ""))
	@patch("trade_execution.mt5.order_send")
	@patch("trade_execution.get_margin_requirement")
	def test_reduces_volume_until_margin_check_passes(
		self,
		mock_margin_requirement,
		mock_order_send,
		_mock_position_limit,
		_mock_validate_symbol,
		_mock_validate_lot,
		_mock_symbol_info,
		_mock_tick,
		_mock_calendar,
	) -> None:
		mock_margin_requirement.side_effect = [
			MarginRequirement(1000.0, 200.0, 200.0),
			MarginRequirement(190.0, 200.0, 200.0),
		]
		mock_order_send.return_value = SimpleNamespace(
			retcode=mt5.TRADE_RETCODE_DONE,
			order=123,
			deal=124,
			volume=0.19,
			price=1.2,
		)

		with tempfile.TemporaryDirectory() as temp_dir:
			result = execute_trade(
				"EURUSD_ecn",
				"BUY",
				1.0,
				Path(temp_dir),
				strategy_id="quant_math",
				extra_log_data={"risk_usd": 25.0},
				return_execution_result=True,
			)
			state_path = Path(temp_dir) / "trade_logs" / "trade_outcomes_state.json"
			state = json.loads(state_path.read_text(encoding="utf-8"))
			log_lines = (Path(temp_dir) / "trade_logs" / "execution_log.jsonl").read_text(encoding="utf-8").splitlines()

		self.assertTrue(result.success)
		self.assertEqual(result.margin_retry_count, 1)
		self.assertEqual(mock_order_send.call_args.args[0]["volume"], 0.19)
		self.assertEqual(state["pending_positions"]["123"]["initial_risk_usd"], 4.75)
		self.assertEqual(json.loads(log_lines[0])["event"], "margin_sizing_retry")

	@patch("economic_calendar.EconomicCalendarService.evaluate", return_value=SimpleNamespace(allow_new_trade=True))
	@patch("trade_execution.get_symbol_tick", return_value=SimpleNamespace(ask=1.2, bid=1.1))
	@patch("trade_execution.get_symbol_info", return_value=SimpleNamespace(volume_min=0.01, volume_max=10.0, volume_step=0.01))
	@patch("trade_execution.validate_lot_size", side_effect=lambda _symbol, volume: (volume, ""))
	@patch("trade_execution.validate_symbol", return_value=(True, ""))
	@patch("trade_execution._check_open_position_limit", return_value=(True, ""))
	@patch("trade_execution.mt5.order_send")
	@patch("trade_execution.get_margin_requirement")
	def test_retries_broker_no_money_with_a_smaller_volume(
		self,
		mock_margin_requirement,
		mock_order_send,
		_mock_position_limit,
		_mock_validate_symbol,
		_mock_validate_lot,
		_mock_symbol_info,
		_mock_tick,
		_mock_calendar,
	) -> None:
		mock_margin_requirement.side_effect = [
			MarginRequirement(200.0, 200.0, 200.0),
			MarginRequirement(200.0, 200.0, 200.0),
			MarginRequirement(100.0, 200.0, 200.0),
		]
		mock_order_send.side_effect = [
			SimpleNamespace(retcode=getattr(mt5, "TRADE_RETCODE_NO_MONEY", 10019), comment="No money"),
			SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, order=125, deal=126, volume=0.1, price=1.2),
		]
		order_volumes: list[float] = []
		responses = iter(mock_order_send.side_effect)
		mock_order_send.side_effect = lambda request: (
			order_volumes.append(float(request["volume"])) or next(responses)
		)

		result = execute_trade("EURUSD_ecn", "BUY", 0.2, return_execution_result=True)

		self.assertTrue(result.success)
		self.assertEqual(result.margin_retry_count, 1)
		self.assertEqual(order_volumes, [0.2, 0.1])

	@patch("economic_calendar.EconomicCalendarService.evaluate", return_value=SimpleNamespace(allow_new_trade=True))
	@patch("trade_execution.get_symbol_tick", return_value=SimpleNamespace(ask=1.2, bid=1.1))
	@patch("trade_execution.get_symbol_info", return_value=SimpleNamespace(volume_min=0.1, volume_max=10.0, volume_step=0.1))
	@patch("trade_execution.validate_lot_size", return_value=(0.1, ""))
	@patch("trade_execution.validate_symbol", return_value=(True, ""))
	@patch("trade_execution._check_open_position_limit", return_value=(True, ""))
	@patch("trade_execution.mt5.order_send")
	@patch("trade_execution.get_margin_requirement", return_value=MarginRequirement(500.0, 100.0, 100.0))
	def test_does_not_increase_a_below_minimum_retry_to_broker_minimum(
		self,
		_mock_margin_requirement,
		mock_order_send,
		_mock_position_limit,
		_mock_validate_symbol,
		_mock_validate_lot,
		_mock_symbol_info,
		_mock_tick,
		_mock_calendar,
	) -> None:
		result = execute_trade("EURUSD_ecn", "BUY", 0.1, return_execution_result=True)

		self.assertFalse(result.success)
		self.assertIn("no smaller broker-valid volume", result.error_message)
		mock_order_send.assert_not_called()


if __name__ == "__main__":
	unittest.main()