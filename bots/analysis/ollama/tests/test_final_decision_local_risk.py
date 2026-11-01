from __future__ import annotations

import unittest
import io
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from final_decision import _print_trade_mode, _resolve_trade_parameters, make_final_trading_decision


class FinalDecisionLocalRiskTests(unittest.TestCase):
	@patch("final_decision._attempt_strategy_trade", return_value=True)
	@patch("final_decision._resolve_ollama_cloud_advisory_candidates", return_value=[])
	@patch("final_decision.count_successful_trades", return_value=6)
	@patch("final_decision.get_account_state", return_value={
		"balance_cap": 5000.0,
		"balance": 5000.0,
		"equity": 5000.0,
		"margin_free": 4000.0,
		"margin_percent": 80.0,
	})
	@patch("final_decision.load_predictions", return_value=[
		{"symbol": "EURUSD_ecn", "BUY": 60, "SELL": 20},
	])
	@patch("final_decision.get_open_positions", return_value=[])
	@patch("final_decision.is_quant_strategy_enabled", return_value=False)
	@patch("final_decision.is_ollama_cloud_enabled", return_value=False)
	@patch("final_decision.is_scalping_strategy_enabled", return_value=False)
	def test_decision_cycle_does_not_read_obsolete_cadence(
		self, _scalping, _cloud, _quant, _positions, _predictions, _account,
		_trade_count, _advisory, mock_attempt,
	) -> None:
		original_getenv = os.getenv

		def checked_getenv(name, default=None):
			self.assertNotEqual(name, "GEMINI_FULL_CONTROL_EVERY_N_TRADES")
			return original_getenv(name, default)

		with tempfile.TemporaryDirectory() as temp_dir:
			with patch("final_decision.os.getenv", side_effect=checked_getenv):
				with redirect_stdout(io.StringIO()):
					result = make_final_trading_decision(Path(temp_dir), Path(temp_dir))

		self.assertTrue(result)
		mock_attempt.assert_called_once()
		self.assertEqual(mock_attempt.call_args.kwargs["profile"].label, "primary")

	def test_trade_mode_is_local_regardless_of_obsolete_cadence(self) -> None:
		for cadence in ("1", "3", "0", "invalid"):
			with self.subTest(cadence=cadence):
				with patch.dict(os.environ, {"GEMINI_FULL_CONTROL_EVERY_N_TRADES": cadence}):
					output = io.StringIO()
					with redirect_stdout(output):
						_print_trade_mode(6, 7)
				self.assertIn("Current trade number: #7", output.getvalue())
				self.assertIn("LOCAL SIGNAL/RISK MODE", output.getvalue())
				self.assertNotIn("ADVISORY GEMINI MODE", output.getvalue())
				self.assertNotIn("full-control", output.getvalue())

	@patch("final_decision.calculate_synthetic_risk_plan")
	def test_local_risk_plan_takes_precedence_over_gemini_lot_and_tp(self, mock_plan) -> None:
		mock_plan.return_value = type(
			"RiskPlan",
			(),
			{
				"lot_size": 0.07,
				"take_profit_price": 1.2345,
			},
		)()

		lot_size, take_profit = _resolve_trade_parameters(
			gemini_full_control_mode=False,
			gemini_lot_size=999,
			gemini_take_profit=999,
			account_state={"balance": 5000.0},
			symbol="EURUSD_ecn",
			action="BUY",
			symbol_is_crypto=False,
			symbol_is_cfd=False,
			market_data={"oscillators": {}, "candles": {}},
		)

		self.assertEqual(lot_size, 0.07)
		self.assertEqual(take_profit, 1.2345)


if __name__ == "__main__":
	unittest.main()