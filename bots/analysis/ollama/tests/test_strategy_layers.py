from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from parallel_strategy_mean_reversion import can_activate_parallel_strategy, validate_mean_reversion_signal
from profit_protection_strategy import (
	calculate_profit_protection_activation_usd,
	calculate_profit_protection_target_profit_usd,
	get_profit_protection_context_for_position,
	is_position_under_profit_protection,
)
from reversal_pattern_strategy import can_activate_reversal_strategy, validate_reversal_pattern_signal
from risk_engine import calculate_synthetic_risk_plan
from signal_rules import validate_trend_following_signal
from strategy_context import get_index_strategy_context, get_parallel_strategy_context, get_primary_strategy_context, get_reversal_strategy_context, is_strategy_trade_window_open, position_belongs_to_strategy


def _build_market_data(
	*,
	spread_points: float = 10.0,
	adx_h4: float = 25.0,
	close_h1: float = 100.0,
	open_h1: float = 99.2,
	high_h1: float = 100.4,
	low_h1: float = 98.8,
	prev_open_h1: float = 100.4,
	prev_high_h1: float = 100.7,
	prev_low_h1: float = 99.0,
	prev_close_h1: float = 99.1,
	rsi_h1: float = 60.0,
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
			"1h": [
				{
					"time": "2026-01-01T23:00:00+00:00",
					"open": prev_open_h1,
					"high": prev_high_h1,
					"low": prev_low_h1,
					"close": prev_close_h1,
				},
				{
					"time": "2026-01-02T00:00:00+00:00",
					"open": open_h1,
					"high": high_h1,
					"low": low_h1,
					"close": close_h1,
				},
			],
		},
		"oscillators": {
			"1h": {
				"ema20": _series(95.0),
				"rsi": [{"time": "2026-01-02T00:00:00+00:00", "value": rsi_h1}],
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

	def test_reversal_pattern_rules_allow_valid_long(self) -> None:
		result = validate_reversal_pattern_signal(
			"EURUSD_ecn",
			"BUY",
			_build_market_data(
				adx_h4=18.0,
				close_h1=100.5,
				open_h1=98.9,
				high_h1=100.8,
				low_h1=98.7,
				prev_open_h1=100.3,
				prev_high_h1=100.4,
				prev_low_h1=98.8,
				prev_close_h1=99.0,
				rsi_h1=41.0,
				rsi2_h1=18.0,
				bb_lower_h1=99.0,
				vwap_h4=101.4,
			),
		)

		self.assertTrue(result.allowed)
		self.assertEqual(result.regime_state, "reversal")

	def test_reversal_pattern_rules_require_confirmed_pattern(self) -> None:
		result = validate_reversal_pattern_signal(
			"EURUSD_ecn",
			"BUY",
			_build_market_data(
				adx_h4=18.0,
				close_h1=99.4,
				open_h1=99.6,
				high_h1=99.8,
				low_h1=98.9,
				prev_open_h1=100.0,
				prev_high_h1=100.3,
				prev_low_h1=99.0,
				prev_close_h1=99.5,
				rsi_h1=42.0,
				rsi2_h1=20.0,
				bb_lower_h1=99.0,
				vwap_h4=101.4,
			),
		)

		self.assertFalse(result.allowed)
		self.assertIn("bullish_reversal_pattern_missing", result.reason_codes)


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

	@patch.dict(os.environ, {"INDEX_STRATEGY_ENABLED": "true"}, clear=False)
	def test_index_strategy_position_is_recognized(self) -> None:
		index_context = get_index_strategy_context()
		index_position = {"magic": index_context.magic, "comment": f"ga:{index_context.strategy_id}"}

		self.assertTrue(position_belongs_to_strategy(index_position, index_context))

	def test_parallel_activation_uses_derived_margin_threshold_and_position_cap(self) -> None:
		account_state = {"balance": 5000.0, "raw_margin_free": 900.0}
		parallel = get_parallel_strategy_context()
		open_positions = [{"magic": parallel.magic, "comment": f"ga:{parallel.strategy_id}"} for _ in range(parallel.max_open_positions)]

		self.assertFalse(can_activate_parallel_strategy(account_state, open_positions))

	@patch.dict(os.environ, {"REVERSAL_STRATEGY_ENABLED": "true"}, clear=False)
	def test_reversal_activation_uses_enable_flag_and_position_cap(self) -> None:
		account_state = {"balance": 5000.0, "raw_margin_free": 900.0}
		reversal = get_reversal_strategy_context()
		open_positions = [{"magic": reversal.magic, "comment": f"ga:{reversal.strategy_id}"} for _ in range(reversal.max_open_positions)]

		self.assertFalse(can_activate_reversal_strategy(account_state, open_positions))

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


class ProfitProtectionTests(unittest.TestCase):
	@patch.dict(
		os.environ,
		{
			"PROFIT_PROTECTION_ACTIVATION_USD": "0.30",
			"PROFIT_PROTECTION_TARGET_BALANCE_DIVISOR": "10",
			"PROFIT_PROTECTION_ACTIVATION_TARGET_RATIO": "0.50",
		},
		clear=False,
	)
	def test_profit_protection_scales_with_balance_and_volume(self) -> None:
		self.assertEqual(calculate_profit_protection_target_profit_usd(5000.0, 0.01), 5.0)
		self.assertEqual(calculate_profit_protection_activation_usd(5000.0, 0.01), 2.5)
		self.assertEqual(calculate_profit_protection_target_profit_usd(5000.0, 0.02), 10.0)
		self.assertEqual(calculate_profit_protection_activation_usd(5000.0, 0.02), 5.0)

	@patch.dict(
		os.environ,
		{
			"PROFIT_PROTECTION_ACTIVATION_USD": "0.30",
			"PROFIT_PROTECTION_TARGET_BALANCE_DIVISOR": "10",
			"PROFIT_PROTECTION_ACTIVATION_TARGET_RATIO": "0.50",
		},
		clear=False,
	)
	def test_profit_protection_keeps_static_floor_for_small_positions(self) -> None:
		self.assertEqual(calculate_profit_protection_target_profit_usd(100.0, 0.01), 0.1)
		self.assertEqual(calculate_profit_protection_activation_usd(100.0, 0.01), 0.3)

	@patch.dict(os.environ, {"INDEX_STRATEGY_ENABLED": "true", "PROFIT_PROTECTION_STRATEGY_ENABLED": "true"}, clear=False)
	def test_profit_protection_can_manage_index_positions(self) -> None:
		index_context = get_index_strategy_context()
		index_position = {"magic": index_context.magic, "comment": f"ga:{index_context.strategy_id}"}

		resolved_context = get_profit_protection_context_for_position(index_position)

		self.assertIsNotNone(resolved_context)
		assert resolved_context is not None
		self.assertEqual(resolved_context.strategy_id, index_context.strategy_id)
		self.assertTrue(is_position_under_profit_protection(index_position))


if __name__ == "__main__":
	unittest.main()