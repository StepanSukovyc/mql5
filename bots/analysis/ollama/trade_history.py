"""Shared trade history helpers."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


def _parse_trade_timestamp(raw_value: str) -> Optional[datetime]:
	if not raw_value:
		return None
	try:
		parsed = datetime.fromisoformat(raw_value)
		if parsed.tzinfo is None:
			return parsed.replace(tzinfo=timezone.utc)
		return parsed.astimezone(timezone.utc)
	except ValueError:
		return None


def _iter_trade_rows(service_folder: Path):
	if service_folder is None:
		return []

	log_file = service_folder / "trade_logs" / "trades.csv"
	if not log_file.exists():
		return []

	try:
		with open(log_file, "r", encoding="utf-8", newline="") as f:
			return list(csv.DictReader(f))
	except Exception as exc:
		print(f"⚠️  Could not read trade history: {exc}")
		return []


def count_successful_trades(service_folder: Path) -> int:
	"""Count successful trades from the service trade log CSV."""
	if service_folder is None:
		return 0

	success_count = 0
	for row in _iter_trade_rows(service_folder):
		raw_success = str(row.get("success", "")).strip().lower()
		if raw_success in {"true", "1", "yes"}:
			success_count += 1

	return success_count


def count_successful_trades_today(
	service_folder: Path,
	*,
	strategy_id: Optional[str] = None,
	now_utc: Optional[datetime] = None,
) -> int:
	"""Count successful trades since the start of the current UTC day."""
	now = now_utc or datetime.now(tz=timezone.utc)
	day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
	return count_successful_trades_since(
		service_folder,
		strategy_id=strategy_id,
		lookback=now - day_start,
		now_utc=now,
	)


def count_successful_trades_since(
	service_folder: Path,
	*,
	strategy_id: Optional[str] = None,
	symbol: Optional[str] = None,
	lookback: Optional[timedelta] = None,
	now_utc: Optional[datetime] = None,
) -> int:
	"""Count successful trades in the requested lookback window with optional filters."""
	if service_folder is None:
		return 0

	now = now_utc or datetime.now(tz=timezone.utc)
	window_start = now - (lookback or timedelta(0))
	count = 0
	for row in _iter_trade_rows(service_folder):
		raw_success = str(row.get("success", "")).strip().lower()
		if raw_success not in {"true", "1", "yes"}:
			continue
		if strategy_id and str(row.get("strategy_id", "")).strip() != strategy_id:
			continue
		if symbol and str(row.get("symbol", "")).strip() != symbol:
			continue
		trade_time = _parse_trade_timestamp(str(row.get("timestamp", "")))
		if trade_time is None or trade_time < window_start:
			continue
		count += 1

	return count