"""Utility script to verify dynamic strategy balance-cap calculations."""

from __future__ import annotations

import sys
from pathlib import Path

from account_state import (
	get_account_balance_cap,
	get_balance_reserve,
	get_effective_balance,
	get_effective_free_margin,
)


def _default_scenarios() -> list[tuple[float, float]]:
	"""Return sample scenarios covering below-cap and above-cap cases."""
	return [
		(4200.0, 3900.0),
		(5000.0, 5000.0),
		(6200.0, 6100.0),
		(6200.0, 800.0),
	]


def _parse_scenarios(argv: list[str]) -> list[tuple[float, float]]:
	"""Parse CLI args as repeated balance/free_margin pairs."""
	if not argv:
		return _default_scenarios()

	if len(argv) % 2 != 0:
		raise ValueError("Provide balance/free_margin pairs, e.g. 6200 6100 6200 800")

	scenarios: list[tuple[float, float]] = []
	for index in range(0, len(argv), 2):
		balance = float(argv[index])
		free_margin = float(argv[index + 1])
		scenarios.append((balance, free_margin))
	return scenarios


def main(argv: list[str]) -> int:
	"""Print effective balance and free margin for dynamic cap scenarios."""

	try:
		scenarios = _parse_scenarios(argv)
	except ValueError as exc:
		print(f"❌ {exc}")
		return 1

	for balance, free_margin in scenarios:
		cap = get_account_balance_cap(balance)
		reserve = get_balance_reserve(balance, cap=cap)
		effective_balance = get_effective_balance(balance, cap=cap)
		effective_free_margin = get_effective_free_margin(balance, free_margin, cap=cap)

		print(f"Raw balance: {balance:.2f}")
		print(f"Strategy balance cap: {cap:.2f}")
		print(f"Raw free margin: {free_margin:.2f}")
		print(f"Reserve above cap: {reserve:.2f}")
		print(f"Effective balance: {effective_balance:.2f}")
		print(f"Effective free margin: {effective_free_margin:.2f}")
		print("-" * 40)

	return 0


if __name__ == "__main__":
	raise SystemExit(main(sys.argv[1:]))