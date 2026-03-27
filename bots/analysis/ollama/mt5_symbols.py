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