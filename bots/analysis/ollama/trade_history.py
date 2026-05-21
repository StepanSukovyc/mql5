"""Shared trade history helpers."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _parse_timestamp(value: str) -> Optional[datetime]:
	try:
		parsed = datetime.fromisoformat(value)
		if parsed.tzinfo is None:
			return parsed.replace(tzinfo=timezone.utc)
		return parsed.astimezone(timezone.utc)
	except (TypeError, ValueError):
		return None


def count_successful_trades(service_folder: Path, *, strategy_id: str | None = None) -> int:
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
				if strategy_id and str(row.get("strategy_id", "")).strip() not in {"", strategy_id}:
					continue
				raw_success = str(row.get("success", "")).strip().lower()
				if raw_success in {"true", "1", "yes"}:
					success_count += 1
	except Exception as exc:
		print(f"⚠️  Could not read trade history for mode selection: {exc}")
		return 0

	return success_count


def count_successful_trades_today(
	service_folder: Path,
	*,
	strategy_id: str | None = None,
	symbol: str | None = None,
	now_utc: datetime | None = None,
) -> int:
	"""Count today's successful trades, optionally filtered by strategy and symbol."""
	if service_folder is None:
		return 0

	log_file = service_folder / "trade_logs" / "trades.csv"
	if not log_file.exists():
		return 0

	now = now_utc or datetime.now(tz=timezone.utc)
	today = now.date()
	count = 0
	try:
		with open(log_file, "r", encoding="utf-8", newline="") as f:
			reader = csv.DictReader(f)
			for row in reader:
				if strategy_id and str(row.get("strategy_id", "")).strip() not in {"", strategy_id}:
					continue
				if symbol and str(row.get("symbol", "")).strip() != symbol:
					continue
				raw_success = str(row.get("success", "")).strip().lower()
				if raw_success not in {"true", "1", "yes"}:
					continue
				ts = _parse_timestamp(str(row.get("timestamp", "")))
				if ts is None or ts.date() != today:
					continue
				count += 1
	except Exception as exc:
		print(f"⚠️  Could not read trade history for daily limits: {exc}")
		return 0

	return count