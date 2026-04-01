"""Utility script to verify minute profit-cleanup calculations on sample scenarios."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from profit_cleanup_strategy import calculate_profit_cleanup_metrics


@dataclass
class Scenario:
	name: str
	balance: float
	position_volume: float
	profit: float
	swap: float


def _default_scenarios() -> list[Scenario]:
	"""Return sample scenarios including the user's example and edge cases."""
	return [
		Scenario("user_example_not_eligible", 3261.0, 0.06, 6.34, -0.30),
		Scenario("eligible_medium_position", 3261.0, 0.01, 60.00, 0.00),
		Scenario("minimum_target_profit_floor", 10.0, 0.01, 0.30, -0.10),
		Scenario("negative_net_profit", 1500.0, 0.03, 0.80, -0.40),
	]


def _parse_scenarios(argv: list[str]) -> list[Scenario]:
	"""Parse CLI args as repeated balance/volume/profit/swap quartets."""
	if not argv:
		return _default_scenarios()

	if len(argv) % 4 != 0:
		raise ValueError(
			"Provide balance/volume/profit/swap quartets, e.g. 3261 0.06 6.34 -0.30 1000 0.01 15 0"
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
	"""Print derived profit-cleanup values for each scenario."""
	try:
		scenarios = _parse_scenarios(argv)
	except ValueError as exc:
		print(f"❌ {exc}")
		return 1

	for scenario in scenarios:
		metrics = calculate_profit_cleanup_metrics(
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
		print(f"  VOLUME: {metrics.reference_volume:.2f}")
		print(f"  fee: {metrics.fee:.2f}")
		print(f"  ZISK: {metrics.net_profit:.2f}")
		print(f"  PCZ: {metrics.target_profit:.4f}")
		print(f"  eligible: {metrics.eligible}")
		print("-" * 40)

	return 0


if __name__ == "__main__":
	raise SystemExit(main(sys.argv[1:]))