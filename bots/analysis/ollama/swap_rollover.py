"""Helpers for broker-derived swap rollover time and blocking window."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import MetaTrader5 as mt5


DEFAULT_ROLLOVER_HOUR = 23
DEFAULT_ROLLOVER_MINUTE = 0
DEFAULT_MANUAL_BLOCK_START_HOUR = 22
DEFAULT_MANUAL_BLOCK_START_MINUTE = 30
DEFAULT_MANUAL_BLOCK_END_HOUR = 23
DEFAULT_MANUAL_BLOCK_END_MINUTE = 30
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_BLOCK_MINUTES = 30
CACHE_TTL_MINUTES = 15

_CACHED_BUCKET_KEY: Optional[str] = None
_CACHED_ROLLOVER_TIME: Optional["SwapRolloverTime"] = None


@dataclass(frozen=True)
class SwapRolloverTime:
	hour: int
	minute: int
	source: str
	sample_size: int
	observed_at_utc: Optional[datetime]


@dataclass(frozen=True)
class SwapBlockWindow:
	rollover_time: SwapRolloverTime
	rollover_at_utc: datetime
	start_utc: datetime
	end_utc: datetime

	def contains(self, now_utc: datetime) -> bool:
		return self.start_utc <= now_utc < self.end_utc


def _normalize_now(now_utc: Optional[datetime]) -> datetime:
	if now_utc is None:
		return datetime.now(tz=timezone.utc)
	if now_utc.tzinfo is None:
		return now_utc.replace(tzinfo=timezone.utc)
	return now_utc.astimezone(timezone.utc)


def _get_env_int(name: str, default: int) -> int:
	raw_value = os.getenv(name)
	if raw_value is None:
		return default
	try:
		return int(raw_value)
	except (TypeError, ValueError):
		return default


def _get_rollover_override() -> Optional[SwapRolloverTime]:
	hour = os.getenv("SWAP_ROLLOVER_HOUR")
	minute = os.getenv("SWAP_ROLLOVER_MINUTE")
	if hour is None and minute is None:
		return None

	try:
		resolved_hour = int(hour) if hour is not None else DEFAULT_ROLLOVER_HOUR
		resolved_minute = int(minute) if minute is not None else DEFAULT_ROLLOVER_MINUTE
	except (TypeError, ValueError):
		return None

	if not (0 <= resolved_hour <= 23 and 0 <= resolved_minute <= 59):
		return None

	return SwapRolloverTime(
		hour=resolved_hour,
		minute=resolved_minute,
		source="env_override",
		sample_size=0,
		observed_at_utc=None,
	)


def _get_manual_block_time_component(name: str, default: int) -> int:
	value = _get_env_int(name, default)
	return max(0, min(59 if "MINUTE" in name else 23, value))


def _get_manual_block_window(now_utc: datetime) -> SwapBlockWindow:
	start_hour = _get_manual_block_time_component("SWAP_BLOCK_START_HOUR", DEFAULT_MANUAL_BLOCK_START_HOUR)
	start_minute = _get_manual_block_time_component("SWAP_BLOCK_START_MINUTE", DEFAULT_MANUAL_BLOCK_START_MINUTE)
	end_hour = _get_manual_block_time_component("SWAP_BLOCK_END_HOUR", DEFAULT_MANUAL_BLOCK_END_HOUR)
	end_minute = _get_manual_block_time_component("SWAP_BLOCK_END_MINUTE", DEFAULT_MANUAL_BLOCK_END_MINUTE)

	base_date = now_utc.date()
	candidate_windows: list[SwapBlockWindow] = []
	for day_offset in (-1, 0, 1):
		candidate_date = base_date + timedelta(days=day_offset)
		start_utc = datetime(
			candidate_date.year,
			candidate_date.month,
			candidate_date.day,
			start_hour,
			start_minute,
			tzinfo=timezone.utc,
		)
		end_utc = datetime(
			candidate_date.year,
			candidate_date.month,
			candidate_date.day,
			end_hour,
			end_minute,
			tzinfo=timezone.utc,
		)
		if end_utc <= start_utc:
			end_utc += timedelta(days=1)

		rollover_at_utc = start_utc + ((end_utc - start_utc) / 2)
		candidate_windows.append(
			SwapBlockWindow(
				rollover_time=SwapRolloverTime(
					hour=rollover_at_utc.hour,
					minute=rollover_at_utc.minute,
					source="env_manual_window",
					sample_size=0,
					observed_at_utc=None,
				),
				rollover_at_utc=rollover_at_utc,
				start_utc=start_utc,
				end_utc=end_utc,
			)
		)

	for window in candidate_windows:
		if window.contains(now_utc):
			return window

	return min(
		candidate_windows,
		key=lambda window: min(
			abs((window.start_utc - now_utc).total_seconds()),
			abs((window.end_utc - now_utc).total_seconds()),
			abs((window.rollover_at_utc - now_utc).total_seconds()),
		),
	)


def _get_deal_timestamp_utc(deal: Any) -> Optional[datetime]:
	time_msc = int(getattr(deal, "time_msc", 0) or 0)
	if time_msc > 0:
		return datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc)

	time_seconds = int(getattr(deal, "time", 0) or 0)
	if time_seconds <= 0:
		return None
	return datetime.fromtimestamp(time_seconds, tz=timezone.utc)


def _is_rollover_deal(deal: Any) -> bool:
	reason = int(getattr(deal, "reason", -1) or -1)
	if reason == getattr(mt5, "DEAL_REASON_ROLLOVER", -999999):
		return True

	comment = str(getattr(deal, "comment", "") or "").strip().lower()
	if "rollover" in comment or "swap" in comment:
		return True

	deal_type = int(getattr(deal, "type", -1) or -1)
	charge_like_types = {
		getattr(mt5, "DEAL_TYPE_CHARGE", -1001),
		getattr(mt5, "DEAL_TYPE_INTEREST", -1002),
		getattr(mt5, "DEAL_TYPE_BALANCE", -1003),
	}
	deal_entry = int(getattr(deal, "entry", -1) or -1)
	closing_entries = {
		getattr(mt5, "DEAL_ENTRY_OUT", -1004),
		getattr(mt5, "DEAL_ENTRY_OUT_BY", -1005),
		getattr(mt5, "DEAL_ENTRY_INOUT", -1006),
	}
	if abs(float(getattr(deal, "swap", 0.0) or 0.0)) <= 0:
		return False
	return deal_type in charge_like_types or deal_entry in closing_entries


def detect_swap_rollover_time_from_deals(deals: Iterable[Any]) -> Optional[SwapRolloverTime]:
	rollover_counts: dict[tuple[int, int], tuple[int, datetime]] = {}

	for deal in deals:
		if not _is_rollover_deal(deal):
			continue

		deal_time_utc = _get_deal_timestamp_utc(deal)
		if deal_time_utc is None:
			continue

		key = (deal_time_utc.hour, deal_time_utc.minute)
		count, latest_seen = rollover_counts.get(key, (0, deal_time_utc))
		rollover_counts[key] = (count + 1, max(latest_seen, deal_time_utc))

	if not rollover_counts:
		return None

	(hour, minute), (sample_size, observed_at_utc) = max(
		rollover_counts.items(),
		key=lambda item: (item[1][0], item[1][1]),
	)
	return SwapRolloverTime(
		hour=hour,
		minute=minute,
		source="mt5_rollover_history",
		sample_size=sample_size,
		observed_at_utc=observed_at_utc,
	)


def detect_swap_rollover_time(
	*,
	now_utc: Optional[datetime] = None,
	lookback_days: Optional[int] = None,
) -> SwapRolloverTime:
	global _CACHED_BUCKET_KEY, _CACHED_ROLLOVER_TIME

	override = _get_rollover_override()
	if override is not None:
		return override

	resolved_now = _normalize_now(now_utc)
	bucket_minutes = max(1, CACHE_TTL_MINUTES)
	bucket_key = (
		f"{resolved_now:%Y%m%d%H}:"
		f"{resolved_now.minute // bucket_minutes}"
	)
	if _CACHED_BUCKET_KEY == bucket_key and _CACHED_ROLLOVER_TIME is not None:
		return _CACHED_ROLLOVER_TIME

	resolved_lookback_days = lookback_days or _get_env_int("SWAP_ROLLOVER_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)
	start_utc = resolved_now - timedelta(days=max(1, resolved_lookback_days))

	rollover_time: Optional[SwapRolloverTime] = None
	deals = mt5.history_deals_get(start_utc, resolved_now)
	if deals is not None:
		rollover_time = detect_swap_rollover_time_from_deals(deals)

	if rollover_time is None:
		rollover_time = SwapRolloverTime(
			hour=DEFAULT_ROLLOVER_HOUR,
			minute=DEFAULT_ROLLOVER_MINUTE,
			source="default_23_00",
			sample_size=0,
			observed_at_utc=None,
		)

	_CACHED_BUCKET_KEY = bucket_key
	_CACHED_ROLLOVER_TIME = rollover_time
	return rollover_time


def get_swap_block_window(
	*,
	now_utc: Optional[datetime] = None,
	block_minutes: Optional[int] = None,
	rollover_time: Optional[SwapRolloverTime] = None,
) -> SwapBlockWindow:
	resolved_now = _normalize_now(now_utc)
	if rollover_time is None:
		return _get_manual_block_window(resolved_now)

	resolved_rollover_time = rollover_time or detect_swap_rollover_time(now_utc=resolved_now)
	resolved_block_minutes = block_minutes or _get_env_int("SWAP_BLOCK_HALF_WINDOW_MINUTES", DEFAULT_BLOCK_MINUTES)

	base_date = resolved_now.date()
	candidate_rollovers = []
	for day_offset in (-1, 0, 1):
		candidate_date = base_date + timedelta(days=day_offset)
		candidate_rollovers.append(
			datetime(
				candidate_date.year,
				candidate_date.month,
				candidate_date.day,
				resolved_rollover_time.hour,
				resolved_rollover_time.minute,
				tzinfo=timezone.utc,
			)
		)

	rollover_at_utc = min(
		candidate_rollovers,
		key=lambda candidate: abs((candidate - resolved_now).total_seconds()),
	)
	half_window = timedelta(minutes=max(1, resolved_block_minutes))
	return SwapBlockWindow(
		rollover_time=resolved_rollover_time,
		rollover_at_utc=rollover_at_utc,
		start_utc=rollover_at_utc - half_window,
		end_utc=rollover_at_utc + half_window,
	)


def is_in_swap_block_window(
	*,
	now_utc: Optional[datetime] = None,
	rollover_time: Optional[SwapRolloverTime] = None,
) -> bool:
	resolved_now = _normalize_now(now_utc)
	window = get_swap_block_window(now_utc=resolved_now, rollover_time=rollover_time)
	return window.contains(resolved_now)