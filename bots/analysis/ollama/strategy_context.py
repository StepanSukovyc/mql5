from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional

import pytz


PRAGUE_TZ = pytz.timezone("Europe/Prague")


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
	session_start_hour_prague: Optional[int] = None
	session_start_minute_prague: int = 0


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


def _session_start_kwargs(
	name: str,
	fallback_utc_hour: int,
	fallback_prague_hour: Optional[int] = None,
	fallback_prague_minute: int = 0,
) -> dict:
	prague_hour_name = f"{name}_SESSION_START_HOUR_PRAGUE"
	if os.getenv(prague_hour_name) is not None:
		return {
			"session_start_hour_utc": fallback_utc_hour,
			"session_start_hour_prague": _get_env_int(prague_hour_name, 0),
			"session_start_minute_prague": _get_env_int(f"{name}_SESSION_START_MINUTE_PRAGUE", 0),
		}

	utc_hour_name = f"{name}_SESSION_START_HOUR_UTC"
	if os.getenv(utc_hour_name) is not None:
		return {
			"session_start_hour_utc": _get_env_int(utc_hour_name, fallback_utc_hour),
			"session_start_hour_prague": None,
			"session_start_minute_prague": 0,
		}

	return {
		"session_start_hour_utc": fallback_utc_hour,
		"session_start_hour_prague": fallback_prague_hour,
		"session_start_minute_prague": fallback_prague_minute,
	}


def prague_local_time_to_utc_minute(now_utc: datetime, hour: int, minute: int) -> int:
	if now_utc.tzinfo is None:
		now_utc = now_utc.replace(tzinfo=timezone.utc)
	else:
		now_utc = now_utc.astimezone(timezone.utc)

	local_date = now_utc.astimezone(PRAGUE_TZ).date()
	local_start = PRAGUE_TZ.localize(datetime(local_date.year, local_date.month, local_date.day, hour, minute))
	start_utc = local_start.astimezone(timezone.utc)
	return start_utc.hour * 60 + start_utc.minute


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
		**_session_start_kwargs("PRIMARY", 0),
		session_end_hour_utc=_get_env_int("PRIMARY_SESSION_END_HOUR_UTC", 24),
		friday_cutoff_hour_utc=_get_env_int("PRIMARY_FRIDAY_CUTOFF_HOUR_UTC", 24),
	)


def get_parallel_strategy_context() -> StrategyContext:
	primary_context = get_primary_strategy_context()
	primary_activation = primary_context.activation_margin_percent
	delta = _get_env_float("PARALLEL_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT", 5.0)
	return StrategyContext(
		strategy_id=os.getenv("PARALLEL_STRATEGY_ID", "parallel_mean_reversion"),
		magic=_get_env_int("PARALLEL_STRATEGY_MAGIC", 234200),
		manage_legacy_positions=_get_env_bool("PARALLEL_MANAGE_LEGACY_POSITIONS", False),
		activation_margin_percent=max(primary_activation - delta, 0.0),
		max_open_positions=_get_env_int("PARALLEL_STRATEGY_MAX_OPEN_POSITIONS", 7),
		**_session_start_kwargs(
			"PARALLEL",
			primary_context.session_start_hour_utc,
			primary_context.session_start_hour_prague,
			primary_context.session_start_minute_prague,
		),
		session_end_hour_utc=_get_env_int(
			"PARALLEL_SESSION_END_HOUR_UTC",
			primary_context.session_end_hour_utc,
		),
		friday_cutoff_hour_utc=_get_env_int(
			"PARALLEL_FRIDAY_CUTOFF_HOUR_UTC",
			primary_context.friday_cutoff_hour_utc,
		),
	)


def get_reversal_strategy_context() -> StrategyContext:
	parallel_context = get_parallel_strategy_context()
	primary_activation = get_primary_strategy_context().activation_margin_percent
	delta = _get_env_float("REVERSAL_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT", 5.0)
	return StrategyContext(
		strategy_id=os.getenv("REVERSAL_STRATEGY_ID", "reversal_pattern"),
		magic=_get_env_int("REVERSAL_STRATEGY_MAGIC", 234300),
		manage_legacy_positions=_get_env_bool("REVERSAL_MANAGE_LEGACY_POSITIONS", False),
		activation_margin_percent=max(primary_activation - delta, 0.0),
		max_open_positions=_get_env_int("REVERSAL_STRATEGY_MAX_OPEN_POSITIONS", 3),
		**_session_start_kwargs(
			"REVERSAL",
			parallel_context.session_start_hour_utc,
			parallel_context.session_start_hour_prague,
			parallel_context.session_start_minute_prague,
		),
		session_end_hour_utc=_get_env_int(
			"REVERSAL_SESSION_END_HOUR_UTC",
			parallel_context.session_end_hour_utc,
		),
		friday_cutoff_hour_utc=_get_env_int(
			"REVERSAL_FRIDAY_CUTOFF_HOUR_UTC",
			parallel_context.friday_cutoff_hour_utc,
		),
	)


def get_quant_strategy_context() -> StrategyContext:
	parallel_context = get_parallel_strategy_context()
	primary_activation = get_primary_strategy_context().activation_margin_percent
	delta = _get_env_float("QUANT_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT", 8.0)
	return StrategyContext(
		strategy_id=os.getenv("QUANT_STRATEGY_ID", "quant_math"),
		magic=_get_env_int("QUANT_STRATEGY_MAGIC", 234400),
		manage_legacy_positions=_get_env_bool("QUANT_MANAGE_LEGACY_POSITIONS", False),
		activation_margin_percent=max(primary_activation - delta, 0.0),
		max_open_positions=_get_env_int("QUANT_STRATEGY_MAX_OPEN_POSITIONS", 8),
		**_session_start_kwargs(
			"QUANT",
			parallel_context.session_start_hour_utc,
			parallel_context.session_start_hour_prague,
			parallel_context.session_start_minute_prague,
		),
		session_end_hour_utc=_get_env_int(
			"QUANT_SESSION_END_HOUR_UTC",
			parallel_context.session_end_hour_utc,
		),
		friday_cutoff_hour_utc=_get_env_int(
			"QUANT_FRIDAY_CUTOFF_HOUR_UTC",
			parallel_context.friday_cutoff_hour_utc,
		),
	)


def get_index_strategy_context() -> StrategyContext:
	primary_context = get_primary_strategy_context()
	primary_activation = primary_context.activation_margin_percent
	return StrategyContext(
		strategy_id=os.getenv("INDEX_STRATEGY_ID", "gemini_indices"),
		magic=_get_env_int("INDEX_STRATEGY_MAGIC", 234100),
		manage_legacy_positions=_get_env_bool("INDEX_MANAGE_LEGACY_POSITIONS", False),
		activation_margin_percent=_get_env_float("INDEX_STRATEGY_ACTIVATION_MARGIN_PERCENT", primary_activation),
		max_open_positions=_get_env_int("INDEX_MAX_OPEN_POSITIONS", 0),
		**_session_start_kwargs(
			"INDEX",
			primary_context.session_start_hour_utc,
			primary_context.session_start_hour_prague,
			primary_context.session_start_minute_prague,
		),
		session_end_hour_utc=_get_env_int(
			"INDEX_SESSION_END_HOUR_UTC",
			primary_context.session_end_hour_utc,
		),
		friday_cutoff_hour_utc=_get_env_int(
			"INDEX_FRIDAY_CUTOFF_HOUR_UTC",
			primary_context.friday_cutoff_hour_utc,
		),
	)


def is_gemini_prediction_strategy_enabled() -> bool:
	return _get_env_bool("GEMINI_PREDICTION_ENABLED", False)


def get_gemini_prediction_strategy_context() -> StrategyContext:
	cloud_context = get_ollama_cloud_strategy_context()
	return StrategyContext(
		strategy_id=os.getenv("GEMINI_PREDICTION_STRATEGY_ID", "gemini_prediction"),
		magic=_get_env_int("GEMINI_PREDICTION_STRATEGY_MAGIC", 234700),
		manage_legacy_positions=False,
		activation_margin_percent=_get_env_float(
			"GEMINI_PREDICTION_STRATEGY_ACTIVATION_MARGIN_PERCENT",
			cloud_context.activation_margin_percent,
		),
		max_open_positions=_get_env_int(
			"GEMINI_PREDICTION_MAX_OPEN_POSITIONS",
			cloud_context.max_open_positions,
		),
		**_session_start_kwargs(
			"GEMINI_PREDICTION",
			cloud_context.session_start_hour_utc,
			cloud_context.session_start_hour_prague,
			cloud_context.session_start_minute_prague,
		),
		session_end_hour_utc=_get_env_int(
			"GEMINI_PREDICTION_SESSION_END_HOUR_UTC",
			cloud_context.session_end_hour_utc,
		),
		friday_cutoff_hour_utc=_get_env_int(
			"GEMINI_PREDICTION_FRIDAY_CUTOFF_HOUR_UTC",
			cloud_context.friday_cutoff_hour_utc,
		),
	)


def get_ollama_cloud_strategy_context() -> StrategyContext:
	primary_activation = get_primary_strategy_context().activation_margin_percent
	return StrategyContext(
		strategy_id=os.getenv("OLLAMA_CLOUD_STRATEGY_ID", "ollama_cloud_primary"),
		magic=_get_env_int("OLLAMA_CLOUD_STRATEGY_MAGIC", 234500),
		manage_legacy_positions=_get_env_bool("OLLAMA_CLOUD_MANAGE_LEGACY_POSITIONS", False),
		activation_margin_percent=_get_env_float(
			"OLLAMA_CLOUD_STRATEGY_ACTIVATION_MARGIN_PERCENT", primary_activation
		),
		max_open_positions=_get_env_int("OLLAMA_CLOUD_MAX_OPEN_POSITIONS", 5),
		**_session_start_kwargs("OLLAMA_CLOUD", 6),
		session_end_hour_utc=_get_env_int("OLLAMA_CLOUD_SESSION_END_HOUR_UTC", 20),
		friday_cutoff_hour_utc=_get_env_int("OLLAMA_CLOUD_FRIDAY_CUTOFF_HOUR_UTC", 16),
	)


def get_scalping_strategy_context() -> StrategyContext:
	"""Context pro Liquidity Sweep Scalping strategii (magic 234600)."""
	return StrategyContext(
		strategy_id=os.getenv("SCALP_STRATEGY_ID", "liquidity_sweep_scalping"),
		magic=_get_env_int("SCALP_STRATEGY_MAGIC", 234600),
		manage_legacy_positions=False,
		activation_margin_percent=_get_env_float("SCALP_ACTIVATION_MARGIN_PERCENT", 5.0),
		max_open_positions=_get_env_int("SCALP_MAX_POSITIONS_TOTAL", 3),
		session_start_hour_utc=0,
		session_end_hour_utc=24,
		friday_cutoff_hour_utc=_get_env_int("SCALP_FRIDAY_CUTOFF_HOUR_UTC", 16),
	)


def is_strategy_trade_window_open(context: StrategyContext, now_utc: Optional[datetime] = None) -> bool:
	now = now_utc or datetime.now(tz=timezone.utc)
	if now.tzinfo is None:
		now = now.replace(tzinfo=timezone.utc)
	else:
		now = now.astimezone(timezone.utc)
	current_hour = now.hour

	if now.weekday() == 4 and current_hour >= max(min(context.friday_cutoff_hour_utc, 24), 0):
		return False

	if context.session_start_hour_prague is None:
		start_minute = max(min(context.session_start_hour_utc, 24), 0) * 60
	else:
		start_hour_prague = max(min(context.session_start_hour_prague, 23), 0)
		start_minute_prague = max(min(context.session_start_minute_prague, 59), 0)
		start_minute = prague_local_time_to_utc_minute(now, start_hour_prague, start_minute_prague)

	end_hour = max(min(context.session_end_hour_utc, 24), 0)
	end_minute = end_hour * 60
	if start_minute == end_minute:
		return True
	current_minute = current_hour * 60 + now.minute
	if start_minute < end_minute:
		return start_minute <= current_minute < end_minute
	return current_minute >= start_minute or current_minute < end_minute


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
		get_ollama_cloud_strategy_context(),
		get_gemini_prediction_strategy_context(),
		get_scalping_strategy_context(),
	]
	if _is_known_strategy_position(position, known_contexts):
		return False
	return magic == 0


def count_open_positions_for_strategy(open_positions: List[dict], context: StrategyContext) -> int:
	return len([position for position in open_positions if position_belongs_to_strategy(position, context)])