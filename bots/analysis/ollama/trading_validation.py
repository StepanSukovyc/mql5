"""Shared trading validation helpers."""

from __future__ import annotations

from typing import Tuple

import MetaTrader5 as mt5

from account_state import get_account_info_raw, get_effective_free_margin
from mt5_symbols import get_current_price, get_symbol_info


def validate_symbol(symbol: str) -> Tuple[bool, str]:
	"""Validate symbol availability, visibility, and trade mode."""
	symbol_info = get_symbol_info(symbol)
	if symbol_info is None:
		return False, f"Symbol {symbol} does not exist"

	if not symbol_info.visible:
		print(f"   ⚠️  Symbol {symbol} not in MarketWatch, adding...")
		if not mt5.symbol_select(symbol, True):
			return False, f"Failed to add {symbol} to MarketWatch"
		print(f"   ✅ Symbol {symbol} added to MarketWatch")

	if not symbol_info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL:
		return False, f"Trading not allowed for {symbol}"

	return True, ""


def validate_lot_size(symbol: str, lot_size: float) -> Tuple[float, str]:
	"""Adjust lot size to broker-specific min/max/step constraints."""
	symbol_info = get_symbol_info(symbol)
	if symbol_info is None:
		return 0.0, f"Cannot get symbol info for {symbol}"

	min_lot = symbol_info.volume_min
	max_lot = symbol_info.volume_max
	lot_step = symbol_info.volume_step

	if lot_size < min_lot:
		return min_lot, f"Lot size {lot_size} too small, adjusted to min {min_lot}"

	if lot_size > max_lot:
		return max_lot, f"Lot size {lot_size} too large, adjusted to max {max_lot}"

	adjusted_lot = round(lot_size / lot_step) * lot_step
	adjusted_lot = round(adjusted_lot, 2)

	if adjusted_lot != lot_size:
		return adjusted_lot, f"Lot size adjusted from {lot_size} to {adjusted_lot} (step: {lot_step})"

	return lot_size, ""


def check_margin_requirements(symbol: str, action: str, lot_size: float) -> Tuple[bool, str]:
	"""Validate that free margin is sufficient for the proposed order."""
	try:
		account_info = get_account_info_raw()
	except RuntimeError:
		return False, "Failed to get account info"

	raw_balance = float(account_info.balance)
	raw_free_margin = float(account_info.margin_free)
	free_margin = get_effective_free_margin(raw_balance, raw_free_margin)
	order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL

	symbol_info = get_symbol_info(symbol)
	if symbol_info is None:
		return False, f"Cannot get symbol info for {symbol}"

	price = get_current_price(symbol, action=action)
	if price is None:
		return False, f"Cannot get tick for {symbol}"

	required_margin = mt5.order_calc_margin(order_type, symbol, lot_size, price)
	if required_margin is None:
		return False, f"Cannot calculate margin for {symbol}"

	if required_margin > free_margin:
		if free_margin != raw_free_margin:
			return False, (
				f"Insufficient margin: required {required_margin:.2f}, available {free_margin:.2f} "
				f"(strategy cap applied, raw free margin {raw_free_margin:.2f})"
			)
		return False, f"Insufficient margin: required {required_margin:.2f}, available {free_margin:.2f}"

	if free_margin != raw_free_margin:
		print(
			f"   ✅ Margin check passed: required {required_margin:.2f}, "
			f"available {free_margin:.2f} (strategy cap applied, raw {raw_free_margin:.2f})"
		)
	else:
		print(f"   ✅ Margin check passed: required {required_margin:.2f}, available {free_margin:.2f}")
	return True, ""