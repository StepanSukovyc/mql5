"""Utility script to verify strategy balance-cap calculations from .env."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from account_state import (
	get_account_balance_cap,
	get_balance_reserve,
	get_effective_balance,
	get_effective_free_margin,
)


def _load_dotenv(dotenv_path: Path) -> None:
	"""Load .env values into process env if keys are not already set."""
	if not dotenv_path.exists():
		return

	for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, value = line.split("=", 1)
		key = key.strip()
		value = value.strip().strip('"').strip("'")
		if key and key not in os.environ:
			os.environ[key] = value


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
	"""Print effective balance and free margin for configured cap scenarios."""
	base_dir = Path(__file__).resolve().parent
	_load_dotenv(base_dir / ".env")
	_load_dotenv(base_dir.parent / ".env")
	_load_dotenv(Path.cwd() / ".env")

	try:
		scenarios = _parse_scenarios(argv)
	except ValueError as exc:
		print(f"❌ {exc}")
		return 1

	cap = get_account_balance_cap()
	print(f"Strategy balance cap from env: {cap:.2f}")
	print()

	for balance, free_margin in scenarios:
		reserve = get_balance_reserve(balance, cap=cap)
		effective_balance = get_effective_balance(balance, cap=cap)
		effective_free_margin = get_effective_free_margin(balance, free_margin, cap=cap)

		print(f"Raw balance: {balance:.2f}")
		print(f"Raw free margin: {free_margin:.2f}")
		print(f"Reserve above cap: {reserve:.2f}")
		print(f"Effective balance: {effective_balance:.2f}")
		print(f"Effective free margin: {effective_free_margin:.2f}")
		print("-" * 40)

	return 0


if __name__ == "__main__":
	raise SystemExit(main(sys.argv[1:]))