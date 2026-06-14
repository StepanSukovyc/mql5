from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional


@dataclass(frozen=True)
class StrategyContext:
	strategy_id: str
	magic: int
	manage_legacy_positions: bool
	activation_margin_percent: float
	max_open_positions: int
	session_start_hour_utc: int
	session_end_hour_utc: int
	friday_cutoff_hour_utc: int


def _get_env_int(name: str, default: int) -> int:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		return int(raw)
	except (TypeError, ValueError):
		return default


def _get_env_float(name: str, default: float) -> float:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		return float(raw)
	except (TypeError, ValueError):
		return default


def _get_env_bool(name: str, default: bool) -> bool:
	raw = os.getenv(name)
	if raw is None:
		return default
	return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_primary_strategy_context() -> StrategyContext:
	default_activation = _get_env_float("TRADING_MARGIN_THRESHOLD", 20.0)
	manage_legacy_positions = _get_env_bool(
		"PRIMARY_STRATEGY_MANAGE_MANUAL_POSITIONS",
		_get_env_bool("PRIMARY_MANAGE_LEGACY_POSITIONS", True),
	)
	return StrategyContext(
		strategy_id=os.getenv("PRIMARY_STRATEGY_ID", "gemini_primary"),
		magic=_get_env_int("PRIMARY_STRATEGY_MAGIC", 234000),
		manage_legacy_positions=manage_legacy_positions,
		activation_margin_percent=_get_env_float("PRIMARY_STRATEGY_ACTIVATION_MARGIN_PERCENT", default_activation),
		max_open_positions=_get_env_int("PRIMARY_MAX_OPEN_POSITIONS", 0),
		session_start_hour_utc=_get_env_int("PRIMARY_SESSION_START_HOUR_UTC", 0),
		session_end_hour_utc=_get_env_int("PRIMARY_SESSION_END_HOUR_UTC", 24),
		friday_cutoff_hour_utc=_get_env_int("PRIMARY_FRIDAY_CUTOFF_HOUR_UTC", 24),
	)


def get_parallel_strategy_context() -> StrategyContext:
	primary_activation = get_primary_strategy_context().activation_margin_percent
	delta = _get_env_float("PARALLEL_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT", 5.0)
	return StrategyContext(
		strategy_id=os.getenv("PARALLEL_STRATEGY_ID", "parallel_mean_reversion"),
		magic=_get_env_int("PARALLEL_STRATEGY_MAGIC", 234200),
		manage_legacy_positions=_get_env_bool("PARALLEL_MANAGE_LEGACY_POSITIONS", False),
		activation_margin_percent=max(primary_activation - delta, 0.0),
		max_open_positions=_get_env_int("PARALLEL_STRATEGY_MAX_OPEN_POSITIONS", 7),
		session_start_hour_utc=_get_env_int(
			"PARALLEL_SESSION_START_HOUR_UTC",
			get_primary_strategy_context().session_start_hour_utc,
		),
		session_end_hour_utc=_get_env_int(
			"PARALLEL_SESSION_END_HOUR_UTC",
			get_primary_strategy_context().session_end_hour_utc,
		),
		friday_cutoff_hour_utc=_get_env_int(
			"PARALLEL_FRIDAY_CUTOFF_HOUR_UTC",
			get_primary_strategy_context().friday_cutoff_hour_utc,
		),
	)


def get_reversal_strategy_context() -> StrategyContext:
	primary_activation = get_primary_strategy_context().activation_margin_percent
	delta = _get_env_float("REVERSAL_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT", 5.0)
	return StrategyContext(
		strategy_id=os.getenv("REVERSAL_STRATEGY_ID", "reversal_pattern"),
		magic=_get_env_int("REVERSAL_STRATEGY_MAGIC", 234300),
		manage_legacy_positions=_get_env_bool("REVERSAL_MANAGE_LEGACY_POSITIONS", False),
		activation_margin_percent=max(primary_activation - delta, 0.0),
		max_open_positions=_get_env_int("REVERSAL_STRATEGY_MAX_OPEN_POSITIONS", 3),
		session_start_hour_utc=_get_env_int(
			"REVERSAL_SESSION_START_HOUR_UTC",
			get_parallel_strategy_context().session_start_hour_utc,
		),
		session_end_hour_utc=_get_env_int(
			"REVERSAL_SESSION_END_HOUR_UTC",
			get_parallel_strategy_context().session_end_hour_utc,
		),
		friday_cutoff_hour_utc=_get_env_int(
			"REVERSAL_FRIDAY_CUTOFF_HOUR_UTC",
			get_parallel_strategy_context().friday_cutoff_hour_utc,
		),
	)


def get_quant_strategy_context() -> StrategyContext:
	primary_activation = get_primary_strategy_context().activation_margin_percent
	delta = _get_env_float("QUANT_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT", 8.0)
	return StrategyContext(
		strategy_id=os.getenv("QUANT_STRATEGY_ID", "quant_math"),
		magic=_get_env_int("QUANT_STRATEGY_MAGIC", 234400),
		manage_legacy_positions=_get_env_bool("QUANT_MANAGE_LEGACY_POSITIONS", False),
		activation_margin_percent=max(primary_activation - delta, 0.0),
		max_open_positions=_get_env_int("QUANT_STRATEGY_MAX_OPEN_POSITIONS", 8),
		session_start_hour_utc=_get_env_int(
			"QUANT_SESSION_START_HOUR_UTC",
			get_parallel_strategy_context().session_start_hour_utc,
		),
		session_end_hour_utc=_get_env_int(
			"QUANT_SESSION_END_HOUR_UTC",
			get_parallel_strategy_context().session_end_hour_utc,
		),
		friday_cutoff_hour_utc=_get_env_int(
			"QUANT_FRIDAY_CUTOFF_HOUR_UTC",
			get_parallel_strategy_context().friday_cutoff_hour_utc,
		),
	)


def get_index_strategy_context() -> StrategyContext:
	primary_activation = get_primary_strategy_context().activation_margin_percent
	return StrategyContext(
		strategy_id=os.getenv("INDEX_STRATEGY_ID", "gemini_indices"),
		magic=_get_env_int("INDEX_STRATEGY_MAGIC", 234100),
		manage_legacy_positions=_get_env_bool("INDEX_MANAGE_LEGACY_POSITIONS", False),
		activation_margin_percent=_get_env_float("INDEX_STRATEGY_ACTIVATION_MARGIN_PERCENT", primary_activation),
		max_open_positions=_get_env_int("INDEX_MAX_OPEN_POSITIONS", 0),
		session_start_hour_utc=_get_env_int(
			"INDEX_SESSION_START_HOUR_UTC",
			get_primary_strategy_context().session_start_hour_utc,
		),
		session_end_hour_utc=_get_env_int(
			"INDEX_SESSION_END_HOUR_UTC",
			get_primary_strategy_context().session_end_hour_utc,
		),
		friday_cutoff_hour_utc=_get_env_int(
			"INDEX_FRIDAY_CUTOFF_HOUR_UTC",
			get_primary_strategy_context().friday_cutoff_hour_utc,
		),
	)


def is_strategy_trade_window_open(context: StrategyContext, now_utc: Optional[datetime] = None) -> bool:
	now = now_utc or datetime.now(tz=timezone.utc)
	current_hour = now.hour

	if now.weekday() == 4 and current_hour >= max(min(context.friday_cutoff_hour_utc, 24), 0):
		return False

	start_hour = max(min(context.session_start_hour_utc, 24), 0)
	end_hour = max(min(context.session_end_hour_utc, 24), 0)
	if start_hour == end_hour:
		return True
	if start_hour < end_hour:
		return start_hour <= current_hour < end_hour
	return current_hour >= start_hour or current_hour < end_hour


def build_strategy_comment(strategy_id: str) -> str:
	return f"ga:{strategy_id}"


def _get_position_magic(position: Any) -> int:
	if isinstance(position, dict):
		return int(position.get("magic", 0) or 0)
	return int(getattr(position, "magic", 0) or 0)


def _get_position_comment(position: Any) -> str:
	if isinstance(position, dict):
		return str(position.get("comment", "") or "")
	return str(getattr(position, "comment", "") or "")


def _is_known_strategy_position(position: Any, contexts: Iterable[StrategyContext]) -> bool:
	magic = _get_position_magic(position)
	comment = _get_position_comment(position).lower()
	for context in contexts:
		if magic == context.magic:
			return True
		if context.strategy_id.lower() in comment or build_strategy_comment(context.strategy_id).lower() in comment:
			return True
	return False


def position_belongs_to_strategy(position: Any, context: StrategyContext) -> bool:
	magic = _get_position_magic(position)
	comment = _get_position_comment(position).lower()
	if magic == context.magic:
		return True
	if context.strategy_id.lower() in comment or build_strategy_comment(context.strategy_id).lower() in comment:
		return True
	if not context.manage_legacy_positions:
		return False

	known_contexts = [
		get_primary_strategy_context(),
		get_parallel_strategy_context(),
		get_reversal_strategy_context(),
		get_quant_strategy_context(),
		get_index_strategy_context(),
	]
	if _is_known_strategy_position(position, known_contexts):
		return False
	return magic == 0


def count_open_positions_for_strategy(open_positions: List[dict], context: StrategyContext) -> int:
	return len([position for position in open_positions if position_belongs_to_strategy(position, context)])