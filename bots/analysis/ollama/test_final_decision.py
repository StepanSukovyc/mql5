"""Unit tests for final decision retry behavior."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from final_decision import make_final_trading_decision


class FinalDecisionRetryTests(unittest.TestCase):
	@patch("final_decision.execute_trade")
	@patch("final_decision.validate_symbol")
	@patch("final_decision.ask_gemini_final_decision")
	@patch("final_decision.count_successful_trades")
	@patch("final_decision._get_gemini_full_control_every_n_trades")
	@patch("final_decision._load_gemini_api_config")
	@patch("final_decision.get_open_positions")
	@patch("final_decision.get_account_state")
	@patch("final_decision.load_predictions")
	def test_trade_failure_excludes_symbol_and_retries_with_next_prediction(
		self,
		mock_load_predictions,
		mock_get_account_state,
		mock_get_open_positions,
		mock_load_gemini_api_config,
		mock_trade_mode,
		mock_count_successful_trades,
		mock_ask_gemini_final_decision,
		mock_validate_symbol,
		mock_execute_trade,
	) -> None:
		mock_load_predictions.return_value = [
			{"symbol": "JP225_ecn", "BUY": 60, "SELL": 30},
			{"symbol": "US30_ecn", "BUY": 55, "SELL": 25},
		]
		mock_get_account_state.return_value = {
			"balance_cap": 5000.0,
			"balance": 4280.60,
			"equity": 3201.53,
			"margin_free": 839.84,
			"margin_percent": 19.62,
		}
		mock_get_open_positions.return_value = []
		mock_load_gemini_api_config.return_value = ("key", "url")
		mock_trade_mode.return_value = 1
		mock_count_successful_trades.return_value = 123
		mock_validate_symbol.return_value = (True, "")
		mock_execute_trade.side_effect = [False, True]

		def _decision_side_effect(predictions, open_positions, account_state, api_key, api_url, excluded_symbols=None, **kwargs):
			if not excluded_symbols:
				return json.dumps(
					{
						"recommended_symbol": "JP225_ecn",
						"action": "BUY",
						"lot_size": 0.01,
						"take_profit": 60000.0,
						"reasoning": "first",
					}
				)

			assert excluded_symbols == ["JP225_ecn"]
			return json.dumps(
				{
					"recommended_symbol": "US30_ecn",
					"action": "SELL",
					"lot_size": 0.01,
					"take_profit": 35000.0,
					"reasoning": "second",
				}
			)

		mock_ask_gemini_final_decision.side_effect = _decision_side_effect

		with tempfile.TemporaryDirectory() as temp_dir:
			predictions_folder = Path(temp_dir) / "predikce"
			service_folder = Path(temp_dir) / "service"
			predictions_folder.mkdir(parents=True, exist_ok=True)

			result = make_final_trading_decision(predictions_folder, service_folder)

		self.assertTrue(result)
		self.assertEqual(mock_ask_gemini_final_decision.call_count, 2)
		self.assertEqual(mock_execute_trade.call_count, 2)
		first_call = mock_execute_trade.call_args_list[0]
		second_call = mock_execute_trade.call_args_list[1]
		self.assertEqual(first_call.args[0], "JP225_ecn")
		self.assertEqual(second_call.args[0], "US30_ecn")


if __name__ == "__main__":
	unittest.main()