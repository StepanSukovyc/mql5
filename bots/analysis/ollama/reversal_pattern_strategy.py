from __future__ import annotations

import os
from typing import Dict, List, Optional

from instrument_utils import symbol_matches_patterns
from signal_rules import SignalValidationResult, _is_news_blocked
from strategy_context import count_open_positions_for_strategy, get_reversal_strategy_context


def _get_float_env(name: str, default: float) -> float:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		return float(raw)
	except (TypeError, ValueError):
		return default


def _get_bool_env(name: str, default: bool) -> bool:
	raw = os.getenv(name)
	if raw is None:
		return default
	return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_csv_env(name: str, default: str) -> List[str]:
	raw = os.getenv(name, default)
	return [item.strip() for item in raw.split(",") if item.strip()]


def _latest_indicator_value(market_data: Dict, timeframe: str, indicator: str) -> Optional[float]:
	series = market_data.get("oscillators", {}).get(timeframe, {}).get(indicator, [])
	if not series:
		return None
	try:
		return float(series[-1]["value"])
	except (KeyError, TypeError, ValueError):
		return None


def _latest_candles(market_data: Dict, timeframe: str, count: int) -> List[Dict]:
	rows = market_data.get("candles", {}).get(timeframe, [])
	if len(rows) < count:
		return []
	return list(rows[-count:])


def _candle_value(candle: Dict, field: str) -> Optional[float]:
	try:
		return float(candle[field])
	except (KeyError, TypeError, ValueError):
		return None


def _body_size(candle: Dict) -> Optional[float]:
	open_price = _candle_value(candle, "open")
	close_price = _candle_value(candle, "close")
	if open_price is None or close_price is None:
		return None
	return abs(close_price - open_price)


def _range_size(candle: Dict) -> Optional[float]:
	high_price = _candle_value(candle, "high")
	low_price = _candle_value(candle, "low")
	if high_price is None or low_price is None:
		return None
	return max(high_price - low_price, 0.0)


def _close_position_in_range(candle: Dict) -> Optional[float]:
	high_price = _candle_value(candle, "high")
	low_price = _candle_value(candle, "low")
	close_price = _candle_value(candle, "close")
	if high_price is None or low_price is None or close_price is None:
		return None
	price_range = high_price - low_price
	if price_range <= 0:
		return None
	return (close_price - low_price) / price_range


def _is_bullish_engulfing(previous_candle: Dict, current_candle: Dict) -> bool:
	prev_open = _candle_value(previous_candle, "open")
	prev_close = _candle_value(previous_candle, "close")
	current_open = _candle_value(current_candle, "open")
	current_close = _candle_value(current_candle, "close")
	if None in {prev_open, prev_close, current_open, current_close}:
		return False
	return (
		prev_close < prev_open
		and current_close > current_open
		and current_open <= prev_close
		and current_close >= prev_open
	)


def _is_bearish_engulfing(previous_candle: Dict, current_candle: Dict) -> bool:
	prev_open = _candle_value(previous_candle, "open")
	prev_close = _candle_value(previous_candle, "close")
	current_open = _candle_value(current_candle, "open")
	current_close = _candle_value(current_candle, "close")
	if None in {prev_open, prev_close, current_open, current_close}:
		return False
	return (
		prev_close > prev_open
		and current_close < current_open
		and current_open >= prev_close
		and current_close <= prev_open
	)


def _is_hammer_like(candle: Dict, bullish: bool) -> bool:
	open_price = _candle_value(candle, "open")
	high_price = _candle_value(candle, "high")
	low_price = _candle_value(candle, "low")
	close_price = _candle_value(candle, "close")
	if None in {open_price, high_price, low_price, close_price}:
		return False
	body = abs(close_price - open_price)
	price_range = max(high_price - low_price, 0.0)
	if price_range <= 0:
		return False
	upper_wick = high_price - max(open_price, close_price)
	lower_wick = min(open_price, close_price) - low_price
	min_wick_ratio = _get_float_env("REVERSAL_PINBAR_MIN_WICK_TO_BODY_RATIO", 2.0)
	max_opposite_wick_ratio = _get_float_env("REVERSAL_PINBAR_MAX_OPPOSITE_WICK_TO_BODY_RATIO", 0.8)
	body_for_ratio = max(body, price_range * 0.05)
	if bullish:
		return close_price >= open_price and lower_wick >= body_for_ratio * min_wick_ratio and upper_wick <= body_for_ratio * max_opposite_wick_ratio
	return close_price <= open_price and upper_wick >= body_for_ratio * min_wick_ratio and lower_wick <= body_for_ratio * max_opposite_wick_ratio


def is_reversal_strategy_enabled() -> bool:
	return _get_bool_env("REVERSAL_STRATEGY_ENABLED", False)


def get_reversal_symbol_whitelist() -> List[str]:
	return _parse_csv_env(
		"REVERSAL_SYMBOL_WHITELIST",
		"EURUSD*,GBPUSD*,USDJPY*,AUDUSD*,USDCHF*",
	)


def validate_reversal_pattern_signal(symbol: str, action: str, market_data: Dict) -> SignalValidationResult:
	reasons: List[str] = []
	metrics: Dict[str, float] = {}

	candles_h1 = _latest_candles(market_data, "1h", 2)
	atr_h1 = _latest_indicator_value(market_data, "1h", "atr14")
	rsi_h1 = _latest_indicator_value(market_data, "1h", "rsi")
	rsi2_h1 = _latest_indicator_value(market_data, "1h", "rsi2")
	bb_upper_h1 = _latest_indicator_value(market_data, "1h", "bb_upper20")
	bb_lower_h1 = _latest_indicator_value(market_data, "1h", "bb_lower20")
	vwap_h4 = _latest_indicator_value(market_data, "4h", "vwap")
	adx_h4 = _latest_indicator_value(market_data, "4h", "adx14")
	spread_points = market_data.get("spread_snapshot", {}).get("spread_points")

	if len(candles_h1) < 2 or None in {atr_h1, rsi_h1, rsi2_h1, bb_upper_h1, bb_lower_h1, vwap_h4, adx_h4}:
		return SignalValidationResult(False, ["missing_indicator_data"], "unknown", metrics)

	previous_candle, current_candle = candles_h1
	current_open = _candle_value(current_candle, "open")
	current_high = _candle_value(current_candle, "high")
	current_low = _candle_value(current_candle, "low")
	current_close = _candle_value(current_candle, "close")
	previous_close = _candle_value(previous_candle, "close")
	current_body = _body_size(current_candle)
	current_range = _range_size(current_candle)
	close_position = _close_position_in_range(current_candle)

	if None in {current_open, current_high, current_low, current_close, previous_close, current_body, current_range, close_position}:
		return SignalValidationResult(False, ["missing_indicator_data"], "unknown", metrics)

	metrics.update(
		{
			"current_open_h1": current_open,
			"current_high_h1": current_high,
			"current_low_h1": current_low,
			"current_close_h1": current_close,
			"previous_close_h1": previous_close,
			"atr_h1": atr_h1,
			"rsi_h1": rsi_h1,
			"rsi2_h1": rsi2_h1,
			"bb_upper_h1": bb_upper_h1,
			"bb_lower_h1": bb_lower_h1,
			"vwap_h4": vwap_h4,
			"adx_h4": adx_h4,
			"spread_points": float(spread_points or 0.0),
			"current_body_h1": current_body,
			"current_range_h1": current_range,
			"close_position_in_range": close_position,
		}
	)

	if not symbol_matches_patterns(symbol, get_reversal_symbol_whitelist()):
		reasons.append("symbol_not_in_reversal_whitelist")

	max_adx = _get_float_env("REVERSAL_MAX_ADX_H4", 22.0)
	max_spread_points = _get_float_env("REVERSAL_MAX_SPREAD_POINTS", 35.0)
	min_pattern_range_ratio = _get_float_env("REVERSAL_MIN_PATTERN_RANGE_ATR_RATIO", 0.8)
	touch_tolerance_ratio = _get_float_env("REVERSAL_EXTREME_TOUCH_ATR_RATIO", 0.20)
	confirmation_ratio = _get_float_env("REVERSAL_MIN_CONFIRMATION_CLOSE_RATIO", 0.55)

	if adx_h4 >= max_adx:
		reasons.append("adx_above_reversal_threshold")
	if spread_points is not None and max_spread_points > 0 and float(spread_points) > max_spread_points:
		reasons.append("spread_above_limit")
	if current_range < (atr_h1 * min_pattern_range_ratio):
		reasons.append("pattern_range_below_threshold")
	if _is_news_blocked(symbol):
		reasons.append("news_blocked")

	touch_tolerance = atr_h1 * touch_tolerance_ratio
	if action == "BUY":
		if current_low > (bb_lower_h1 + touch_tolerance):
			reasons.append("pattern_not_at_lower_extreme")
		if current_close >= vwap_h4:
			reasons.append("close_not_below_vwap")
		if rsi_h1 > _get_float_env("REVERSAL_LONG_RSI_MAX", 45.0) and rsi2_h1 > _get_float_env("REVERSAL_LONG_RSI2_MAX", 25.0):
			reasons.append("rsi_not_supportive_long")
		if not (_is_bullish_engulfing(previous_candle, current_candle) or _is_hammer_like(current_candle, bullish=True)):
			reasons.append("bullish_reversal_pattern_missing")
		if close_position < confirmation_ratio or current_close <= previous_close:
			reasons.append("confirmation_close_too_weak")
	else:
		if current_high < (bb_upper_h1 - touch_tolerance):
			reasons.append("pattern_not_at_upper_extreme")
		if current_close <= vwap_h4:
			reasons.append("close_not_above_vwap")
		if rsi_h1 < _get_float_env("REVERSAL_SHORT_RSI_MIN", 55.0) and rsi2_h1 < _get_float_env("REVERSAL_SHORT_RSI2_MIN", 75.0):
			reasons.append("rsi_not_supportive_short")
		if not (_is_bearish_engulfing(previous_candle, current_candle) or _is_hammer_like(current_candle, bullish=False)):
			reasons.append("bearish_reversal_pattern_missing")
		if (1.0 - close_position) < confirmation_ratio or current_close >= previous_close:
			reasons.append("confirmation_close_too_weak")

	return SignalValidationResult(not reasons, reasons, "reversal", metrics)


def can_activate_reversal_strategy(account_state: Dict, open_positions: List[dict]) -> bool:
	"""Return whether the reversal strategy is enabled and eligible to trade."""
	if not is_reversal_strategy_enabled():
		return False

	reversal = get_reversal_strategy_context()
	balance = float(account_state.get("balance", 0.0) or 0.0)
	raw_free_margin = float(account_state.get("raw_margin_free", account_state.get("margin_free", 0.0)) or 0.0)
	if balance <= 0:
		return False
	margin_percent = (raw_free_margin / balance) * 100.0
	if margin_percent < reversal.activation_margin_percent:
		return False
	if reversal.max_open_positions > 0 and count_open_positions_for_strategy(open_positions, reversal) >= reversal.max_open_positions:
		return False
	return True