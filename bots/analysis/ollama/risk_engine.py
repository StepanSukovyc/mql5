from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional

from instrument_utils import get_crypto_lot_multiplier, is_crypto_symbol
from mt5_symbols import estimate_order_profit, get_current_price, get_symbol_info


@dataclass(frozen=True)
class SyntheticRiskPlan:
	entry_price: float
	risk_usd: float
	lot_size: float
	synthetic_stop_distance: float
	synthetic_stop_price: float
	take_profit_distance: float
	take_profit_price: float


def _get_float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		value = float(raw)
		if value < minimum:
			raise ValueError
		return value
	except (TypeError, ValueError):
		return default


def _get_latest_indicator_value(market_data: Dict, timeframe: str, indicator: str) -> Optional[float]:
	series = market_data.get("oscillators", {}).get(timeframe, {}).get(indicator, [])
	if not series:
		return None
	try:
		return float(series[-1]["value"])
	except (KeyError, TypeError, ValueError):
		return None


def _round_price(symbol: str, price: float) -> float:
	symbol_info = get_symbol_info(symbol)
	digits = getattr(symbol_info, "digits", None) if symbol_info is not None else None
	if isinstance(digits, int) and digits >= 0:
		return round(price, digits)
	return price


def calculate_synthetic_risk_plan(
	*,
	symbol: str,
	action: str,
	account_state: Dict,
	market_data: Dict,
	risk_percent_env: str = "PRIMARY_RISK_PER_TRADE_PERCENT",
	stop_atr_multiplier_env: str = "PRIMARY_SYNTHETIC_STOP_ATR_MULTIPLIER",
	tp_r_multiple_env: str = "PRIMARY_TAKE_PROFIT_R_MULTIPLIER",
) -> Optional[SyntheticRiskPlan]:
	entry_price = get_current_price(symbol, action=action)
	if entry_price is None:
		return None

	atr_value = _get_latest_indicator_value(market_data, "1h", "atr14")
	if atr_value is None or atr_value <= 0:
		return None

	balance = float(account_state.get("balance", 0.0) or 0.0)
	if balance <= 0:
		return None

	risk_percent = _get_float_env(risk_percent_env, 0.50, minimum=0.01)
	stop_atr_multiplier = _get_float_env(stop_atr_multiplier_env, 1.5, minimum=0.01)
	tp_r_multiple = _get_float_env(tp_r_multiple_env, 2.2, minimum=0.1)
	risk_usd = balance * (risk_percent / 100.0)
	synthetic_stop_distance = atr_value * stop_atr_multiplier
	if synthetic_stop_distance <= 0:
		return None

	if action == "BUY":
		synthetic_stop_price = entry_price - synthetic_stop_distance
		take_profit_price = entry_price + (synthetic_stop_distance * tp_r_multiple)
	else:
		synthetic_stop_price = entry_price + synthetic_stop_distance
		take_profit_price = entry_price - (synthetic_stop_distance * tp_r_multiple)

	per_lot_loss = estimate_order_profit(symbol, action, 1.0, entry_price, synthetic_stop_price)
	if per_lot_loss is None or per_lot_loss == 0:
		return None

	lot_size = risk_usd / abs(float(per_lot_loss))
	if is_crypto_symbol(symbol):
		lot_size *= get_crypto_lot_multiplier()

	return SyntheticRiskPlan(
		entry_price=float(entry_price),
		risk_usd=round(risk_usd, 2),
		lot_size=float(lot_size),
		synthetic_stop_distance=float(synthetic_stop_distance),
		synthetic_stop_price=_round_price(symbol, synthetic_stop_price),
		take_profit_distance=float(synthetic_stop_distance * tp_r_multiple),
		take_profit_price=_round_price(symbol, take_profit_price),
	)