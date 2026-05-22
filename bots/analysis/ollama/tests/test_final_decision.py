"""Unit tests for final decision retry behavior."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from final_decision import _resolve_trade_parameters, make_final_trading_decision


class FinalDecisionRetryTests(unittest.TestCase):
	@patch("final_decision.estimate_order_profit")
	@patch("final_decision.get_symbol_info")
	@patch("final_decision.get_current_price")
	def test_cfd_tp_is_adjusted_to_cover_modeled_fee(
		self,
		mock_get_current_price,
		mock_get_symbol_info,
		mock_estimate_order_profit,
	) -> None:
		mock_get_current_price.return_value = 100.0
		mock_get_symbol_info.return_value = type("SymbolInfo", (), {"digits": 2})()

		def _estimate_profit(symbol, action, volume, open_price, close_price):
			return close_price - open_price

		mock_estimate_order_profit.side_effect = _estimate_profit

		lot_size, take_profit = _resolve_trade_parameters(
			gemini_full_control_mode=True,
			gemini_lot_size=0.01,
			gemini_take_profit=100.05,
			account_state={},
			symbol="JP225_ecn",
			action="BUY",
			symbol_is_crypto=False,
			symbol_is_cfd=True,
		)

		self.assertEqual(lot_size, 0.01)
		self.assertEqual(take_profit, 100.2)

	@patch("final_decision.estimate_order_profit")
	@patch("final_decision.get_symbol_info")
	@patch("final_decision.get_current_price")
	def test_cfd_tp_is_disabled_when_no_safe_target_exists(
		self,
		mock_get_current_price,
		mock_get_symbol_info,
		mock_estimate_order_profit,
	) -> None:
		mock_get_current_price.return_value = 100.0
		mock_get_symbol_info.return_value = type("SymbolInfo", (), {"digits": 2})()
		mock_estimate_order_profit.return_value = 0.05

		lot_size, take_profit = _resolve_trade_parameters(
			gemini_full_control_mode=True,
			gemini_lot_size=0.01,
			gemini_take_profit=100.05,
			account_state={},
			symbol="JP225_ecn",
			action="BUY",
			symbol_is_crypto=False,
			symbol_is_cfd=True,
		)

		self.assertEqual(lot_size, 0.01)
		self.assertIsNone(take_profit)

	@patch("final_decision.execute_trade")
	@patch("final_decision.validate_symbol")
	@patch("final_decision.calculate_synthetic_risk_plan")
	@patch("final_decision.validate_trend_following_signal")
	@patch("final_decision._load_market_data_for_symbol")
	@patch("final_decision.ask_gemini_final_decision")
	@patch("final_decision.count_successful_trades_since")
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
		mock_count_successful_trades_since,
		mock_ask_gemini_final_decision,
		mock_load_market_data_for_symbol,
		mock_validate_trend_following_signal,
		mock_calculate_synthetic_risk_plan,
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
		mock_load_gemini_api_config.return_value = SimpleNamespace(
			credentials_path="C:/vertex/service-account.json",
			project="demo-project",
			region="europe-west4",
			model="gemini-2.5-flash",
			fallback_models=("gemini-2.5-flash",),
		)
		mock_trade_mode.return_value = 1
		mock_count_successful_trades.return_value = 123
		mock_count_successful_trades_since.return_value = 0
		mock_load_market_data_for_symbol.return_value = {"oscillators": {}, "candles": {}}
		mock_validate_trend_following_signal.return_value = SimpleNamespace(allowed=True, reason_codes=[], regime_state="trend", metrics={})
		mock_calculate_synthetic_risk_plan.return_value = SimpleNamespace(
			risk_usd=25.0,
			synthetic_stop_price=99.0,
			synthetic_stop_distance=1.0,
			take_profit_price=102.0,
			lot_size=0.01,
		)
		mock_validate_symbol.return_value = (True, "")
		mock_execute_trade.side_effect = [False, True]

		def _decision_side_effect(predictions, open_positions, account_state, gemini_config, excluded_symbols=None, **kwargs):
			return json.dumps(
				{
					"recommended_symbol": "JP225_ecn",
					"action": "BUY",
					"lot_size": 0.01,
					"take_profit": 60000.0,
					"reasoning": "first",
				}
			)

		mock_ask_gemini_final_decision.side_effect = _decision_side_effect

		with tempfile.TemporaryDirectory() as temp_dir:
			predictions_folder = Path(temp_dir) / "predikce"
			service_folder = Path(temp_dir) / "service"
			predictions_folder.mkdir(parents=True, exist_ok=True)

			result = make_final_trading_decision(predictions_folder, service_folder)

		self.assertTrue(result)
		self.assertEqual(mock_ask_gemini_final_decision.call_count, 1)
		self.assertEqual(mock_execute_trade.call_count, 2)
		first_call = mock_execute_trade.call_args_list[0]
		second_call = mock_execute_trade.call_args_list[1]
		self.assertEqual(first_call.args[0], "JP225_ecn")
		self.assertEqual(second_call.args[0], "US30_ecn")

	@patch.dict(
		os.environ,
		{
			"PRIMARY_MAX_OPEN_POSITIONS": "0",
			"PRIMARY_MAX_TRADES_PER_DAY": "0",
			"PRIMARY_MAX_TRADES_PER_SYMBOL_PER_DAY": "0",
		},
		clear=False,
	)
	@patch("final_decision.execute_trade")
	@patch("final_decision.validate_symbol")
	@patch("final_decision.calculate_synthetic_risk_plan")
	@patch("final_decision.validate_trend_following_signal")
	@patch("final_decision._load_market_data_for_symbol")
	@patch("final_decision.ask_gemini_final_decision")
	@patch("final_decision.count_successful_trades_since")
	@patch("final_decision.count_successful_trades_today")
	@patch("final_decision.count_successful_trades")
	@patch("final_decision._get_gemini_full_control_every_n_trades")
	@patch("final_decision._load_gemini_api_config")
	@patch("final_decision.get_open_positions")
	@patch("final_decision.get_account_state")
	@patch("final_decision.load_predictions")
	def test_zero_limits_disable_profile_position_and_trade_caps(
		self,
		mock_load_predictions,
		mock_get_account_state,
		mock_get_open_positions,
		mock_load_gemini_api_config,
		mock_trade_mode,
		mock_count_successful_trades,
		mock_count_successful_trades_today,
		mock_count_successful_trades_since,
		mock_ask_gemini_final_decision,
		mock_load_market_data_for_symbol,
		mock_validate_trend_following_signal,
		mock_calculate_synthetic_risk_plan,
		mock_validate_symbol,
		mock_execute_trade,
	) -> None:
		mock_load_predictions.return_value = [{"symbol": "EURUSD_ecn", "BUY": 60, "SELL": 20}]
		mock_get_account_state.return_value = {
			"balance_cap": 5000.0,
			"balance": 4280.60,
			"equity": 3201.53,
			"margin_free": 839.84,
			"margin_percent": 19.62,
		}
		mock_get_open_positions.return_value = []
		mock_load_gemini_api_config.return_value = SimpleNamespace(
			credentials_path="C:/vertex/service-account.json",
			project="demo-project",
			region="europe-west4",
			model="gemini-2.5-flash",
			fallback_models=("gemini-2.5-flash",),
		)
		mock_trade_mode.return_value = 1
		mock_count_successful_trades.return_value = 123
		mock_count_successful_trades_today.return_value = 99
		mock_count_successful_trades_since.return_value = 0
		mock_load_market_data_for_symbol.return_value = {"oscillators": {}, "candles": {}}
		mock_validate_trend_following_signal.return_value = SimpleNamespace(allowed=True, reason_codes=[], regime_state="trend", metrics={})
		mock_calculate_synthetic_risk_plan.return_value = SimpleNamespace(
			risk_usd=25.0,
			synthetic_stop_price=99.0,
			synthetic_stop_distance=1.0,
			take_profit_price=1.11,
			lot_size=0.01,
		)
		mock_validate_symbol.return_value = (True, "")
		mock_execute_trade.return_value = True
		mock_ask_gemini_final_decision.return_value = json.dumps(
			{
				"recommended_symbol": "EURUSD_ecn",
				"action": "BUY",
				"lot_size": 0.01,
				"take_profit": 1.11,
				"reasoning": "go",
			}
		)

		with tempfile.TemporaryDirectory() as temp_dir:
			predictions_folder = Path(temp_dir) / "predikce"
			service_folder = Path(temp_dir) / "service"
			predictions_folder.mkdir(parents=True, exist_ok=True)

			result = make_final_trading_decision(predictions_folder, service_folder)

		self.assertTrue(result)
		self.assertEqual(mock_execute_trade.call_count, 1)

	@patch.dict(
		os.environ,
		{
			"SYMBOL_TRADE_COOLDOWN_MINUTES": "60",
		},
		clear=False,
	)
	@patch("final_decision.execute_trade")
	@patch("final_decision.validate_symbol")
	@patch("final_decision.calculate_synthetic_risk_plan")
	@patch("final_decision.validate_trend_following_signal")
	@patch("final_decision._load_market_data_for_symbol")
	@patch("final_decision.ask_gemini_final_decision")
	@patch("final_decision.count_successful_trades_since")
	@patch("final_decision.count_successful_trades_today")
	@patch("final_decision.count_successful_trades")
	@patch("final_decision._get_gemini_full_control_every_n_trades")
	@patch("final_decision._load_gemini_api_config")
	@patch("final_decision.get_open_positions")
	@patch("final_decision.get_account_state")
	@patch("final_decision.load_predictions")
	def test_symbol_cooldown_excludes_recent_symbol_and_uses_next_candidate(
		self,
		mock_load_predictions,
		mock_get_account_state,
		mock_get_open_positions,
		mock_load_gemini_api_config,
		mock_trade_mode,
		mock_count_successful_trades,
		mock_count_successful_trades_today,
		mock_count_successful_trades_since,
		mock_ask_gemini_final_decision,
		mock_load_market_data_for_symbol,
		mock_validate_trend_following_signal,
		mock_calculate_synthetic_risk_plan,
		mock_validate_symbol,
		mock_execute_trade,
	) -> None:
		mock_load_predictions.return_value = [
			{"symbol": "VIX_ecn", "BUY": 60, "SELL": 20},
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
		mock_load_gemini_api_config.return_value = SimpleNamespace(
			credentials_path="C:/vertex/service-account.json",
			project="demo-project",
			region="europe-west4",
			model="gemini-2.5-flash",
			fallback_models=("gemini-2.5-flash",),
		)
		mock_trade_mode.return_value = 1
		mock_count_successful_trades.return_value = 6
		mock_count_successful_trades_today.return_value = 0
		mock_load_market_data_for_symbol.return_value = {"oscillators": {}, "candles": {}}
		mock_validate_trend_following_signal.return_value = SimpleNamespace(allowed=True, reason_codes=[], regime_state="trend", metrics={})
		mock_calculate_synthetic_risk_plan.return_value = SimpleNamespace(
			risk_usd=25.0,
			synthetic_stop_price=99.0,
			synthetic_stop_distance=1.0,
			take_profit_price=35000.0,
			lot_size=0.01,
		)
		def _recent_trades_side_effect(service_folder, *, strategy_id=None, symbol=None, lookback=None, now_utc=None):
			return 1 if symbol == "VIX_ecn" else 0

		mock_count_successful_trades_since.side_effect = _recent_trades_side_effect
		mock_validate_symbol.return_value = (True, "")
		mock_execute_trade.return_value = True

		def _decision_side_effect(predictions, open_positions, account_state, gemini_config, excluded_symbols=None, **kwargs):
			return json.dumps(
				{
					"recommended_symbol": "VIX_ecn",
					"action": "BUY",
					"lot_size": 0.01,
					"take_profit": 100.0,
					"reasoning": "first",
				}
			)

		mock_ask_gemini_final_decision.side_effect = _decision_side_effect

		with tempfile.TemporaryDirectory() as temp_dir:
			predictions_folder = Path(temp_dir) / "predikce"
			service_folder = Path(temp_dir) / "service"
			predictions_folder.mkdir(parents=True, exist_ok=True)

			result = make_final_trading_decision(predictions_folder, service_folder)

		self.assertTrue(result)
		self.assertEqual(mock_ask_gemini_final_decision.call_count, 1)
		self.assertEqual(mock_execute_trade.call_count, 1)
		self.assertEqual(mock_execute_trade.call_args.args[0], "US30_ecn")

	@patch("final_decision.execute_trade")
	@patch("final_decision.validate_symbol")
	@patch("final_decision.calculate_synthetic_risk_plan")
	@patch("final_decision.validate_mean_reversion_signal")
	@patch("final_decision.validate_trend_following_signal")
	@patch("final_decision.can_activate_parallel_strategy")
	@patch("final_decision._load_market_data_for_symbol")
	@patch("final_decision.ask_gemini_final_decision")
	@patch("final_decision.count_successful_trades_since")
	@patch("final_decision.count_successful_trades_today")
	@patch("final_decision.count_successful_trades")
	@patch("final_decision._load_gemini_api_config")
	@patch("final_decision.get_open_positions")
	@patch("final_decision.get_account_state")
	@patch("final_decision.load_predictions")
	def test_same_state_reuses_cached_gemini_advisory(
		self,
		mock_load_predictions,
		mock_get_account_state,
		mock_get_open_positions,
		mock_load_gemini_api_config,
		mock_count_successful_trades,
		mock_count_successful_trades_today,
		mock_count_successful_trades_since,
		mock_ask_gemini_final_decision,
		mock_load_market_data_for_symbol,
		mock_can_activate_parallel_strategy,
		mock_validate_trend_following_signal,
		mock_validate_mean_reversion_signal,
		mock_calculate_synthetic_risk_plan,
		mock_validate_symbol,
		mock_execute_trade,
	) -> None:
		mock_load_predictions.return_value = [{"symbol": "EURUSD_ecn", "BUY": 60, "SELL": 20}]
		mock_get_account_state.return_value = {
			"balance_cap": 5000.0,
			"balance": 4280.60,
			"equity": 3201.53,
			"margin_free": 839.84,
			"margin_percent": 19.62,
		}
		mock_get_open_positions.return_value = []
		mock_load_gemini_api_config.return_value = SimpleNamespace(
			credentials_path="C:/vertex/service-account.json",
			project="demo-project",
			region="europe-west4",
			model="gemini-2.5-flash",
			fallback_models=("gemini-2.5-flash",),
		)
		mock_count_successful_trades.return_value = 5
		mock_count_successful_trades_today.return_value = 0
		mock_count_successful_trades_since.return_value = 0
		mock_load_market_data_for_symbol.return_value = {"oscillators": {}, "candles": {}}
		mock_can_activate_parallel_strategy.return_value = False
		mock_validate_trend_following_signal.return_value = SimpleNamespace(allowed=True, reason_codes=[], regime_state="trend", metrics={})
		mock_validate_mean_reversion_signal.return_value = SimpleNamespace(allowed=False, reason_codes=["unused"], regime_state="range", metrics={})
		mock_calculate_synthetic_risk_plan.return_value = SimpleNamespace(
			risk_usd=25.0,
			synthetic_stop_price=99.0,
			synthetic_stop_distance=1.0,
			take_profit_price=1.11,
			lot_size=0.01,
		)
		mock_validate_symbol.return_value = (True, "")
		mock_execute_trade.return_value = True
		mock_ask_gemini_final_decision.return_value = json.dumps(
			{
				"recommended_symbol": "EURUSD_ecn",
				"action": "BUY",
				"reasoning": "cached",
			}
		)

		with tempfile.TemporaryDirectory() as temp_dir:
			predictions_folder = Path(temp_dir) / "predikce"
			service_folder = Path(temp_dir) / "service"
			predictions_folder.mkdir(parents=True, exist_ok=True)

			first_result = make_final_trading_decision(predictions_folder, service_folder)
			second_result = make_final_trading_decision(predictions_folder, service_folder)

		self.assertTrue(first_result)
		self.assertTrue(second_result)
		self.assertEqual(mock_ask_gemini_final_decision.call_count, 1)
		self.assertEqual(mock_execute_trade.call_count, 2)

	@patch("final_decision.execute_trade")
	@patch("final_decision.validate_symbol")
	@patch("final_decision.calculate_synthetic_risk_plan")
	@patch("final_decision.validate_mean_reversion_signal")
	@patch("final_decision.validate_trend_following_signal")
	@patch("final_decision.can_activate_parallel_strategy")
	@patch("final_decision._load_market_data_for_symbol")
	@patch("final_decision.ask_gemini_final_decision")
	@patch("final_decision.count_successful_trades_since")
	@patch("final_decision.count_successful_trades_today")
	@patch("final_decision.count_successful_trades")
	@patch("final_decision._load_gemini_api_config")
	@patch("final_decision.get_open_positions")
	@patch("final_decision.get_account_state")
	@patch("final_decision.load_predictions")
	def test_parallel_strategy_executes_when_primary_signal_is_rejected(
		self,
		mock_load_predictions,
		mock_get_account_state,
		mock_get_open_positions,
		mock_load_gemini_api_config,
		mock_count_successful_trades,
		mock_count_successful_trades_today,
		mock_count_successful_trades_since,
		mock_ask_gemini_final_decision,
		mock_load_market_data_for_symbol,
		mock_can_activate_parallel_strategy,
		mock_validate_trend_following_signal,
		mock_validate_mean_reversion_signal,
		mock_calculate_synthetic_risk_plan,
		mock_validate_symbol,
		mock_execute_trade,
	) -> None:
		mock_load_predictions.return_value = [{"symbol": "EURUSD_ecn", "BUY": 60, "SELL": 20}]
		mock_get_account_state.return_value = {
			"balance_cap": 5000.0,
			"balance": 4280.60,
			"equity": 3201.53,
			"margin_free": 839.84,
			"margin_percent": 19.62,
		}
		mock_get_open_positions.return_value = []
		mock_load_gemini_api_config.return_value = SimpleNamespace(
			credentials_path="C:/vertex/service-account.json",
			project="demo-project",
			region="europe-west4",
			model="gemini-2.5-flash",
			fallback_models=("gemini-2.5-flash",),
		)
		mock_count_successful_trades.return_value = 5
		mock_count_successful_trades_today.return_value = 0
		mock_count_successful_trades_since.return_value = 0
		mock_load_market_data_for_symbol.return_value = {"oscillators": {}, "candles": {}}
		mock_can_activate_parallel_strategy.return_value = True
		mock_validate_trend_following_signal.return_value = SimpleNamespace(
			allowed=False,
			reason_codes=["adx_below_threshold"],
			regime_state="range",
			metrics={},
		)
		mock_validate_mean_reversion_signal.return_value = SimpleNamespace(
			allowed=True,
			reason_codes=[],
			regime_state="range",
			metrics={},
		)
		mock_calculate_synthetic_risk_plan.return_value = SimpleNamespace(
			risk_usd=17.5,
			synthetic_stop_price=99.0,
			synthetic_stop_distance=1.0,
			take_profit_price=101.5,
			lot_size=0.01,
		)
		mock_validate_symbol.return_value = (True, "")
		mock_execute_trade.return_value = True
		mock_ask_gemini_final_decision.return_value = json.dumps(
			{
				"recommended_symbol": "EURUSD_ecn",
				"action": "BUY",
				"reasoning": "fallback",
			}
		)

		with tempfile.TemporaryDirectory() as temp_dir:
			predictions_folder = Path(temp_dir) / "predikce"
			service_folder = Path(temp_dir) / "service"
			predictions_folder.mkdir(parents=True, exist_ok=True)

			result = make_final_trading_decision(predictions_folder, service_folder)
			audit_file = service_folder / "trade_logs" / "trade_decision_audit.csv"
			snapshot_file = service_folder / "trade_logs" / "trade_decision_snapshot.csv"
			with open(audit_file, "r", encoding="utf-8", newline="") as handle:
				audit_rows = list(csv.DictReader(handle))
			with open(snapshot_file, "r", encoding="utf-8", newline="") as handle:
				snapshot_rows = list(csv.DictReader(handle))
			snapshot_by_label = {row["strategy_label"]: row for row in snapshot_rows}

		self.assertTrue(result)
		self.assertEqual(mock_ask_gemini_final_decision.call_count, 1)
		self.assertEqual(mock_execute_trade.call_count, 1)
		self.assertEqual(mock_execute_trade.call_args.kwargs["strategy_id"], "parallel_mean_reversion")
		self.assertTrue(audit_rows)
		self.assertTrue(any(row["strategy_id"] == "gemini_primary" and row["reason"] == "signal_rejected" for row in audit_rows))
		self.assertTrue(any(row["strategy_id"] == "parallel_mean_reversion" and row["trade_executed"] == "True" for row in audit_rows))
		self.assertEqual(snapshot_by_label["primary"]["reason"], "signal_rejected")
		self.assertEqual(snapshot_by_label["parallel"]["trade_executed"], "True")


if __name__ == "__main__":
	unittest.main()