from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from instrument_utils import is_secondary_strategy_symbol_allowed
from signal_rules import SignalValidationResult, _is_news_blocked
from strategy_context import count_open_positions_for_strategy, get_quant_strategy_context


@dataclass(frozen=True)
class QuantCandidate:
	symbol: str
	action: str
	score: float
	metrics: Dict[str, float]


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


def _get_int_env(name: str, default: int) -> int:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		return int(raw)
	except (TypeError, ValueError):
		return default


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


def _load_market_data_file(path: Path) -> Optional[Dict]:
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return None


def is_quant_strategy_enabled() -> bool:
	return _get_bool_env("QUANT_STRATEGY_ENABLED", True)


def get_quant_symbol_whitelist() -> List[str]:
	return _parse_csv_env(
		"QUANT_SYMBOL_WHITELIST",
		"",
	)


def _build_quant_signal(symbol: str, market_data: Dict) -> Optional[QuantCandidate]:
	whitelist = get_quant_symbol_whitelist()
	if not is_secondary_strategy_symbol_allowed(symbol, whitelist):
		return None

	candles_h1 = _latest_candles(market_data, "1h", 4)
	if len(candles_h1) < 4:
		return None

	current_candle = candles_h1[-1]
	prior_window = candles_h1[:-1]
	close_now = _candle_value(candles_h1[-1], "close")
	close_prev = _candle_value(candles_h1[-2], "close")
	close_prev2 = _candle_value(candles_h1[-3], "close")
	close_prev3 = _candle_value(candles_h1[-4], "close")
	open_now = _candle_value(current_candle, "open")
	ema20_h1 = _latest_indicator_value(market_data, "1h", "ema20")
	ema50_h4 = _latest_indicator_value(market_data, "4h", "ema50")
	ema200_h4 = _latest_indicator_value(market_data, "4h", "ema200")
	atr_h1 = _latest_indicator_value(market_data, "1h", "atr14")
	rsi_h1 = _latest_indicator_value(market_data, "1h", "rsi")
	adx_h4 = _latest_indicator_value(market_data, "4h", "adx14")
	spread_points = market_data.get("spread_snapshot", {}).get("spread_points")
	recent_high = max((_candle_value(candle, "high") for candle in prior_window), default=None)
	recent_low = min((_candle_value(candle, "low") for candle in prior_window), default=None)

	if None in {
		close_now,
		close_prev,
		close_prev2,
		close_prev3,
		open_now,
		ema20_h1,
		ema50_h4,
		ema200_h4,
		atr_h1,
		rsi_h1,
		adx_h4,
		recent_high,
		recent_low,
	}:
		return None
	if atr_h1 <= 0:
		return None

	move_1 = close_now - close_prev
	move_2 = close_prev - close_prev2
	move_3 = close_prev2 - close_prev3
	momentum = move_1 / atr_h1
	acceleration = (move_1 - move_2) / atr_h1
	second_derivative = ((move_1 - move_2) - (move_2 - move_3)) / atr_h1
	distance_from_ema = (close_now - ema20_h1) / atr_h1
	trend_bias = (ema50_h4 - ema200_h4) / atr_h1
	body_impulse = (close_now - open_now) / atr_h1
	breakout_bias = (close_now - recent_high) / atr_h1 if close_now >= recent_high else (close_now - recent_low) / atr_h1
	direction_streak = 0.0
	for delta in (move_1, move_2, move_3):
		if delta > 0:
			direction_streak += 1.0
		elif delta < 0:
			direction_streak -= 1.0
	rsi_centered = (rsi_h1 - 50.0) / 10.0
	adx_component = max(min((adx_h4 - 18.0) / 10.0, 2.0), -2.0)
	spread_penalty = float(spread_points or 0.0) / max(_get_float_env("QUANT_MAX_SPREAD_POINTS", 30.0), 1.0)

	acceleration_weight = _get_float_env("QUANT_ACCELERATION_WEIGHT", 1.35)
	breakout_weight = _get_float_env("QUANT_BREAKOUT_WEIGHT", 1.10)
	body_weight = _get_float_env("QUANT_BODY_IMPULSE_WEIGHT", 0.90)
	streak_weight = _get_float_env("QUANT_DIRECTION_STREAK_WEIGHT", 0.40)
	curvature_weight = _get_float_env("QUANT_CURVATURE_WEIGHT", 0.70)

	buy_score = (
		momentum
		+ (acceleration * acceleration_weight)
		+ (second_derivative * curvature_weight)
		+ distance_from_ema
		+ trend_bias
		+ (body_impulse * body_weight)
		+ (max(breakout_bias, 0.0) * breakout_weight)
		+ (max(direction_streak, 0.0) * streak_weight)
		+ rsi_centered
		+ adx_component
		- spread_penalty
	)
	sell_score = (
		(-momentum)
		+ ((-acceleration) * acceleration_weight)
		+ ((-second_derivative) * curvature_weight)
		+ (-distance_from_ema)
		+ (-trend_bias)
		+ ((-body_impulse) * body_weight)
		+ (max(-breakout_bias, 0.0) * breakout_weight)
		+ (max(-direction_streak, 0.0) * streak_weight)
		+ (-rsi_centered)
		+ adx_component
		- spread_penalty
	)

	action = "BUY" if buy_score >= sell_score else "SELL"
	score = max(buy_score, sell_score)
	if score < _get_float_env("QUANT_MIN_SIGNAL_SCORE", 2.4):
		return None

	return QuantCandidate(
		symbol=symbol,
		action=action,
		score=score,
		metrics={
			"momentum": momentum,
			"acceleration": acceleration,
			"curvature": second_derivative,
			"distance_from_ema": distance_from_ema,
			"trend_bias": trend_bias,
			"body_impulse": body_impulse,
			"breakout_bias": breakout_bias,
			"direction_streak": direction_streak,
			"rsi_centered": rsi_centered,
			"adx_h4": adx_h4,
			"spread_points": float(spread_points or 0.0),
			"buy_score": buy_score,
			"sell_score": sell_score,
		},
	)


def build_quant_candidates(source_folder: Path) -> List[QuantCandidate]:
	if not is_quant_strategy_enabled() or not source_folder.exists():
		return []

	max_candidates = max(_get_int_env("QUANT_MAX_CANDIDATES", 12), 0)
	candidates: List[QuantCandidate] = []
	for market_data_file in source_folder.glob("*.json"):
		symbol = market_data_file.stem
		market_data = _load_market_data_file(market_data_file)
		if market_data is None:
			continue
		candidate = _build_quant_signal(symbol, market_data)
		if candidate is not None:
			candidates.append(candidate)

	ordered = sorted(candidates, key=lambda item: item.score, reverse=True)
	if max_candidates == 0:
		return ordered
	return ordered[:max_candidates]


def validate_quant_signal(symbol: str, action: str, market_data: Dict) -> SignalValidationResult:
	reasons: List[str] = []
	metrics: Dict[str, float] = {}
	whitelist = get_quant_symbol_whitelist()

	if not is_secondary_strategy_symbol_allowed(symbol, whitelist):
		return SignalValidationResult(False, ["symbol_not_in_quant_whitelist"], "quant", metrics)

	candidate = _build_quant_signal(symbol, market_data)
	if candidate is None:
		return SignalValidationResult(False, ["quant_score_below_threshold"], "quant", metrics)

	metrics.update(candidate.metrics)
	max_spread_points = _get_float_env("QUANT_MAX_SPREAD_POINTS", 30.0)
	min_adx = _get_float_env("QUANT_MIN_ADX_H4", 16.0)
	min_distance = _get_float_env("QUANT_MIN_EMA_DISTANCE_ATR", 0.15)
	rsi_long_min = _get_float_env("QUANT_LONG_RSI_MIN", 54.0)
	rsi_short_max = _get_float_env("QUANT_SHORT_RSI_MAX", 46.0)
	min_breakout_bias = _get_float_env("QUANT_MIN_BREAKOUT_BIAS_ATR", -0.05)
	min_body_impulse = _get_float_env("QUANT_MIN_BODY_IMPULSE_ATR", 0.05)

	adx_h4 = float(candidate.metrics.get("adx_h4", 0.0))
	spread_points = float(candidate.metrics.get("spread_points", 0.0))
	distance_from_ema = float(candidate.metrics.get("distance_from_ema", 0.0))
	body_impulse = float(candidate.metrics.get("body_impulse", 0.0))
	breakout_bias = float(candidate.metrics.get("breakout_bias", 0.0))
	rsi_centered = float(candidate.metrics.get("rsi_centered", 0.0))
	rsi_h1 = (rsi_centered * 10.0) + 50.0

	if adx_h4 < min_adx:
		reasons.append("adx_below_quant_threshold")
	if max_spread_points > 0 and spread_points > max_spread_points:
		reasons.append("spread_above_limit")
	if _is_news_blocked(symbol):
		reasons.append("news_blocked")

	if action == "BUY":
		if candidate.action != "BUY":
			reasons.append("quant_direction_conflict")
		if distance_from_ema < min_distance:
			reasons.append("quant_distance_too_small")
		if body_impulse < min_body_impulse:
			reasons.append("quant_distance_too_small")
		if breakout_bias < min_breakout_bias:
			reasons.append("quant_score_below_threshold")
		if rsi_h1 < rsi_long_min:
			reasons.append("quant_rsi_not_supportive_long")
	else:
		if candidate.action != "SELL":
			reasons.append("quant_direction_conflict")
		if distance_from_ema > -min_distance:
			reasons.append("quant_distance_too_small")
		if body_impulse > -min_body_impulse:
			reasons.append("quant_distance_too_small")
		if breakout_bias > -min_breakout_bias:
			reasons.append("quant_score_below_threshold")
		if rsi_h1 > rsi_short_max:
			reasons.append("quant_rsi_not_supportive_short")

	return SignalValidationResult(not reasons, reasons, "quant", metrics)


def can_activate_quant_strategy(account_state: Dict, open_positions: List[dict]) -> bool:
	if not is_quant_strategy_enabled():
		return False

	quant = get_quant_strategy_context()
	balance = float(account_state.get("balance", 0.0) or 0.0)
	raw_free_margin = float(account_state.get("raw_margin_free", account_state.get("margin_free", 0.0)) or 0.0)
	if balance <= 0:
		return False
	margin_percent = (raw_free_margin / balance) * 100.0
	if margin_percent < quant.activation_margin_percent:
		return False
	if quant.max_open_positions > 0 and count_open_positions_for_strategy(open_positions, quant) >= quant.max_open_positions:
		return False
	return True