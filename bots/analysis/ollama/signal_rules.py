from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from instrument_utils import get_symbol_news_currencies


@dataclass(frozen=True)
class SignalValidationResult:
	allowed: bool
	reason_codes: List[str]
	regime_state: str
	metrics: Dict[str, float]


def _get_float_env(name: str, default: float) -> float:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		return float(raw)
	except (TypeError, ValueError):
		return default


def _get_int_env(name: str, default: int) -> int:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		return int(raw)
	except (TypeError, ValueError):
		return default


def _get_bool_env(name: str, default: bool) -> bool:
	raw = os.getenv(name)
	if raw is None:
		return default
	return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _latest_value(market_data: Dict, timeframe: str, indicator: str) -> Optional[float]:
	series = market_data.get("oscillators", {}).get(timeframe, {}).get(indicator, [])
	if not series:
		return None
	try:
		return float(series[-1]["value"])
	except (KeyError, TypeError, ValueError):
		return None


def _previous_value(market_data: Dict, timeframe: str, indicator: str) -> Optional[float]:
	series = market_data.get("oscillators", {}).get(timeframe, {}).get(indicator, [])
	if len(series) < 2:
		return None
	try:
		return float(series[-2]["value"])
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


def _is_news_blocked(symbol: str) -> bool:
	if not _get_bool_env("NEWS_FILTER_ENABLED", False):
		return False

	url_template = os.getenv("NEWS_FILTER_API_URL", "").strip()
	if not url_template:
		return False

	now = datetime.now(tz=timezone.utc)
	from_date = (now - timedelta(minutes=_get_int_env("NEWS_FILTER_LOOKBACK_MINUTES", 15))).date().isoformat()
	to_date = (now + timedelta(minutes=_get_int_env("NEWS_FILTER_LOOKAHEAD_MINUTES", 30))).date().isoformat()
	url = url_template.format(from_date=from_date, to_date=to_date, token=os.getenv("NEWS_FILTER_API_TOKEN", ""))
	headers = {}
	token = os.getenv("NEWS_FILTER_API_TOKEN", "").strip()
	header_name = os.getenv("NEWS_FILTER_API_TOKEN_HEADER", "Authorization").strip()
	prefix = os.getenv("NEWS_FILTER_API_TOKEN_PREFIX", "Bearer ")
	if token:
		headers[header_name] = f"{prefix}{token}" if prefix is not None else token

	request = Request(url, headers=headers)
	try:
		with urlopen(request, timeout=_get_int_env("NEWS_FILTER_TIMEOUT_SECONDS", 10)) as response:
			payload = json.loads(response.read().decode("utf-8"))
	except (OSError, URLError, ValueError):
		return False

	tracked_currencies = {currency.upper() for currency in get_symbol_news_currencies(symbol)}
	if not tracked_currencies:
		return False
	allowed_impacts = {
		item.strip().lower()
		for item in os.getenv("NEWS_FILTER_IMPACTS", "high").split(",")
		if item.strip()
	}
	lookback_minutes = _get_int_env("NEWS_FILTER_LOOKBACK_MINUTES", 15)
	lookahead_minutes = _get_int_env("NEWS_FILTER_LOOKAHEAD_MINUTES", 30)
	window_start = now - timedelta(minutes=lookback_minutes)
	window_end = now + timedelta(minutes=lookahead_minutes)

	for event in payload if isinstance(payload, list) else []:
		impact = str(event.get("impact", "") or "").strip().lower()
		currency = str(event.get("currency", "") or "").strip().upper()
		if impact not in allowed_impacts or currency not in tracked_currencies:
			continue
		event_time_raw = event.get("date") or event.get("time") or event.get("datetime")
		if not isinstance(event_time_raw, str):
			continue
		try:
			event_time = datetime.fromisoformat(event_time_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
		except ValueError:
			continue
		if window_start <= event_time <= window_end:
			return True

	return False


def validate_trend_following_signal(symbol: str, action: str, market_data: Dict) -> SignalValidationResult:
	reasons: List[str] = []
	metrics: Dict[str, float] = {}

	close_h1 = _latest_close(market_data, "1h")
	ema20_h1 = _latest_value(market_data, "1h", "ema20")
	rsi_h1 = _latest_value(market_data, "1h", "rsi")
	atr_h1 = _latest_value(market_data, "1h", "atr14")
	adx_h4 = _latest_value(market_data, "4h", "adx14")
	ema50_h4 = _latest_value(market_data, "4h", "ema50")
	ema200_h4 = _latest_value(market_data, "4h", "ema200")
	ema200_d1 = _latest_value(market_data, "day", "ema200")
	ema200_d1_prev = _previous_value(market_data, "day", "ema200")

	if None in {close_h1, ema20_h1, rsi_h1, atr_h1, adx_h4, ema50_h4, ema200_h4, ema200_d1, ema200_d1_prev}:
		return SignalValidationResult(False, ["missing_indicator_data"], "unknown", metrics)

	atr_ratio_percent = (atr_h1 / close_h1) * 100.0 if close_h1 else 0.0
	spread_points = market_data.get("spread_snapshot", {}).get("spread_points")
	metrics.update(
		{
			"close_h1": close_h1,
			"ema20_h1": ema20_h1,
			"rsi_h1": rsi_h1,
			"atr_h1": atr_h1,
			"atr_ratio_percent": atr_ratio_percent,
			"adx_h4": adx_h4,
			"spread_points": float(spread_points or 0.0),
		}
	)

	min_adx = _get_float_env("PRIMARY_MIN_ADX_H4", 20.0)
	min_atr_ratio = _get_float_env("PRIMARY_MIN_ATR_RATIO_PERCENT", 0.25)
	max_spread_points = _get_float_env("PRIMARY_MAX_SPREAD_POINTS", 35.0)

	if adx_h4 < min_adx:
		reasons.append("adx_below_threshold")
	if atr_ratio_percent < min_atr_ratio:
		reasons.append("atr_ratio_below_threshold")
	if spread_points is not None and max_spread_points > 0 and float(spread_points) > max_spread_points:
		reasons.append("spread_above_limit")
	if _is_news_blocked(symbol):
		reasons.append("news_blocked")

	if action == "BUY":
		if ema200_d1 <= ema200_d1_prev:
			reasons.append("d1_ema200_not_rising")
		if ema50_h4 <= ema200_h4:
			reasons.append("h4_trend_not_bullish")
		if close_h1 <= ema20_h1:
			reasons.append("h1_close_below_ema20")
		if not (_get_float_env("PRIMARY_LONG_RSI_MIN", 52.0) <= rsi_h1 <= _get_float_env("PRIMARY_LONG_RSI_MAX", 68.0)):
			reasons.append("rsi_outside_long_band")
	else:
		if ema200_d1 >= ema200_d1_prev:
			reasons.append("d1_ema200_not_falling")
		if ema50_h4 >= ema200_h4:
			reasons.append("h4_trend_not_bearish")
		if close_h1 >= ema20_h1:
			reasons.append("h1_close_above_ema20")
		if not (_get_float_env("PRIMARY_SHORT_RSI_MIN", 32.0) <= rsi_h1 <= _get_float_env("PRIMARY_SHORT_RSI_MAX", 48.0)):
			reasons.append("rsi_outside_short_band")

	regime_state = "trend" if adx_h4 >= min_adx else "range"
	return SignalValidationResult(not reasons, reasons, regime_state, metrics)