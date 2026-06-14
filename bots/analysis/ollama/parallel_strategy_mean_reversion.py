from __future__ import annotations

from typing import Dict, List, Optional

from env_utils import get_float_env, parse_csv_env
from instrument_utils import is_secondary_strategy_symbol_allowed
from signal_rules import SignalValidationResult, _is_news_blocked
from strategy_context import count_open_positions_for_strategy, get_parallel_strategy_context


def _latest_indicator_value(market_data: Dict, timeframe: str, indicator: str) -> Optional[float]:
	series = market_data.get("oscillators", {}).get(timeframe, {}).get(indicator, [])
	if not series:
		return None
	try:
		return float(series[-1]["value"])
	except (KeyError, TypeError, ValueError):
		return None


def _latest_close(market_data: Dict, timeframe: str) -> Optional[float]:
	rows = market_data.get("candles", {}).get(timeframe, [])
	if not rows:
		return None
	try:
		return float(rows[-1]["close"])
	except (KeyError, TypeError, ValueError):
		return None


def get_parallel_symbol_whitelist() -> List[str]:
	return parse_csv_env(
		"PARALLEL_SYMBOL_WHITELIST",
		"EURUSD*,GBPUSD*,USDJPY*,AUDUSD*,USDCHF*",
	)


def validate_mean_reversion_signal(symbol: str, action: str, market_data: Dict) -> SignalValidationResult:
	reasons: List[str] = []
	metrics: Dict[str, float] = {}
	whitelist = get_parallel_symbol_whitelist()

	close_h1 = _latest_close(market_data, "1h")
	rsi2_h1 = _latest_indicator_value(market_data, "1h", "rsi2")
	atr_h1 = _latest_indicator_value(market_data, "1h", "atr14")
	bb_upper_h1 = _latest_indicator_value(market_data, "1h", "bb_upper20")
	bb_lower_h1 = _latest_indicator_value(market_data, "1h", "bb_lower20")
	bb_middle_h1 = _latest_indicator_value(market_data, "1h", "bb_middle20")
	vwap_h4 = _latest_indicator_value(market_data, "4h", "vwap")
	adx_h4 = _latest_indicator_value(market_data, "4h", "adx14")
	spread_points = market_data.get("spread_snapshot", {}).get("spread_points")

	if None in {close_h1, rsi2_h1, atr_h1, bb_upper_h1, bb_lower_h1, bb_middle_h1, vwap_h4, adx_h4}:
		return SignalValidationResult(False, ["missing_indicator_data"], "unknown", metrics)

	metrics.update(
		{
			"close_h1": close_h1,
			"rsi2_h1": rsi2_h1,
			"atr_h1": atr_h1,
			"bb_upper_h1": bb_upper_h1,
			"bb_lower_h1": bb_lower_h1,
			"bb_middle_h1": bb_middle_h1,
			"vwap_h4": vwap_h4,
			"adx_h4": adx_h4,
			"spread_points": float(spread_points or 0.0),
			"vwap_distance": abs(close_h1 - vwap_h4),
		}
	)

	if not is_secondary_strategy_symbol_allowed(symbol, whitelist):
		reasons.append("symbol_not_in_parallel_whitelist")

	max_adx = get_float_env("PARALLEL_MAX_ADX_H4", 18.0)
	max_spread_points = get_float_env("PARALLEL_MAX_SPREAD_POINTS", 35.0)
	vwap_distance_multiplier = get_float_env("PARALLEL_VWAP_ATR_DISTANCE_MULTIPLIER", 1.2)

	if adx_h4 >= max_adx:
		reasons.append("adx_above_range_threshold")
	if spread_points is not None and max_spread_points > 0 and float(spread_points) > max_spread_points:
		reasons.append("spread_above_limit")
	if abs(close_h1 - vwap_h4) < (atr_h1 * vwap_distance_multiplier):
		reasons.append("vwap_distance_below_threshold")
	if _is_news_blocked(symbol):
		reasons.append("news_blocked")

	if action == "BUY":
		if close_h1 >= bb_lower_h1:
			reasons.append("close_not_below_lower_band")
		if rsi2_h1 > get_float_env("PARALLEL_LONG_RSI2_MAX", 5.0):
			reasons.append("rsi2_not_extreme_long")
	else:
		if close_h1 <= bb_upper_h1:
			reasons.append("close_not_above_upper_band")
		if rsi2_h1 < get_float_env("PARALLEL_SHORT_RSI2_MIN", 95.0):
			reasons.append("rsi2_not_extreme_short")

	return SignalValidationResult(not reasons, reasons, "range", metrics)


def can_activate_parallel_strategy(account_state: Dict, open_positions: List[dict]) -> bool:
	"""Return whether the parallel strategy is eligible to trade under its own margin gate."""
	parallel = get_parallel_strategy_context()
	balance = float(account_state.get("balance", 0.0) or 0.0)
	raw_free_margin = float(account_state.get("raw_margin_free", account_state.get("margin_free", 0.0)) or 0.0)
	if balance <= 0:
		return False
	margin_percent = (raw_free_margin / balance) * 100.0
	if margin_percent < parallel.activation_margin_percent:
		return False
	if parallel.max_open_positions > 0 and count_open_positions_for_strategy(open_positions, parallel) >= parallel.max_open_positions:
		return False
	return True