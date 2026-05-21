"""Market-regime classification and entry guardrails used before AI trade selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class MarketRegimeContext:
	regime: str
	strong_trend: bool
	entry_allowed: bool
	buy_setup: bool
	sell_setup: bool
	spread_ok: bool
	spread_points: Optional[float]
	atr_h1_ratio: Optional[float]
	atr_h1_value: Optional[float]
	atr_h4_ratio: Optional[float]
	bb_bandwidth_h1: Optional[float]
	rsi_h4: Optional[float]
	ma_slope_h4_ratio: Optional[float]
	d1_above_ma: Optional[bool]
	h1_above_ma: Optional[bool]
	reason: str

	def to_dict(self) -> Dict[str, object]:
		return {
			"regime": self.regime,
			"strong_trend": self.strong_trend,
			"entry_allowed": self.entry_allowed,
			"buy_setup": self.buy_setup,
			"sell_setup": self.sell_setup,
			"spread_ok": self.spread_ok,
			"spread_points": self.spread_points,
			"atr_h1_ratio": self.atr_h1_ratio,
			"atr_h1_value": self.atr_h1_value,
			"atr_h4_ratio": self.atr_h4_ratio,
			"bb_bandwidth_h1": self.bb_bandwidth_h1,
			"rsi_h4": self.rsi_h4,
			"ma_slope_h4_ratio": self.ma_slope_h4_ratio,
			"d1_above_ma": self.d1_above_ma,
			"h1_above_ma": self.h1_above_ma,
			"reason": self.reason,
		}


def _latest_indicator_value(data: Dict, timeframe: str, indicator: str, key: str = "value") -> Optional[float]:
	series = data.get("oscillators", {}).get(timeframe, {}).get(indicator, [])
	if not series:
		return None
	latest = series[-1]
	value = latest.get(key)
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _latest_close(data: Dict, timeframe: str) -> Optional[float]:
	candles = data.get("candles", {}).get(timeframe, [])
	if not candles:
		return None
	try:
		return float(candles[-1].get("close"))
	except (TypeError, ValueError):
		return None


def _previous_ma(data: Dict, timeframe: str, offset: int) -> Optional[float]:
	series = data.get("oscillators", {}).get(timeframe, {}).get("ma", [])
	if len(series) <= offset:
		return None
	try:
		return float(series[-1 - offset].get("value"))
	except (TypeError, ValueError):
		return None


def classify_market_regime(data: Dict, *, max_spread_points: float) -> MarketRegimeContext:
	close_h1 = _latest_close(data, "1h")
	close_h4 = _latest_close(data, "4h")
	close_d1 = _latest_close(data, "day")
	ma_h1 = _latest_indicator_value(data, "1h", "ma")
	ma_h4 = _latest_indicator_value(data, "4h", "ma")
	ma_d1 = _latest_indicator_value(data, "day", "ma")
	prev_ma_h4 = _previous_ma(data, "4h", 1)
	old_ma_h4 = _previous_ma(data, "4h", 4)
	rsi_h4 = _latest_indicator_value(data, "4h", "rsi")
	atr_h1 = _latest_indicator_value(data, "1h", "atr")
	atr_h4 = _latest_indicator_value(data, "4h", "atr")
	bb_bandwidth_h1 = _latest_indicator_value(data, "1h", "bollinger", key="bandwidth")
	spread_points = data.get("current_spread_points")
	try:
		spread_points = float(spread_points) if spread_points is not None else None
	except (TypeError, ValueError):
		spread_points = None

	if not all(value is not None for value in (close_h1, close_h4, close_d1, ma_h1, ma_h4, ma_d1, prev_ma_h4, old_ma_h4, rsi_h4, atr_h1, atr_h4, bb_bandwidth_h1)):
		return MarketRegimeContext(
			regime="unknown",
			strong_trend=False,
			entry_allowed=False,
			buy_setup=False,
			sell_setup=False,
			spread_ok=False,
			spread_points=spread_points,
			atr_h1_ratio=None,
			atr_h1_value=atr_h1,
			atr_h4_ratio=None,
			bb_bandwidth_h1=bb_bandwidth_h1,
			rsi_h4=rsi_h4,
			ma_slope_h4_ratio=None,
			d1_above_ma=None,
			h1_above_ma=None,
			reason="insufficient_data",
		)

	atr_h1_ratio = float(atr_h1) / float(close_h1) if close_h1 else None
	atr_h4_ratio = float(atr_h4) / float(close_h4) if close_h4 else None
	ma_slope_h4_ratio = abs(float(ma_h4) - float(old_ma_h4)) / float(close_h4) if close_h4 else None
	spread_ok = spread_points is None or spread_points <= max_spread_points
	d1_above_ma = float(close_d1) > float(ma_d1)
	h1_above_ma = float(close_h1) > float(ma_h1)

	regime = "neutral"
	strong_trend = False
	reason = "neutral_conditions"

	if atr_h1_ratio is not None and atr_h1_ratio > 0.008:
		regime = "disorder"
		reason = "atr_h1_ratio_too_high"
	elif ma_slope_h4_ratio is not None and atr_h4_ratio is not None and ma_slope_h4_ratio >= 0.004 and atr_h4_ratio >= 0.0025:
		regime = "trend"
		strong_trend = ma_slope_h4_ratio >= 0.006 and atr_h4_ratio >= 0.0035
		reason = "trend_confirmed"
	elif ma_slope_h4_ratio is not None and bb_bandwidth_h1 is not None and ma_slope_h4_ratio < 0.002 and bb_bandwidth_h1 < 0.02:
		regime = "range"
		reason = "range_detected"

	buy_setup = bool(
		regime == "trend"
		and d1_above_ma
		and float(ma_h4) > float(prev_ma_h4)
		and 52.0 <= float(rsi_h4) <= 68.0
		and h1_above_ma
		and atr_h1_ratio is not None
		and atr_h1_ratio >= 0.0035
	)
	sell_setup = bool(
		regime == "trend"
		and not d1_above_ma
		and float(ma_h4) < float(prev_ma_h4)
		and 32.0 <= float(rsi_h4) <= 48.0
		and not h1_above_ma
		and atr_h1_ratio is not None
		and atr_h1_ratio >= 0.0035
	)
	entry_allowed = spread_ok and (buy_setup or sell_setup)
	if not spread_ok:
		reason = "spread_too_wide"
	elif not entry_allowed and regime == "trend":
		reason = "trend_without_confirmed_setup"

	return MarketRegimeContext(
		regime=regime,
		strong_trend=strong_trend,
		entry_allowed=entry_allowed,
		buy_setup=buy_setup,
		sell_setup=sell_setup,
		spread_ok=spread_ok,
		spread_points=spread_points,
		atr_h1_ratio=round(atr_h1_ratio, 6) if atr_h1_ratio is not None else None,
		atr_h1_value=round(float(atr_h1), 6) if atr_h1 is not None else None,
		atr_h4_ratio=round(atr_h4_ratio, 6) if atr_h4_ratio is not None else None,
		bb_bandwidth_h1=round(float(bb_bandwidth_h1), 6) if bb_bandwidth_h1 is not None else None,
		rsi_h4=round(float(rsi_h4), 4) if rsi_h4 is not None else None,
		ma_slope_h4_ratio=round(ma_slope_h4_ratio, 6) if ma_slope_h4_ratio is not None else None,
		d1_above_ma=d1_above_ma,
		h1_above_ma=h1_above_ma,
		reason=reason,
	)