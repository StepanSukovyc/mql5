"""Shared MetaTrader 5 symbol helpers."""

from __future__ import annotations

from typing import Any, Optional

import MetaTrader5 as mt5


def get_symbol_info(symbol: str) -> Any:
	"""Return MT5 symbol metadata or None when the symbol is unavailable."""
	return mt5.symbol_info(symbol)


def get_symbol_tick(symbol: str) -> Any:
	"""Return the latest MT5 tick for a symbol or None when unavailable."""
	return mt5.symbol_info_tick(symbol)


def get_current_price(
	symbol: str,
	*,
	action: Optional[str] = None,
	default: Optional[float] = None,
) -> Optional[float]:
	"""Return the current bid/ask price for a symbol with optional fallback."""
	tick = get_symbol_tick(symbol)
	if tick is None:
		return default

	if action == "BUY":
		return float(tick.ask)

	return float(tick.bid)


def estimate_order_profit(
	symbol: str,
	action: str,
	volume: float,
	open_price: float,
	close_price: float,
) -> Optional[float]:
	"""Estimate MT5 order profit for a hypothetical exit price."""
	if action == "BUY":
		order_type = mt5.ORDER_TYPE_BUY
	elif action == "SELL":
		order_type = mt5.ORDER_TYPE_SELL
	else:
		return None

	profit = mt5.order_calc_profit(order_type, symbol, volume, open_price, close_price)
	if profit is None:
		return None

	return float(profit)