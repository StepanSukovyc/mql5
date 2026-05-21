"""Strategy profile definitions and runtime guardrails."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from strategy_context import (
	DEFAULT_INDEX_MAGIC,
	DEFAULT_INDEX_STRATEGY_ID,
	DEFAULT_PRIMARY_MAGIC,
	DEFAULT_PRIMARY_STRATEGY_ID,
)


def _parse_csv(value: Optional[str]) -> List[str]:
	if not value:
		return []
	return [item.strip() for item in value.split(",") if item.strip()]


def _parse_int(name: str, default: int, *, minimum: int) -> int:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		value = int(raw)
		if value < minimum:
			raise ValueError
		return value
	except (TypeError, ValueError):
		return default


def _parse_float(name: str, default: float, *, minimum: float) -> float:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		value = float(raw)
		if value < minimum:
			raise ValueError
		return value
	except (TypeError, ValueError):
		return default


def _parse_bool(name: str, default: bool) -> bool:
	raw = os.getenv(name)
	if raw is None:
		return default
	return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class StrategyProfile:
	strategy_id: str
	magic: int
	label: str
	allowed_symbols: tuple[str, ...]
	service_subdir: str
	trading_session_start_hour_utc: int
	trading_session_end_hour_utc: int
	friday_cutoff_hour_utc: int
	max_open_positions: int
	max_trades_per_day: int
	max_trades_per_symbol_per_day: int
	max_spread_points: float
	balance_step_usd: float
	lot_per_balance_step: float
	max_lot_cap: float

	def allows_symbol(self, symbol: str) -> bool:
		if not self.allowed_symbols:
			return True
		return symbol in self.allowed_symbols


def is_strategy_session_open(profile: StrategyProfile, *, now_utc: datetime | None = None) -> bool:
	now = now_utc or datetime.now(tz=timezone.utc)
	if now.weekday() >= 5:
		return False
	if now.weekday() == 4 and now.hour >= profile.friday_cutoff_hour_utc:
		return False
	if profile.trading_session_start_hour_utc <= profile.trading_session_end_hour_utc:
		return profile.trading_session_start_hour_utc <= now.hour < profile.trading_session_end_hour_utc
	return now.hour >= profile.trading_session_start_hour_utc or now.hour < profile.trading_session_end_hour_utc


def get_primary_strategy_profile() -> StrategyProfile:
	return StrategyProfile(
		strategy_id=os.getenv("PRIMARY_STRATEGY_ID", DEFAULT_PRIMARY_STRATEGY_ID),
		magic=_parse_int("PRIMARY_STRATEGY_MAGIC", DEFAULT_PRIMARY_MAGIC, minimum=1),
		label="Primary AI FX strategy",
		allowed_symbols=tuple(_parse_csv(os.getenv("PRIMARY_STRATEGY_SYMBOL_WHITELIST"))),
		service_subdir="",
		trading_session_start_hour_utc=_parse_int("PRIMARY_SESSION_START_HOUR_UTC", 6, minimum=0),
		trading_session_end_hour_utc=_parse_int("PRIMARY_SESSION_END_HOUR_UTC", 20, minimum=0),
		friday_cutoff_hour_utc=_parse_int("PRIMARY_FRIDAY_CUTOFF_HOUR_UTC", 16, minimum=0),
		max_open_positions=_parse_int("PRIMARY_MAX_OPEN_POSITIONS", 4, minimum=0),
		max_trades_per_day=_parse_int("PRIMARY_MAX_TRADES_PER_DAY", 6, minimum=0),
		max_trades_per_symbol_per_day=_parse_int("PRIMARY_MAX_TRADES_PER_SYMBOL_PER_DAY", 2, minimum=0),
		max_spread_points=_parse_float("PRIMARY_MAX_SPREAD_POINTS", 35.0, minimum=0.0),
		balance_step_usd=_parse_float("PRIMARY_BALANCE_STEP_USD", 1000.0, minimum=1.0),
		lot_per_balance_step=_parse_float("PRIMARY_LOT_PER_BALANCE_STEP", 0.01, minimum=0.0),
		max_lot_cap=_parse_float("PRIMARY_MAX_LOT_CAP", 0.10, minimum=0.01),
	)


def get_index_strategy_profile() -> StrategyProfile:
	default_symbols = (
		"US100_ecn",
		"US500_ecn",
		"US30_ecn",
		"GER40_ecn",
		"FRA40_ecn",
		"UK100_ecn",
		"JP225_ecn",
		"EU50_ecn",
		"HKD50_ecn",
		"FANG_ecn",
	)
	configured_symbols = tuple(_parse_csv(os.getenv("INDEX_STRATEGY_SYMBOL_WHITELIST"))) or default_symbols
	return StrategyProfile(
		strategy_id=os.getenv("INDEX_STRATEGY_ID", DEFAULT_INDEX_STRATEGY_ID),
		magic=_parse_int("INDEX_STRATEGY_MAGIC", DEFAULT_INDEX_MAGIC, minimum=1),
		label="Parallel AI index strategy",
		allowed_symbols=configured_symbols,
		service_subdir=os.getenv("INDEX_STRATEGY_SERVICE_SUBDIR", "indices_strategy").strip() or "indices_strategy",
		trading_session_start_hour_utc=_parse_int("INDEX_SESSION_START_HOUR_UTC", 7, minimum=0),
		trading_session_end_hour_utc=_parse_int("INDEX_SESSION_END_HOUR_UTC", 19, minimum=0),
		friday_cutoff_hour_utc=_parse_int("INDEX_FRIDAY_CUTOFF_HOUR_UTC", 15, minimum=0),
		max_open_positions=_parse_int("INDEX_MAX_OPEN_POSITIONS", 2, minimum=0),
		max_trades_per_day=_parse_int("INDEX_MAX_TRADES_PER_DAY", 3, minimum=0),
		max_trades_per_symbol_per_day=_parse_int("INDEX_MAX_TRADES_PER_SYMBOL_PER_DAY", 1, minimum=0),
		max_spread_points=_parse_float("INDEX_MAX_SPREAD_POINTS", 60.0, minimum=0.0),
		balance_step_usd=_parse_float("INDEX_BALANCE_STEP_USD", 1500.0, minimum=1.0),
		lot_per_balance_step=_parse_float("INDEX_LOT_PER_BALANCE_STEP", 0.01, minimum=0.0),
		max_lot_cap=_parse_float("INDEX_MAX_LOT_CAP", 0.10, minimum=0.01),
	)


def is_index_strategy_enabled() -> bool:
	return _parse_bool("INDEX_STRATEGY_ENABLED", True)


def list_allowed_symbols(profile: StrategyProfile) -> Iterable[str]:
	return profile.allowed_symbols


def get_active_strategy_profiles() -> List[StrategyProfile]:
	"""Return currently enabled strategy profiles."""
	profiles = [get_primary_strategy_profile()]
	if is_index_strategy_enabled():
		profiles.append(get_index_strategy_profile())
	return profiles