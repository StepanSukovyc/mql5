"""Shared trade risk helpers."""

from __future__ import annotations

import math

from account_state import get_effective_balance


def calculate_lot_size(balance: float) -> float:
	"""Calculate standard-mode lot size from current balance."""
	effective_balance = get_effective_balance(balance)
	numerator = effective_balance + 500
	quotient = numerator / 500
	whole_number = math.floor(quotient)
	lot_size = whole_number / 100

	print(f"\n💰 Lot Size Calculation:")
	print(f"   Balance for strategy: {effective_balance:.2f}")
	if effective_balance != balance:
		print(f"   Raw balance: {balance:.2f}")
	print(f"   ({effective_balance:.2f} + 500) / 500 = {quotient:.3f}")
	print(f"   Floor: {whole_number}")
	print(f"   Lot size: {lot_size:.2f}")

	return lot_size
