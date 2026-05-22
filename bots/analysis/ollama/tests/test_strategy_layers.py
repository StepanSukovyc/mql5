from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from parallel_strategy_mean_reversion import can_activate_parallel_strategy, validate_mean_reversion_signal
from risk_engine import calculate_synthetic_risk_plan
from signal_rules import validate_trend_following_signal
from strategy_context import get_parallel_strategy_context, get_primary_strategy_context, is_strategy_trade_window_open, position_belongs_to_strategy


def _build_market_data(
	*,
	spread_points: float = 10.0,
	adx_h4: float = 25.0,
	close_h1: float = 100.0,
	rsi2_h1: float = 4.0,
	bb_upper_h1: float = 103.0,
	bb_lower_h1: float = 101.0,
	vwap_h4: float = 102.0,
) -> dict:
	def _series(value: float):
		return [{"time": "2026-01-01T00:00:00+00:00", "value": value}, {"time": "2026-01-02T00:00:00+00:00", "value": value + 1.0}]

	return {
		"spread_snapshot": {"spread_points": spread_points},
		"candles": {
			"1h": [{"time": "2026-01-02T00:00:00+00:00", "close": close_h1}],
		},
		"oscillators": {
			"1h": {
				"ema20": _series(95.0),
				"rsi": [{"time": "2026-01-02T00:00:00+00:00", "value": 60.0}],
				"rsi2": [{"time": "2026-01-02T00:00:00+00:00", "value": rsi2_h1}],
				"atr14": [{"time": "2026-01-02T00:00:00+00:00", "value": 1.0}],
				"bb_middle20": [{"time": "2026-01-02T00:00:00+00:00", "value": 101.0}],
				"bb_upper20": [{"time": "2026-01-02T00:00:00+00:00", "value": bb_upper_h1}],
				"bb_lower20": [{"time": "2026-01-02T00:00:00+00:00", "value": bb_lower_h1}],
			},
			"4h": {
				"ema50": [{"time": "2026-01-02T00:00:00+00:00", "value": 105.0}],
				"ema200": [{"time": "2026-01-02T00:00:00+00:00", "value": 100.0}],
				"adx14": [{"time": "2026-01-02T00:00:00+00:00", "value": adx_h4}],
				"vwap": [{"time": "2026-01-02T00:00:00+00:00", "value": vwap_h4}],
			},
			"day": {
				"ema200": _series(90.0),
			},
		},
	}


class SignalRuleTests(unittest.TestCase):
	def test_trend_following_rules_allow_valid_long(self) -> None:
		result = validate_trend_following_signal("EURUSD_ecn", "BUY", _build_market_data())

		self.assertTrue(result.allowed)
		self.assertEqual(result.reason_codes, [])
		self.assertEqual(result.regime_state, "trend")

	def test_trend_following_rules_block_excessive_spread(self) -> None:
		result = validate_trend_following_signal("EURUSD_ecn", "BUY", _build_market_data(spread_points=50.0))

		self.assertFalse(result.allowed)
		self.assertIn("spread_above_limit", result.reason_codes)

	def test_mean_reversion_rules_allow_valid_long(self) -> None:
		result = validate_mean_reversion_signal("EURUSD_ecn", "BUY", _build_market_data(adx_h4=16.0))

		self.assertTrue(result.allowed)
		self.assertEqual(result.regime_state, "range")

	def test_mean_reversion_rules_block_non_whitelisted_symbol(self) -> None:
		result = validate_mean_reversion_signal("XAUUSD_ecn", "BUY", _build_market_data(adx_h4=16.0))

		self.assertFalse(result.allowed)
		self.assertIn("symbol_not_in_parallel_whitelist", result.reason_codes)


class RiskEngineTests(unittest.TestCase):
	@patch("risk_engine.get_current_price", return_value=100.0)
	@patch("risk_engine.estimate_order_profit", return_value=-10.0)
	def test_synthetic_risk_plan_uses_percent_risk_and_r_multiple(self, _mock_profit, _mock_price) -> None:
		market_data = _build_market_data()
		account_state = {"balance": 5000.0}

		plan = calculate_synthetic_risk_plan(
			symbol="EURUSD_ecn",
			action="BUY",
			account_state=account_state,
			market_data=market_data,
		)

		self.assertIsNotNone(plan)
		assert plan is not None
		self.assertEqual(plan.risk_usd, 25.0)
		self.assertAlmostEqual(plan.synthetic_stop_distance, 1.5)
		self.assertAlmostEqual(plan.lot_size, 2.5)
		self.assertAlmostEqual(plan.take_profit_distance, 3.3)


class StrategyOwnershipTests(unittest.TestCase):
	@patch.dict(os.environ, {"PRIMARY_STRATEGY_MANAGE_MANUAL_POSITIONS": "true"}, clear=False)
	def test_primary_strategy_can_manage_manual_position(self) -> None:
		primary = get_primary_strategy_context()
		manual_position = {"magic": 0, "comment": "manual entry"}

		self.assertTrue(position_belongs_to_strategy(manual_position, primary))

	def test_parallel_activation_uses_derived_margin_threshold_and_position_cap(self) -> None:
		account_state = {"balance": 5000.0, "raw_margin_free": 900.0}
		parallel = get_parallel_strategy_context()
		open_positions = [{"magic": parallel.magic, "comment": f"ga:{parallel.strategy_id}"} for _ in range(parallel.max_open_positions)]

		self.assertFalse(can_activate_parallel_strategy(account_state, open_positions))

	@patch.dict(
		os.environ,
		{
			"PARALLEL_SESSION_START_HOUR_UTC": "7",
			"PARALLEL_SESSION_END_HOUR_UTC": "19",
			"PARALLEL_FRIDAY_CUTOFF_HOUR_UTC": "15",
		},
		clear=False,
	)
	def test_parallel_strategy_session_window_blocks_outside_hours(self) -> None:
		parallel = get_parallel_strategy_context()

		allowed = is_strategy_trade_window_open(parallel, datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc))
		blocked_after_cutoff = is_strategy_trade_window_open(parallel, datetime(2026, 5, 22, 15, 0, tzinfo=timezone.utc))
		blocked_before_session = is_strategy_trade_window_open(parallel, datetime(2026, 5, 21, 5, 0, tzinfo=timezone.utc))

		self.assertTrue(allowed)
		self.assertFalse(blocked_after_cutoff)
		self.assertFalse(blocked_before_session)


if __name__ == "__main__":
	unittest.main()