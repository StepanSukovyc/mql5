from __future__ import annotations

import unittest
from unittest.mock import patch

from final_decision import _resolve_trade_parameters


class FinalDecisionLocalRiskTests(unittest.TestCase):
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