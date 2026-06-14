"""Profit-only position management for strategy-owned trades."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import MetaTrader5 as mt5

from strategy_context import StrategyContext, get_index_strategy_context, get_primary_strategy_context, position_belongs_to_strategy
from swap_rollover import get_swap_block_window
from trade_execution import close_position_by_ticket


DEFAULT_ENABLED = True
DEFAULT_DRY_RUN = True
DEFAULT_ACTIVATION_USD = 0.30
DEFAULT_TARGET_BALANCE_DIVISOR = 10.0
DEFAULT_ACTIVATION_TARGET_RATIO = 0.50
DEFAULT_RETRACE_RATIO = 0.55
DEFAULT_STALE_HOURS = 12
DEFAULT_MAX_HOLD_DAYS = 5
DEFAULT_STRATEGY_ID = "gemini_primary"
DEFAULT_STRATEGY_MAGIC = 234000
FEE_PER_001_LOT = 0.10
STATE_FILE_NAME = "profit_protection_state.json"
LOG_HEADERS = [
	"timestamp",
	"strategy_id",
	"symbol",
	"ticket",
	"net_profit",
	"max_net_profit",
	"profit_age_hours",
	"closed",
	"message",
]

_LAST_EVALUATED_MINUTE_KEY: Optional[str] = None


def _iter_env_paths() -> tuple[Path, ...]:
	base_dir = Path(__file__).resolve().parent
	return (
		base_dir / ".env",
		base_dir.parent / ".env",
		Path.cwd() / ".env",
	)


def _load_dotenv_value(key: str) -> Optional[str]:
	for env_path in _iter_env_paths():
		if not env_path.exists():
			continue
		for raw_line in env_path.read_text(encoding="utf-8").splitlines():
			line = raw_line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			loaded_key, value = line.split("=", 1)
			if loaded_key.strip() == key:
				return value.strip().strip('"').strip("'")
	return None


def _to_bool(value: Optional[str], *, default: bool) -> bool:
	if value is None:
		return default
	return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _to_float(value: Optional[str], *, default: float, minimum: float) -> float:
	if value is None:
		return default
	try:
		parsed = float(value)
		if parsed < minimum:
			raise ValueError
		return parsed
	except (TypeError, ValueError):
		return default


def _to_int(value: Optional[str], *, default: int, minimum: int) -> int:
	if value is None:
		return default
	try:
		parsed = int(value)
		if parsed < minimum:
			raise ValueError
		return parsed
	except (TypeError, ValueError):
		return default


def _get_enabled() -> bool:
	return _to_bool(_load_dotenv_value("PROFIT_PROTECTION_STRATEGY_ENABLED"), default=DEFAULT_ENABLED)


def _get_index_strategy_enabled() -> bool:
	return _to_bool(_load_dotenv_value("INDEX_STRATEGY_ENABLED"), default=False)


def _get_dry_run() -> bool:
	return _to_bool(_load_dotenv_value("PROFIT_PROTECTION_STRATEGY_DRY_RUN"), default=DEFAULT_DRY_RUN)


def _get_activation_usd() -> float:
	return _to_float(_load_dotenv_value("PROFIT_PROTECTION_ACTIVATION_USD"), default=DEFAULT_ACTIVATION_USD, minimum=0.0)


def _get_target_balance_divisor() -> float:
	return _to_float(
		_load_dotenv_value("PROFIT_PROTECTION_TARGET_BALANCE_DIVISOR"),
		default=DEFAULT_TARGET_BALANCE_DIVISOR,
		minimum=0.000001,
	)


def _get_activation_target_ratio() -> float:
	return _to_float(
		_load_dotenv_value("PROFIT_PROTECTION_ACTIVATION_TARGET_RATIO"),
		default=DEFAULT_ACTIVATION_TARGET_RATIO,
		minimum=0.0,
	)


def _get_retrace_ratio() -> float:
	return _to_float(_load_dotenv_value("PROFIT_PROTECTION_RETRACE_RATIO"), default=DEFAULT_RETRACE_RATIO, minimum=0.0)


def _get_stale_hours() -> int:
	return _to_int(_load_dotenv_value("PROFIT_PROTECTION_STALE_HOURS"), default=DEFAULT_STALE_HOURS, minimum=1)


def _get_max_hold_days() -> int:
	return _to_int(_load_dotenv_value("PROFIT_PROTECTION_MAX_HOLD_DAYS"), default=DEFAULT_MAX_HOLD_DAYS, minimum=1)


def _get_service_folder() -> Optional[Path]:
	raw_value = _load_dotenv_value("SERVICE_DEST_FOLDER")
	if not raw_value:
		return None
	return Path(raw_value)


def _get_state_path(service_folder: Optional[Path]) -> Path:
	if service_folder is not None:
		state_dir = service_folder / "trade_logs"
	else:
		state_dir = Path(__file__).resolve().parent
	state_dir.mkdir(parents=True, exist_ok=True)
	return state_dir / STATE_FILE_NAME


def _load_state(service_folder: Optional[Path]) -> dict[str, dict[str, float]]:
	state_path = _get_state_path(service_folder)
	if not state_path.exists():
		return {}
	try:
		payload = json.loads(state_path.read_text(encoding="utf-8"))
		return payload if isinstance(payload, dict) else {}
	except (OSError, json.JSONDecodeError):
		return {}


def _save_state(service_folder: Optional[Path], state: dict[str, dict[str, float]]) -> None:
	state_path = _get_state_path(service_folder)
	state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_fee(volume: float) -> float:
	return round((float(volume) / 0.01) * FEE_PER_001_LOT, 2)


def calculate_profit_protection_target_profit_usd(balance: float, volume: float) -> float:
	if balance <= 0 or volume <= 0:
		return 0.0
	return round((float(balance) * float(volume)) / _get_target_balance_divisor(), 2)


def calculate_profit_protection_activation_usd(balance: float, volume: float) -> float:
	base_activation_usd = _get_activation_usd()
	target_profit_usd = calculate_profit_protection_target_profit_usd(balance, volume)
	derived_activation_usd = round(target_profit_usd * _get_activation_target_ratio(), 2)
	return max(base_activation_usd, derived_activation_usd)


def calculate_profit_protection_locked_profit_usd(max_net_profit: float, activation_usd: float) -> float:
	if max_net_profit <= 0:
		return 0.0
	return round(max(max_net_profit * _get_retrace_ratio(), activation_usd), 2)


def get_profit_protection_contexts() -> list[StrategyContext]:
	contexts = [get_primary_strategy_context()]
	if _get_index_strategy_enabled():
		contexts.append(get_index_strategy_context())
	return contexts


def get_profit_protection_context_for_position(position: Any) -> Optional[StrategyContext]:
	for context in get_profit_protection_contexts():
		if position_belongs_to_strategy(position, context):
			return context
	return None


def is_position_under_profit_protection(position: Any) -> bool:
	if not _get_enabled():
		return False
	return get_profit_protection_context_for_position(position) is not None


def _log_action(
	*,
	service_folder: Optional[Path],
	now_utc: datetime,
	strategy_id: str,
	symbol: str,
	ticket: int,
	net_profit: float,
	max_net_profit: float,
	profit_age_hours: float,
	closed: bool,
	message: str,
) -> None:
	if service_folder is None:
		return
	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	log_file = log_dir / "profit_protection.csv"
	file_exists = log_file.exists()
	with open(log_file, "a", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		if not file_exists:
			writer.writerow(LOG_HEADERS)
		writer.writerow(
			[
				now_utc.isoformat(),
				strategy_id,
				symbol,
				ticket,
				f"{net_profit:.2f}",
				f"{max_net_profit:.2f}",
				f"{profit_age_hours:.2f}",
				str(closed),
				message,
			]
		)


def run_profit_protection_strategy_if_due() -> None:
	"""Manage profitable strategy-owned positions at most once per minute."""
	global _LAST_EVALUATED_MINUTE_KEY
	if not _get_enabled():
		return

	now_utc = datetime.now(tz=timezone.utc)
	minute_key = now_utc.strftime("%Y%m%d%H%M")
	if _LAST_EVALUATED_MINUTE_KEY == minute_key:
		return
	_LAST_EVALUATED_MINUTE_KEY = minute_key

	window = get_swap_block_window(now_utc=now_utc)
	if window.contains(now_utc):
		return

	positions = mt5.positions_get()
	if positions is None:
		return
	account_info = mt5.account_info()
	if account_info is None:
		return
	balance = float(getattr(account_info, "balance", 0.0) or 0.0)

	service_folder = _get_service_folder()
	state = _load_state(service_folder)
	retrace_ratio = _get_retrace_ratio()
	stale_hours = _get_stale_hours()
	max_hold_days = _get_max_hold_days()
	dry_run = _get_dry_run()
	updated_state = dict(state)

	for position in positions:
		strategy_context = get_profit_protection_context_for_position(position)
		if strategy_context is None:
			continue

		ticket = int(getattr(position, "ticket", 0) or 0)
		if ticket <= 0:
			continue
		volume = float(getattr(position, "volume", 0.0) or 0.0)
		if volume <= 0:
			continue

		profit = float(getattr(position, "profit", 0.0) or 0.0)
		swap = float(getattr(position, "swap", 0.0) or 0.0)
		net_profit = round(profit + swap - _get_fee(volume), 2)
		if net_profit <= 0:
			continue
		activation_usd = calculate_profit_protection_activation_usd(balance, volume)

		opened_at = datetime.fromtimestamp(int(getattr(position, "time", 0) or 0), tz=timezone.utc)
		age_hours = max((now_utc - opened_at).total_seconds() / 3600.0, 0.0)
		state_key = f"{strategy_context.strategy_id}:{ticket}"
		position_state = updated_state.get(state_key, {})
		max_net_profit = max(float(position_state.get("max_net_profit", 0.0) or 0.0), net_profit)
		locked_net_profit = calculate_profit_protection_locked_profit_usd(max_net_profit, activation_usd)
		updated_state[state_key] = {
			"max_net_profit": max_net_profit,
			"current_net_profit": net_profit,
			"locked_net_profit": locked_net_profit,
		}

		close_reason: Optional[str] = None
		if max_net_profit >= activation_usd and net_profit <= locked_net_profit:
			locked_profit_usd = locked_net_profit
			close_reason = (
				f"profit retracement detected: current {net_profit:.2f} <= "
				f"locked {locked_profit_usd:.2f} (activation {activation_usd:.2f})"
			)
		elif age_hours >= stale_hours and net_profit > activation_usd:
			close_reason = f"profitable stale position older than {stale_hours}h"
		elif age_hours >= (24.0 * max_hold_days) and net_profit > 0:
			close_reason = f"profitable position older than {max_hold_days} days"

		if close_reason is None:
			continue

		print(
			f"💰 Profit protection [{strategy_context.strategy_id}] {getattr(position, 'symbol', '')} "
			f"ticket={ticket} net={net_profit:.2f} max={max_net_profit:.2f} -> {close_reason}"
		)
		if dry_run:
			closed = False
			message = f"DRY-RUN: {close_reason}"
		else:
			closed = close_position_by_ticket(
				position_ticket=ticket,
				symbol=str(getattr(position, "symbol", "")),
				position_type=int(getattr(position, "type", 0)),
				volume=volume,
				comment=f"pp:{strategy_context.strategy_id}",
				magic=strategy_context.magic,
			)
			message = close_reason if closed else f"close failed: {close_reason}"

		_log_action(
			service_folder=service_folder,
			now_utc=now_utc,
			strategy_id=strategy_context.strategy_id,
			symbol=str(getattr(position, "symbol", "")),
			ticket=ticket,
			net_profit=net_profit,
			max_net_profit=max_net_profit,
			profit_age_hours=age_hours,
			closed=closed,
			message=message,
		)
		if closed:
			updated_state.pop(state_key, None)

	_save_state(service_folder, updated_state)