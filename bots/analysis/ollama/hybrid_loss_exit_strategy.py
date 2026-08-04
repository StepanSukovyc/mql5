"""Deterministic internal exit management for eligible losing positions."""

from __future__ import annotations

import csv
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5

from market_data import exponential_moving_average
from strategy_context import (
	get_chaotic_strategy_context,
	get_index_strategy_context,
	get_ollama_cloud_strategy_context,
	get_parallel_strategy_context,
	get_primary_strategy_context,
	get_quant_strategy_context,
	get_reversal_strategy_context,
	get_scalping_strategy_context,
	position_belongs_to_strategy,
)
from swap_rollover import get_swap_block_window
from trade_execution import close_position_by_ticket


FEE_PER_001_LOT = 0.10
CSV_HEADERS = [
	"timestamp_utc", "evaluation_id", "ticket", "symbol", "magic", "opened_at_utc",
	"opened_at_prague", "calendar_age_hours", "market_closed_hours", "active_market_age_hours",
	"age_band", "net_profit", "legacy", "eligible", "decision", "close_reason", "dry_run",
	"close_attempted", "closed", "mt5_message",
]
OUTCOME_STATE_FILE = "hybrid_loss_exit_outcome_state.json"
_LAST_EVALUATED_AT: Optional[datetime] = None


@dataclass
class Candidate:
	ticket: int
	symbol: str
	position_type: int
	volume: float
	opened_at: datetime
	magic: int
	profit: float
	swap: float
	net_profit: float
	calendar_age_hours: float
	market_closed_hours: float
	active_market_age_hours: float
	age_band: str
	legacy: bool
	eligible: bool
	decision: str
	reason_codes: list[str]


def _get_bool(name: str, default: bool) -> bool:
	raw = os.getenv(name)
	return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_int(name: str, default: int, minimum: int = 0) -> int:
	try:
		value = int(os.getenv(name, str(default)))
		return value if value >= minimum else default
	except ValueError:
		return default


def _get_float(name: str, default: float, minimum: float = 0.0) -> float:
	try:
		value = float(os.getenv(name, str(default)))
		return value if value >= minimum else default
	except ValueError:
		return default


def _get_timezone() -> ZoneInfo:
	name = os.getenv("HYBRID_EXIT_TIMEZONE", "Europe/Prague")
	try:
		return ZoneInfo(name)
	except Exception:
		return ZoneInfo("Europe/Prague")


def _get_analysis_start_utc() -> datetime:
	raw = os.getenv("HYBRID_EXIT_ANALYSIS_START_DATE")
	if not raw:
		raise ValueError("HYBRID_EXIT_ANALYSIS_START_DATE is required")
	try:
		start_date = date.fromisoformat(raw)
	except ValueError as exc:
		raise ValueError("HYBRID_EXIT_ANALYSIS_START_DATE must be YYYY-MM-DD") from exc
	return datetime.combine(start_date, time.min, tzinfo=_get_timezone()).astimezone(timezone.utc)


def _get_service_folder() -> Optional[Path]:
	raw = os.getenv("SERVICE_DEST_FOLDER", "").strip()
	return Path(raw) if raw else None


def _fee(volume: float) -> float:
	return round((volume / 0.01) * FEE_PER_001_LOT, 2)


def _market_close_interval(local_day: date, tz: ZoneInfo) -> Optional[tuple[datetime, datetime]]:
	if local_day.weekday() != _get_int("HYBRID_EXIT_MARKET_CLOSE_WEEKDAY", 4, 0):
		return None
	close = datetime.combine(
		local_day,
		time(_get_int("HYBRID_EXIT_MARKET_CLOSE_HOUR", 23, 0) % 24, _get_int("HYBRID_EXIT_MARKET_CLOSE_MINUTE", 0, 0) % 60),
		tzinfo=tz,
	)
	open_day = local_day + timedelta(days=(_get_int("HYBRID_EXIT_MARKET_OPEN_WEEKDAY", 6, 0) - local_day.weekday()) % 7)
	open_at = datetime.combine(
		open_day,
		time(_get_int("HYBRID_EXIT_MARKET_OPEN_HOUR", 23, 0) % 24, _get_int("HYBRID_EXIT_MARKET_OPEN_MINUTE", 0, 0) % 60),
		tzinfo=tz,
	)
	return close, open_at


def calculate_active_market_age(opened_at: datetime, now_utc: datetime) -> tuple[float, float, float]:
	"""Return calendar, closed-market, and active-market hours for an FX weekly session."""
	start = opened_at.astimezone(timezone.utc)
	end = now_utc.astimezone(timezone.utc)
	calendar_hours = max((end - start).total_seconds() / 3600.0, 0.0)
	closed_hours = 0.0
	tz = _get_timezone()
	local_day = start.astimezone(tz).date() - timedelta(days=7)
	last_day = end.astimezone(tz).date()
	while local_day <= last_day:
		interval = _market_close_interval(local_day, tz)
		if interval is not None:
			closed_start = interval[0].astimezone(timezone.utc)
			closed_end = interval[1].astimezone(timezone.utc)
			overlap_start = max(start, closed_start)
			overlap_end = min(end, closed_end)
			if overlap_end > overlap_start:
				closed_hours += (overlap_end - overlap_start).total_seconds() / 3600.0
		local_day += timedelta(days=1)
	return round(calendar_hours, 2), round(closed_hours, 2), round(max(calendar_hours - closed_hours, 0.0), 2)


def _age_band(active_age_hours: float) -> str:
	if active_age_hours < _get_int("HYBRID_EXIT_YOUNG_HOURS", 24, 1):
		return "young"
	if active_age_hours < _get_int("HYBRID_EXIT_OLD_HOURS", 72, 1):
		return "middle"
	return "old"


def _belongs_to_managed_strategy(position: Any) -> bool:
	contexts = [
		get_primary_strategy_context(), get_parallel_strategy_context(), get_reversal_strategy_context(),
		get_quant_strategy_context(), get_index_strategy_context(), get_ollama_cloud_strategy_context(),
		get_scalping_strategy_context(), get_chaotic_strategy_context(),
	]
	if any(position_belongs_to_strategy(position, context) for context in contexts):
		return True
	return _get_bool("HYBRID_EXIT_MANAGE_MANUAL_POSITIONS", False) and int(getattr(position, "magic", 0) or 0) == 0


def _closed_rates(symbol: str, timeframe: int, count: int = 260) -> list[Any]:
	"""MT5 position 1 deliberately excludes the currently open candle."""
	rates = mt5.copy_rates_from_pos(symbol, timeframe, 1, count)
	return list(rates) if rates is not None else []


def _market_reason_codes(position: Any) -> tuple[list[str], dict[str, Any]]:
	h1 = _closed_rates(str(position.symbol), mt5.TIMEFRAME_H1)
	h4 = _closed_rates(str(position.symbol), mt5.TIMEFRAME_H4)
	if len(h1) < 21 or len(h4) < 21:
		return ["missing_market_data"], {}
	h1_closes = [float(row["close"]) for row in h1]
	h4_closes = [float(row["close"]) for row in h4]
	h1_ema20 = exponential_moving_average(h1_closes, 20)[-1]
	h4_ema20 = exponential_moving_average(h4_closes, 20)[-1]
	if h1_ema20 is None or h4_ema20 is None:
		return ["missing_indicator_data"], {}
	side_is_buy = int(position.type) == mt5.POSITION_TYPE_BUY
	last_h1 = h1[-1]
	prior_h1 = h1[-5:-1]
	reasons: list[str] = []
	if side_is_buy:
		if float(last_h1["low"]) < min(float(row["low"]) for row in prior_h1):
			reasons.append("h1_new_low")
		if h1_closes[-1] < h1_ema20:
			reasons.append("h1_close_below_ema20")
		if h4_closes[-1] < h4_ema20:
			reasons.append("h4_downtrend")
	else:
		if float(last_h1["high"]) > max(float(row["high"]) for row in prior_h1):
			reasons.append("h1_new_high")
		if h1_closes[-1] > h1_ema20:
			reasons.append("h1_close_above_ema20")
		if h4_closes[-1] > h4_ema20:
			reasons.append("h4_uptrend")
	return reasons, {
		"h1_close": h1_closes[-1], "h1_ema20": h1_ema20, "h4_close": h4_closes[-1],
		"h4_ema20": h4_ema20, "h1_bar_time_utc": datetime.fromtimestamp(int(last_h1["time"]), timezone.utc).isoformat(),
	}


def _build_candidate(position: Any, now_utc: datetime, start_utc: datetime) -> Candidate:
	opened_at = datetime.fromtimestamp(int(getattr(position, "time", 0) or 0), timezone.utc)
	volume = float(getattr(position, "volume", 0.0) or 0.0)
	net_profit = round(float(getattr(position, "profit", 0.0) or 0.0) + float(getattr(position, "swap", 0.0) or 0.0) - _fee(volume), 2)
	calendar_age, closed_hours, active_age = calculate_active_market_age(opened_at, now_utc)
	legacy = opened_at < start_utc
	eligible = not legacy and net_profit < 0 and _belongs_to_managed_strategy(position)
	decision = "legacy_excluded" if legacy else "ineligible"
	if eligible:
		decision = f"{_age_band(active_age)}_hold"
	return Candidate(
		ticket=int(getattr(position, "ticket", 0) or 0), symbol=str(getattr(position, "symbol", "")),
		position_type=int(getattr(position, "type", 0) or 0), volume=volume, opened_at=opened_at,
		magic=int(getattr(position, "magic", 0) or 0), profit=float(getattr(position, "profit", 0.0) or 0.0),
		swap=float(getattr(position, "swap", 0.0) or 0.0), net_profit=net_profit, calendar_age_hours=calendar_age,
		market_closed_hours=closed_hours, active_market_age_hours=active_age, age_band=_age_band(active_age),
		legacy=legacy, eligible=eligible, decision=decision, reason_codes=[],
	)


def _log_candidate(service_folder: Optional[Path], evaluation_id: str, candidate: Candidate, market: dict[str, Any], *, close_attempted: bool = False, closed: bool = False, message: str = "") -> None:
	if service_folder is None:
		return
	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	path = log_dir / "hybrid_loss_exit.csv"
	new_file = not path.exists()
	with path.open("a", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		if new_file:
			writer.writerow(CSV_HEADERS)
		writer.writerow([datetime.now(timezone.utc).isoformat(), evaluation_id, candidate.ticket, candidate.symbol, candidate.magic, candidate.opened_at.isoformat(), candidate.opened_at.astimezone(_get_timezone()).isoformat(), candidate.calendar_age_hours, candidate.market_closed_hours, candidate.active_market_age_hours, candidate.age_band, candidate.net_profit, candidate.legacy, candidate.eligible, candidate.decision, ",".join(candidate.reason_codes), _get_bool("HYBRID_EXIT_DRY_RUN", True), close_attempted, closed, message])
	with (log_dir / "hybrid_loss_exit_events.jsonl").open("a", encoding="utf-8") as handle:
		handle.write(json.dumps({"timestamp_utc": datetime.now(timezone.utc).isoformat(), "evaluation_id": evaluation_id, "candidate": asdict(candidate), "market": market, "close_attempted": close_attempted, "closed": closed, "message": message}, ensure_ascii=False, default=str) + "\n")


def _outcome_state_path(service_folder: Optional[Path]) -> Optional[Path]:
	if service_folder is None:
		return None
	path = service_folder / "trade_logs" / OUTCOME_STATE_FILE
	path.parent.mkdir(parents=True, exist_ok=True)
	return path


def _load_outcome_state(service_folder: Optional[Path]) -> dict[str, dict[str, Any]]:
	path = _outcome_state_path(service_folder)
	if path is None or not path.exists():
		return {}
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
		return payload if isinstance(payload, dict) else {}
	except (OSError, json.JSONDecodeError):
		return {}


def _save_outcome_state(service_folder: Optional[Path], state: dict[str, dict[str, Any]]) -> None:
	path = _outcome_state_path(service_folder)
	if path is not None:
		path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _outcome_horizons() -> list[int]:
	values: list[int] = []
	for raw in os.getenv("HYBRID_EXIT_OUTCOME_HORIZONS_HOURS", "24,48").split(","):
		try:
			value = int(raw.strip())
			if value > 0:
				values.append(value)
		except ValueError:
			continue
	return sorted(set(values)) or [24, 48]


def _process_due_outcomes(service_folder: Optional[Path], now_utc: datetime, positions: tuple[Any, ...]) -> None:
	state = _load_outcome_state(service_folder)
	if not state or service_folder is None:
		return
	by_ticket = {int(getattr(position, "ticket", 0) or 0): position for position in positions}
	log_path = service_folder / "trade_logs" / "hybrid_loss_exit_outcomes.jsonl"
	changed = False
	with log_path.open("a", encoding="utf-8") as handle:
		for key, entry in list(state.items()):
			for horizon in entry.get("horizons", []):
				if horizon.get("logged") or now_utc < datetime.fromisoformat(horizon["due_at_utc"]):
					continue
				position = by_ticket.get(int(entry["ticket"]))
				if position is None:
					outcome = {"outcome_status": "position_closed_before_horizon"}
				else:
					volume = float(getattr(position, "volume", 0.0) or 0.0)
					current_net = round(float(getattr(position, "profit", 0.0) or 0.0) + float(getattr(position, "swap", 0.0) or 0.0) - _fee(volume), 2)
					outcome = {"outcome_status": "position_open", "net_profit": current_net, "net_profit_change": round(current_net - float(entry["net_profit_at_decision"]), 2)}
				handle.write(json.dumps({"timestamp_utc": now_utc.isoformat(), "evaluation_id": entry["evaluation_id"], "ticket": entry["ticket"], "symbol": entry["symbol"], "decision": entry["decision"], "horizon_hours": horizon["hours"], **outcome}, ensure_ascii=False) + "\n")
				horizon["logged"] = True
				changed = True
			if all(horizon.get("logged") for horizon in entry.get("horizons", [])):
				del state[key]
				changed = True
	if changed:
		_save_outcome_state(service_folder, state)


def _schedule_outcome(state: dict[str, dict[str, Any]], evaluation_id: str, candidate: Candidate, now_utc: datetime) -> None:
	if candidate.decision not in {"exit_momentum", "old_hold_momentum_ok", "old_hold_near_break_even"}:
		return
	key = f"{candidate.ticket}:{candidate.decision}"
	if key in state:
		return
	state[key] = {
		"evaluation_id": evaluation_id, "ticket": candidate.ticket, "symbol": candidate.symbol,
		"decision": candidate.decision, "net_profit_at_decision": candidate.net_profit,
		"horizons": [{"hours": hours, "due_at_utc": (now_utc + timedelta(hours=hours)).isoformat(), "logged": False} for hours in _outcome_horizons()],
	}


def _get_equity(account_info: Optional[dict[str, Any]]) -> float:
	if account_info is not None:
		return float(account_info.get("equity", account_info.get("raw_balance", account_info.get("balance", 0.0))) or 0.0)
	account = mt5.account_info()
	return float(getattr(account, "equity", 0.0) or 0.0) if account is not None else 0.0


def run_hybrid_loss_exit_strategy_if_due(account_info: Optional[dict[str, Any]] = None) -> None:
	"""Evaluate internal loss exits. Live close remains disabled by default."""
	global _LAST_EVALUATED_AT
	if not _get_bool("HYBRID_EXIT_ENABLED", False):
		return
	now_utc = datetime.now(timezone.utc)
	interval = timedelta(minutes=_get_int("HYBRID_EXIT_CHECK_INTERVAL_MINUTES", 15, 1))
	if _LAST_EVALUATED_AT is not None and now_utc - _LAST_EVALUATED_AT < interval:
		return
	_LAST_EVALUATED_AT = now_utc
	start_utc = _get_analysis_start_utc()
	service_folder = _get_service_folder()
	evaluation_id = f"{now_utc.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
	window = get_swap_block_window(now_utc=now_utc)
	positions = mt5.positions_get()
	if positions is None:
		raise RuntimeError(f"positions_get failed: {mt5.last_error()}")
	positions = tuple(positions)
	_process_due_outcomes(service_folder, now_utc, positions)
	outcome_state = _load_outcome_state(service_folder)
	equity = _get_equity(account_info)
	emergency_loss = round(equity * _get_float("HYBRID_EXIT_EMERGENCY_ACCOUNT_LOSS_PERCENT", 1.0) / 100.0, 2)
	closed_count = 0
	for position in positions:
		candidate = _build_candidate(position, now_utc, start_utc)
		market: dict[str, Any] = {}
		if candidate.eligible and emergency_loss > 0 and -candidate.net_profit >= emergency_loss:
			candidate.decision = "exit_emergency"
			candidate.reason_codes = ["emergency_account_loss_limit"]
		elif candidate.eligible and window.contains(now_utc):
			candidate.decision = "skipped_rollover"
		elif candidate.eligible and candidate.age_band == "old":
			threshold = -max(2 * _fee(candidate.volume), _get_float("HYBRID_EXIT_BREAK_EVEN_BUFFER_USD", 0.20))
			if candidate.net_profit >= threshold:
				candidate.decision = "old_hold_near_break_even"
			else:
				candidate.reason_codes, market = _market_reason_codes(position)
				if len(candidate.reason_codes) >= 2 and "missing_market_data" not in candidate.reason_codes:
					candidate.decision = "exit_momentum"
				else:
					candidate.decision = "old_hold_momentum_ok"
		if candidate.decision in {"exit_momentum", "exit_emergency"} and closed_count >= _get_int("HYBRID_EXIT_MAX_CLOSES_PER_CYCLE", 1, 1):
			candidate.decision = "skipped_cycle_limit"
		if candidate.decision not in {"exit_momentum", "exit_emergency"}:
			_schedule_outcome(outcome_state, evaluation_id, candidate, now_utc)
			_log_candidate(service_folder, evaluation_id, candidate, market)
			continue
		_schedule_outcome(outcome_state, evaluation_id, candidate, now_utc)
		if _get_bool("HYBRID_EXIT_DRY_RUN", True):
			_log_candidate(service_folder, evaluation_id, candidate, market, message="DRY-RUN: close would be sent")
			continue
		# Revalidate immediately before execution to avoid closing stale snapshots.
		current = next((item for item in (mt5.positions_get() or ()) if int(getattr(item, "ticket", 0)) == candidate.ticket), None)
		if current is None or float(getattr(current, "profit", 0.0) or 0.0) >= 0:
			candidate.decision = "revalidation_failed"
			_log_candidate(service_folder, evaluation_id, candidate, market, message="position missing or no longer losing")
			continue
		closed = close_position_by_ticket(candidate.ticket, candidate.symbol, candidate.position_type, candidate.volume, comment="hybrid_loss_exit", magic=candidate.magic)
		closed_count += int(closed)
		_log_candidate(service_folder, evaluation_id, candidate, market, close_attempted=True, closed=closed, message="position closed" if closed else "close failed")
	_save_outcome_state(service_folder, outcome_state)