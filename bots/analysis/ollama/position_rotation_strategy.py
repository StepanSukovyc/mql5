"""Hourly position rotation for accounts holding more than seven positions."""

from __future__ import annotations

import csv
import json
import math
import os
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol, cast

import MetaTrader5 as mt5
import pytz

from position_costs import get_position_charged_costs
from swap_rollover import PRAGUE_TZ
from trade_execution import close_position_by_ticket


DEFAULT_ENABLED = False
DEFAULT_DRY_RUN = True
MIN_OPEN_POSITIONS = 7
MIN_POSITION_AGE_SECONDS = 60 * 60
MAX_FALLBACK_CLOSE_ATTEMPTS = 2
DEFAULT_SERVER_TIME_OFFSET_FROM_PRAGUE_HOURS = 1
ACTIVE_START = time(hour=7, minute=25)
ACTIVE_END = time(hour=20, minute=34)
PROFIT_WINDOW_START_MINUTE = 25
PROFIT_WINDOW_END_MINUTE = 30
LAST_ACTIVE_MINUTE = 34
STATE_FILE_NAME = "position_rotation_state.json"
LOG_FILE_NAME = "position_rotation.csv"
LOG_HEADERS = [
	"timestamp_utc",
	"window_key_prague",
	"phase",
	"dry_run",
	"open_position_count",
	"ticket",
	"symbol",
	"age_minutes",
	"profit",
	"swap",
	"charged_costs",
	"net_profit",
	"result",
	"message",
]

_RUN_LOCK = threading.Lock()


class MT5Position(Protocol):
	ticket: int
	symbol: str
	type: int
	volume: float
	time: float
	profit: float
	swap: float


@dataclass(frozen=True)
class PositionSnapshot:
	ticket: int
	symbol: str
	position_type: int
	volume: float
	opened_at: float
	age_seconds: float
	profit: float
	swap: float
	charged_costs: float
	net_profit: float


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


def _is_enabled() -> bool:
	raw = os.getenv("POSITION_ROTATION_STRATEGY_ENABLED")
	if raw is None:
		raw = _load_dotenv_value("POSITION_ROTATION_STRATEGY_ENABLED")
	return _to_bool(raw, default=DEFAULT_ENABLED)


def _is_dry_run() -> bool:
	raw = os.getenv("POSITION_ROTATION_STRATEGY_DRY_RUN")
	if raw is None:
		raw = _load_dotenv_value("POSITION_ROTATION_STRATEGY_DRY_RUN")
	return _to_bool(raw, default=DEFAULT_DRY_RUN)


def _get_service_folder() -> Optional[Path]:
	raw = os.getenv("SERVICE_DEST_FOLDER")
	if raw is None:
		raw = _load_dotenv_value("SERVICE_DEST_FOLDER")
	return Path(raw) if raw else None


def _get_active_bounds() -> tuple[time, time]:
	def read_component(name: str, default: int, maximum: int) -> int:
		raw = os.getenv(name)
		if raw is None:
			raw = _load_dotenv_value(name)
		if raw is None:
			return default
		try:
			value = int(raw)
		except ValueError as exc:
			raise RuntimeError(f"Invalid {name}: {raw!r}") from exc
		if not 0 <= value <= maximum:
			raise RuntimeError(f"Invalid {name}: {raw!r}")
		return value

	start = time(
		hour=read_component("POSITION_ROTATION_STRATEGY_START_HOUR", ACTIVE_START.hour, 23),
		minute=read_component("POSITION_ROTATION_STRATEGY_START_MINUTE", ACTIVE_START.minute, 59),
	)
	end = time(
		hour=read_component("POSITION_ROTATION_STRATEGY_END_HOUR", ACTIVE_END.hour, 23),
		minute=read_component("POSITION_ROTATION_STRATEGY_END_MINUTE", ACTIVE_END.minute, 59),
	)
	if end <= start:
		raise RuntimeError("Position rotation end time must be after its start time")
	return start, end


def _get_state_path(service_folder: Path) -> Path:
	state_dir = service_folder / "trade_logs"
	state_dir.mkdir(parents=True, exist_ok=True)
	return state_dir / STATE_FILE_NAME


def _new_window_state(window_key: str) -> dict[str, Any]:
	return {
		"window_key": window_key,
		"last_evaluated_minute": None,
		"observed_profit_minutes": [],
		"previous_open_tickets": None,
		"position_closed_during_window": False,
		"profitable_position_seen": False,
		"dry_run_closed_tickets": [],
		"fallback_evaluated": False,
	}


def _load_state(service_folder: Path, window_key: str) -> dict[str, Any]:
	state_path = _get_state_path(service_folder)
	if not state_path.exists():
		return _new_window_state(window_key)
	try:
		payload = json.loads(state_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise RuntimeError(f"Unable to read position rotation state: {exc}") from exc
	if not isinstance(payload, dict):
		raise RuntimeError("Position rotation state must be a JSON object")
	state = cast(dict[str, Any], payload)
	if state.get("window_key") != window_key:
		return _new_window_state(window_key)
	for key in ("position_closed_during_window", "profitable_position_seen", "fallback_evaluated"):
		if not isinstance(state.get(key), bool):
			raise RuntimeError(f"Invalid position rotation state field: {key}")
	if not isinstance(state.get("observed_profit_minutes"), list):
		raise RuntimeError("Invalid position rotation state field: observed_profit_minutes")
	if not isinstance(state.get("dry_run_closed_tickets"), list):
		raise RuntimeError("Invalid position rotation state field: dry_run_closed_tickets")
	if not isinstance(state.get("previous_open_tickets"), (list, type(None))):
		raise RuntimeError("Invalid position rotation state field: previous_open_tickets")
	return state


def _save_state(service_folder: Path, state: dict[str, Any]) -> None:
	state_path = _get_state_path(service_folder)
	temp_path = state_path.with_name(f"{state_path.name}.tmp")
	temp_path.write_text(
		json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False),
		encoding="utf-8",
	)
	temp_path.replace(state_path)


def _normalize_now(now_utc: Optional[datetime]) -> datetime:
	now = now_utc or datetime.now(tz=timezone.utc)
	if now.tzinfo is None:
		return now.replace(tzinfo=timezone.utc)
	return now.astimezone(timezone.utc)


def _position_time_to_utc(position_time: float) -> datetime:
	raw_offset = os.getenv("POSITION_ROTATION_SERVER_OFFSET_FROM_PRAGUE_HOURS")
	if raw_offset is None:
		raw_offset = _load_dotenv_value("POSITION_ROTATION_SERVER_OFFSET_FROM_PRAGUE_HOURS")
	if raw_offset is None:
		offset_hours = DEFAULT_SERVER_TIME_OFFSET_FROM_PRAGUE_HOURS
	else:
		try:
			offset_hours = float(raw_offset)
		except ValueError as exc:
			raise RuntimeError(
				"Invalid POSITION_ROTATION_SERVER_OFFSET_FROM_PRAGUE_HOURS: "
				f"{raw_offset!r}"
			) from exc
	if not math.isfinite(offset_hours) or not -14 <= offset_hours <= 14:
		raise RuntimeError(
			"POSITION_ROTATION_SERVER_OFFSET_FROM_PRAGUE_HOURS must be between -14 and 14"
		)

	server_wall_time = datetime.fromtimestamp(position_time, tz=timezone.utc).replace(tzinfo=None)
	prague_wall_time = server_wall_time - timedelta(hours=offset_hours)
	try:
		return PRAGUE_TZ.localize(prague_wall_time, is_dst=None).astimezone(timezone.utc)
	except (pytz.AmbiguousTimeError, pytz.NonExistentTimeError) as exc:
		raise ValueError(f"Position time cannot be resolved in the Prague timezone: {exc}") from exc


def is_position_rotation_active(now_utc: datetime) -> bool:
	now_prague = _normalize_now(now_utc).astimezone(PRAGUE_TZ)
	local_time = time(hour=now_prague.hour, minute=now_prague.minute)
	start, end = _get_active_bounds()
	return (
		start <= local_time <= end
		and PROFIT_WINDOW_START_MINUTE <= now_prague.minute <= LAST_ACTIVE_MINUTE
	)


def _snapshot_positions(now_utc: datetime) -> list[PositionSnapshot]:
	positions_get = cast(
		Callable[[], Optional[Iterable[MT5Position]]],
		getattr(mt5, "positions_get"),
	)
	positions = positions_get()
	if positions is None:
		last_error = cast(Callable[[], object], getattr(mt5, "last_error"))()
		raise RuntimeError(f"Failed to get positions for position rotation: {last_error}")

	snapshots: list[PositionSnapshot] = []
	for position in positions:
		try:
			server_opened_at = float(position.time)
			profit = float(position.profit)
			swap = float(position.swap)
			volume = float(position.volume)
			charged_costs = float(get_position_charged_costs(position))
			values = (server_opened_at, profit, swap, volume, charged_costs)
			if (
				not all(math.isfinite(value) for value in values)
				or server_opened_at <= 0
				or volume <= 0
			):
				raise ValueError("Position contains invalid numeric values")
			opened_at = _position_time_to_utc(server_opened_at).timestamp()
			snapshots.append(
				PositionSnapshot(
					ticket=int(position.ticket),
					symbol=str(position.symbol),
					position_type=int(position.type),
					volume=volume,
					opened_at=opened_at,
					age_seconds=now_utc.timestamp() - opened_at,
					profit=profit,
					swap=swap,
					charged_costs=charged_costs,
					net_profit=round(profit + swap + charged_costs, 2),
				)
			)
		except (AttributeError, TypeError, ValueError, OverflowError) as exc:
			raise RuntimeError(
				f"Unable to evaluate position {getattr(position, 'ticket', 'unknown')}: {exc}"
			) from exc
	return snapshots


def _log_action(
	service_folder: Path,
	*,
	now_utc: datetime,
	window_key: str,
	phase: str,
	dry_run: bool,
	open_position_count: int,
	candidate: Optional[PositionSnapshot],
	result: str,
	message: str,
) -> None:
	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	log_path = log_dir / LOG_FILE_NAME
	write_header = not log_path.exists()
	with log_path.open("a", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		if write_header:
			writer.writerow(LOG_HEADERS)
		writer.writerow([
			now_utc.isoformat(),
			window_key,
			phase,
			str(dry_run),
			open_position_count,
			candidate.ticket if candidate else "",
			candidate.symbol if candidate else "",
			f"{candidate.age_seconds / 60:.2f}" if candidate else "",
			f"{candidate.profit:.2f}" if candidate else "",
			f"{candidate.swap:.2f}" if candidate else "",
			f"{candidate.charged_costs:.2f}" if candidate else "",
			f"{candidate.net_profit:.2f}" if candidate else "",
			result,
			message,
		])


def _eligible_old_profits(positions: list[PositionSnapshot]) -> list[PositionSnapshot]:
	return sorted(
		(
			position for position in positions
			if position.age_seconds > MIN_POSITION_AGE_SECONDS and position.net_profit > 0
		),
		key=lambda position: position.net_profit,
		reverse=True,
	)


def _close_old_profitable_positions(
	service_folder: Path,
	state: dict[str, Any],
	*,
	now_utc: datetime,
	window_key: str,
	dry_run: bool,
	candidates: list[PositionSnapshot],
) -> None:
	state["profitable_position_seen"] = True
	_save_state(service_folder, state)
	closed_tickets: set[int] = set()
	dry_run_closed_tickets = set(state["dry_run_closed_tickets"])
	for candidate in candidates:
		positions = _snapshot_positions(now_utc)
		open_count = len(positions)
		reflected_tickets = {position.ticket for position in positions}
		effective_open_count = open_count - len(closed_tickets & reflected_tickets)
		if dry_run:
			effective_open_count -= len(dry_run_closed_tickets & reflected_tickets)
		if effective_open_count <= MIN_OPEN_POSITIONS:
			break
		current = next((position for position in positions if position.ticket == candidate.ticket), None)
		if (
			current is None
			or current.age_seconds <= MIN_POSITION_AGE_SECONDS
			or current.net_profit <= 0
		):
			continue
		if dry_run:
			_log_action(
				service_folder, now_utc=now_utc, window_key=window_key, phase="profit",
				dry_run=True, open_position_count=effective_open_count, candidate=current,
				result="dry_run", message="Eligible old profitable position; no order sent",
			)
			dry_run_closed_tickets.add(current.ticket)
			state["dry_run_closed_tickets"] = sorted(dry_run_closed_tickets)
			_save_state(service_folder, state)
			continue
		close_errors: list[str] = []
		closed = close_position_by_ticket(
			position_ticket=current.ticket,
			symbol=current.symbol,
			position_type=current.position_type,
			volume=current.volume,
			comment="Position rotation profit",
			on_failure=close_errors.append,
		)
		_log_action(
			service_folder, now_utc=now_utc, window_key=window_key, phase="profit",
			dry_run=False, open_position_count=open_count, candidate=current,
			result="closed" if closed else "close_failed",
			message=(
				"Closed old profitable position" if closed
				else close_errors[0] if close_errors else "Position close failed; no diagnostic provided"
			),
		)
		if closed:
			closed_tickets.add(current.ticket)


def _run_loss_fallback(
	service_folder: Path,
	state: dict[str, Any],
	*,
	now_utc: datetime,
	window_key: str,
	dry_run: bool,
) -> None:
	state["fallback_evaluated"] = True
	_save_state(service_folder, state)
	attempted_tickets: set[int] = set()
	closed_tickets: set[int] = set()
	attempts = 0

	while attempts < MAX_FALLBACK_CLOSE_ATTEMPTS:
		positions = _snapshot_positions(now_utc)
		reflected_tickets = {position.ticket for position in positions}
		open_count = len(positions) - len(closed_tickets & reflected_tickets)
		if open_count <= MIN_OPEN_POSITIONS:
			_log_action(
				service_folder, now_utc=now_utc, window_key=window_key, phase="fallback",
				dry_run=dry_run, open_position_count=open_count, candidate=None,
				result="position_floor", message="Keeping at least seven account positions",
			)
			return

		old_profits = _eligible_old_profits(positions)
		if old_profits:
			state["profitable_position_seen"] = True
			_save_state(service_folder, state)
			_close_old_profitable_positions(
				service_folder, state, now_utc=now_utc, window_key=window_key,
				dry_run=dry_run, candidates=old_profits,
			)
			return

		young_profits = [
			position for position in positions
			if position.age_seconds <= MIN_POSITION_AGE_SECONDS and position.net_profit > 0
		]
		if young_profits:
			_log_action(
				service_folder, now_utc=now_utc, window_key=window_key, phase="fallback",
				dry_run=dry_run, open_position_count=open_count, candidate=None,
				result="young_profit", message="Young profitable positions prevent loss cleanup",
			)
			return

		loss_candidates = sorted(
			(
				position for position in positions
				if position.age_seconds > MIN_POSITION_AGE_SECONDS
				and position.net_profit < 0
				and position.ticket not in attempted_tickets
			),
			key=lambda position: position.net_profit,
			reverse=True,
		)
		if not loss_candidates:
			_log_action(
				service_folder, now_utc=now_utc, window_key=window_key, phase="fallback",
				dry_run=dry_run, open_position_count=open_count, candidate=None,
				result="no_loss_candidate", message="No old net-losing positions to rotate",
			)
			return

		current = loss_candidates[0]
		attempted_tickets.add(current.ticket)
		attempts += 1
		if dry_run:
			result = "dry_run"
			message = "Least-losing old position selected; no order sent"
		else:
			close_errors: list[str] = []
			closed = close_position_by_ticket(
				position_ticket=current.ticket,
				symbol=current.symbol,
				position_type=current.position_type,
				volume=current.volume,
				comment="Rotation loss",
				on_failure=close_errors.append,
			)
			result = "closed" if closed else "close_failed"
			message = (
				"Closed least-losing old position" if closed
				else close_errors[0] if close_errors else "Position close failed; no diagnostic provided"
			)
			if closed:
				closed_tickets.add(current.ticket)
		_log_action(
			service_folder, now_utc=now_utc, window_key=window_key, phase="fallback",
			dry_run=dry_run, open_position_count=open_count, candidate=current,
			result=result, message=message,
		)


def run_position_rotation_strategy_if_due(*, now_utc: Optional[datetime] = None) -> None:
	"""Rotate old positions during the Prague-time hourly window, preserving seven positions."""
	if not _is_enabled():
		return

	now = _normalize_now(now_utc)
	if not is_position_rotation_active(now):
		return

	service_folder = _get_service_folder()
	if service_folder is None:
		raise RuntimeError("SERVICE_DEST_FOLDER is required for position rotation")
	dry_run = _is_dry_run()
	now_prague = now.astimezone(PRAGUE_TZ)
	window_key = now_prague.strftime("%Y-%m-%d-%H")
	minute_key = now_prague.strftime("%Y-%m-%d-%H-%M")

	with _RUN_LOCK:
		state = _load_state(service_folder, window_key)
		if state.get("last_evaluated_minute") == minute_key or state["fallback_evaluated"]:
			return

		if now_prague.minute < PROFIT_WINDOW_END_MINUTE:
			observed_minutes = set(state["observed_profit_minutes"])
			observed_minutes.add(minute_key)
			state["observed_profit_minutes"] = sorted(observed_minutes)

		positions = _snapshot_positions(now)
		current_tickets = sorted(position.ticket for position in positions)
		reflected_tickets = set(current_tickets)
		previous_tickets = state["previous_open_tickets"]
		if previous_tickets is not None and set(previous_tickets) - set(current_tickets):
			state["position_closed_during_window"] = True
		state["previous_open_tickets"] = current_tickets

		effective_open_count = len(positions)
		if dry_run:
			effective_open_count -= len(set(state["dry_run_closed_tickets"]) & reflected_tickets)
		if effective_open_count <= MIN_OPEN_POSITIONS:
			_log_action(
				service_folder, now_utc=now, window_key=window_key, phase="scan",
				dry_run=dry_run, open_position_count=effective_open_count, candidate=None,
				result="position_floor", message="Strategy requires more than seven account positions",
			)
			state["last_evaluated_minute"] = minute_key
			_save_state(service_folder, state)
			return

		profit_candidates = [
			position for position in _eligible_old_profits(positions)
			if not dry_run or position.ticket not in state["dry_run_closed_tickets"]
		]
		old_positions = [
			position for position in positions
			if position.age_seconds > MIN_POSITION_AGE_SECONDS
		]
		young_profit_count = sum(
			position.age_seconds <= MIN_POSITION_AGE_SECONDS and position.net_profit > 0
			for position in positions
		)
		best_old_position = max(old_positions, key=lambda position: position.net_profit, default=None)
		_log_action(
			service_folder, now_utc=now, window_key=window_key, phase="scan",
			dry_run=dry_run, open_position_count=effective_open_count,
			candidate=profit_candidates[0] if profit_candidates else best_old_position,
			result="profit_candidates" if profit_candidates else "no_profit_candidates",
			message=(
				f"old_positions={len(old_positions)}, old_net_profitable={len(profit_candidates)}, "
				f"young_net_profitable={young_profit_count}"
			),
		)
		if profit_candidates:
			state["profitable_position_seen"] = True
			_save_state(service_folder, state)
			if now_prague.minute <= PROFIT_WINDOW_END_MINUTE:
				_close_old_profitable_positions(
					service_folder, state, now_utc=now, window_key=window_key,
					dry_run=dry_run, candidates=profit_candidates,
				)
			else:
				_log_action(
					service_folder, now_utc=now, window_key=window_key, phase="profit",
					dry_run=dry_run, open_position_count=len(positions), candidate=profit_candidates[0],
					result="late_candidate",
					message="Late monitor check; candidate suppresses fallback but is outside the profit-close window",
				)
			if now_prague.minute >= PROFIT_WINDOW_END_MINUTE:
				state["fallback_evaluated"] = True
		elif now_prague.minute >= PROFIT_WINDOW_END_MINUTE:
			observed_minutes = set(state["observed_profit_minutes"])
			if state["profitable_position_seen"] or state["position_closed_during_window"]:
				_log_action(
					service_folder, now_utc=now, window_key=window_key, phase="fallback",
					dry_run=dry_run, open_position_count=len(positions), candidate=None,
					result="suppressed",
					message="A position was profitable or closed during the five-minute window",
				)
				state["fallback_evaluated"] = True
			elif not observed_minutes:
				_log_action(
					service_folder, now_utc=now, window_key=window_key, phase="fallback",
					dry_run=dry_run, open_position_count=len(positions), candidate=None,
					result="incomplete_window",
					message="Loss fallback skipped because no position scan ran before minute 30",
				)
				state["fallback_evaluated"] = True
			else:
				_run_loss_fallback(
					service_folder, state, now_utc=now, window_key=window_key,
					dry_run=dry_run,
				)

		state["last_evaluated_minute"] = minute_key
		_save_state(service_folder, state)
