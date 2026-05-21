"""Shared trade history helpers."""

from __future__ import annotations

import csv
from pathlib import Path


def count_successful_trades(service_folder: Path) -> int:
	"""Count successful trades from the service trade log CSV."""
	if service_folder is None:
		return 0

	log_file = service_folder / "trade_logs" / "trades.csv"
	if not log_file.exists():
		return 0

	success_count = 0
	try:
		with open(log_file, "r", encoding="utf-8", newline="") as f:
			reader = csv.DictReader(f)
			for row in reader:
				raw_success = str(row.get("success", "")).strip().lower()
				if raw_success in {"true", "1", "yes"}:
					success_count += 1
	except Exception as exc:
		print(f"⚠️  Could not read trade history for mode selection: {exc}")
		return 0

	return success_count