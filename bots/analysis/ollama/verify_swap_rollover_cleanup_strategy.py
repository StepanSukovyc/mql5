"""Utility script to verify rollover-window cleanup calculations on sample scenarios."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from swap_rollover_cleanup_strategy import calculate_swap_rollover_cleanup_metrics


@dataclass
class Scenario:
	name: str
	balance: float
	position_volume: float
	profit: float
	swap: float


def _default_scenarios() -> list[Scenario]:
	"""Return sample scenarios for the rollover-window cleanup strategy."""
	return [
		Scenario("below_threshold_after_fee", 3261.0, 0.06, 0.70, -0.05),
		Scenario("eligible_small_profit", 3261.0, 0.01, 0.25, 0.00),
		Scenario("exact_threshold", 10.0, 0.01, 0.20, 0.00),
		Scenario("negative_net_profit", 1500.0, 0.03, 0.20, -0.40),
	]


def _parse_scenarios(argv: list[str]) -> list[Scenario]:
	"""Parse CLI args as repeated balance/volume/profit/swap quartets."""
	if not argv:
		return _default_scenarios()

	if len(argv) % 4 != 0:
		raise ValueError(
			"Provide balance/volume/profit/swap quartets, e.g. 3261 0.06 0.70 -0.05 1000 0.01 0.25 0"
		)

	scenarios: list[Scenario] = []
	for index in range(0, len(argv), 4):
		scenarios.append(
			Scenario(
				name=f"cli_scenario_{index // 4 + 1}",
				balance=float(argv[index]),
				position_volume=float(argv[index + 1]),
				profit=float(argv[index + 2]),
				swap=float(argv[index + 3]),
			)
		)
	return scenarios


def main(argv: list[str]) -> int:
	"""Print derived rollover cleanup values for each scenario."""
	try:
		scenarios = _parse_scenarios(argv)
	except ValueError as exc:
		print(f"❌ {exc}")
		return 1

	for scenario in scenarios:
		metrics = calculate_swap_rollover_cleanup_metrics(
			balance=scenario.balance,
			position_volume=scenario.position_volume,
			profit=scenario.profit,
			swap=scenario.swap,
		)

		print(f"Scenario: {scenario.name}")
		print(f"  B: {metrics.balance:.2f}")
		print(f"  L: {metrics.position_volume:.2f}")
		print(f"  profit: {metrics.profit:.2f}")
		print(f"  swap: {metrics.swap:.2f}")
		print(f"  fee: {metrics.fee:.2f}")
		print(f"  ZISK: {metrics.net_profit:.2f}")
		print(f"  threshold: {metrics.target_profit:.2f}")
		print(f"  eligible: {metrics.eligible}")
		print("-" * 40)

	return 0


if __name__ == "__main__":
	raise SystemExit(main(sys.argv[1:]))