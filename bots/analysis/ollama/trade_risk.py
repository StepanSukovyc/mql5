"""Shared trade risk helpers."""

from __future__ import annotations

import math


def calculate_lot_size(balance: float) -> float:
	"""Calculate standard-mode lot size from current balance."""
	numerator = balance + 500
	quotient = numerator / 500
	whole_number = math.floor(quotient)
	lot_size = whole_number / 100

	print(f"\n💰 Lot Size Calculation:")
	print(f"   Balance: {balance:.2f}")
	print(f"   ({balance:.2f} + 500) / 500 = {quotient:.3f}")
	print(f"   Floor: {whole_number}")
	print(f"   Lot size: {lot_size:.2f}")

	return lot_size
