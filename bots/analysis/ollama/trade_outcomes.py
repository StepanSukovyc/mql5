"""Track closed MT5 positions and report realized outcomes by strategy."""

from __future__ import annotations

import csv
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import MetaTrader5 as mt5


OUTCOME_HEADERS = [
	"position_id",
	"strategy_id",
	"symbol",
	"action",
	"entry_time_utc",
	"exit_time_utc",
	"holding_seconds",
	"entry_volume",
	"exit_volume",
	"gross_profit",
	"commission",
	"swap",
	"fee",
	"net_profit",
	"initial_risk_usd",
	"r_multiple",
	"exit_deal_count",
	"exit_reason",
]

STATE_FILE_NAME = "trade_outcomes_state.json"
OUTCOME_FILE_NAME = "closed_trade_outcomes.csv"
STRATEGY_MAGIC_IDS = {
	234000: "gemini_primary",
	234100: "gemini_indices",
	234200: "parallel_mean_reversion",
	234300: "reversal_pattern",
	234400: "quant_math",
	234500: "ollama_cloud_primary",
	234600: "liquidity_sweep_scalping",
}
_STATE_LOCK = threading.RLock()


def _trade_logs_dir(service_folder: Path) -> Path:
	path = service_folder / "trade_logs"
	path.mkdir(parents=True, exist_ok=True)
	return path


def _state_path(service_folder: Path) -> Path:
	return _trade_logs_dir(service_folder) / STATE_FILE_NAME


def _load_state(service_folder: Path) -> Dict[str, Any]:
	path = _state_path(service_folder)
	if not path.exists():
		return {"pending_positions": {}}
	try:
		state = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise RuntimeError(f"Could not read trade outcome state {path}: {exc}") from exc
	if not isinstance(state, dict) or not isinstance(state.get("pending_positions", {}), dict):
		raise RuntimeError(f"Invalid trade outcome state format in {path}")
	state.setdefault("pending_positions", {})
	return state


def _save_state(service_folder: Path, state: Dict[str, Any]) -> None:
	path = _state_path(service_folder)
	temporary_path = path.with_suffix(".json.tmp")
	temporary_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
	os.replace(temporary_path, path)


def _normalize_datetime(value: datetime) -> datetime:
	if value.tzinfo is None:
		return value.replace(tzinfo=timezone.utc)
	return value.astimezone(timezone.utc)


def _mt5_time_to_datetime(value: object) -> Optional[datetime]:
	try:
		timestamp = float(value)
	except (TypeError, ValueError):
		return None
	if timestamp <= 0:
		return None
	return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _deal_datetime(deal: Any) -> Optional[datetime]:
	time_msc = getattr(deal, "time_msc", None)
	if time_msc:
		try:
			return datetime.fromtimestamp(float(time_msc) / 1000.0, tz=timezone.utc)
		except (TypeError, ValueError, OSError, OverflowError):
			pass
	return _mt5_time_to_datetime(getattr(deal, "time", None))


def _position_id(position: Any) -> int:
	return int(getattr(position, "identifier", 0) or getattr(position, "ticket", 0) or 0)


def _strategy_id_from_position(position: Any) -> str:
	comment = str(getattr(position, "comment", "") or "")
	marker = comment.lower().find("ga:")
	if marker >= 0:
		strategy_id = comment[marker + 3 :].split()[0].strip()
		if strategy_id:
			return strategy_id
	magic = int(getattr(position, "magic", 0) or 0)
	return STRATEGY_MAGIC_IDS.get(magic, "manual" if magic == 0 else "unknown")


def _load_written_position_ids(path: Path) -> set[int]:
	if not path.exists():
		return set()
	try:
		with open(path, "r", newline="", encoding="utf-8") as handle:
			return {
				int(row["position_id"])
				for row in csv.DictReader(handle)
				if str(row.get("position_id", "")).strip().isdigit()
			}
	except (OSError, csv.Error, ValueError) as exc:
		raise RuntimeError(f"Could not read closed trade outcomes {path}: {exc}") from exc


def _register_open_trade(
	service_folder: Path,
	*,
	position_id: int,
	strategy_id: str,
	symbol: str,
	action: str,
	entry_time_utc: datetime,
	entry_volume: float,
	entry_price: float,
	initial_risk_usd: Optional[float],
) -> None:
	"""Persist entry metadata immediately after a successful opening deal."""
	if position_id <= 0:
		raise ValueError("position_id must be positive")
	state = _load_state(service_folder)
	pending = state["pending_positions"]
	if str(position_id) in pending:
		return
	entry_time = _normalize_datetime(entry_time_utc)
	pending[str(position_id)] = {
		"position_id": position_id,
		"strategy_id": strategy_id,
		"symbol": symbol,
		"action": action,
		"entry_time_utc": entry_time.isoformat(),
		"entry_volume": float(entry_volume),
		"entry_price": float(entry_price),
		"initial_risk_usd": (
			float(initial_risk_usd)
			if initial_risk_usd is not None and float(initial_risk_usd) > 0
			else None
		),
		"processed_deal_ids": [],
		"gross_profit": 0.0,
		"commission": 0.0,
		"swap": 0.0,
		"fee": 0.0,
		"exit_volume": 0.0,
		"exit_deal_count": 0,
	}
	_save_state(service_folder, state)


def register_open_trade(
	service_folder: Path,
	*,
	position_id: int,
	strategy_id: str,
	symbol: str,
	action: str,
	entry_time_utc: datetime,
	entry_volume: float,
	entry_price: float,
	initial_risk_usd: Optional[float],
) -> None:
	with _STATE_LOCK:
		_register_open_trade(
			service_folder,
			position_id=position_id,
			strategy_id=strategy_id,
			symbol=symbol,
			action=action,
			entry_time_utc=entry_time_utc,
			entry_volume=entry_volume,
			entry_price=entry_price,
			initial_risk_usd=initial_risk_usd,
		)


def _seed_untracked_open_positions(
	pending: Dict[str, Any],
	open_positions: tuple[Any, ...] | list[Any],
) -> None:
	for position in open_positions:
		position_id = _position_id(position)
		if position_id <= 0 or str(position_id) in pending:
			continue
		entry_time = _mt5_time_to_datetime(getattr(position, "time", None))
		if entry_time is None:
			entry_time = datetime.now(tz=timezone.utc)
		position_type = int(getattr(position, "type", -1))
		pending[str(position_id)] = {
			"position_id": position_id,
			"strategy_id": _strategy_id_from_position(position),
			"symbol": str(getattr(position, "symbol", "") or ""),
			"action": "BUY" if position_type == getattr(mt5, "POSITION_TYPE_BUY", 0) else "SELL",
			"entry_time_utc": entry_time.isoformat(),
			"entry_volume": float(getattr(position, "volume", 0.0) or 0.0),
			"entry_price": float(getattr(position, "price_open", 0.0) or 0.0),
			"initial_risk_usd": None,
			"processed_deal_ids": [],
			"gross_profit": 0.0,
			"commission": 0.0,
			"swap": 0.0,
			"fee": 0.0,
			"exit_volume": 0.0,
			"exit_deal_count": 0,
		}


def _deal_position_id(deal: Any) -> int:
	return int(getattr(deal, "position_id", 0) or 0)


def _deal_dedupe_key(deal: Any) -> str:
	ticket = int(getattr(deal, "ticket", 0) or 0)
	if ticket > 0:
		return str(ticket)
	return "|".join(
		(
			str(_deal_position_id(deal)),
			str(getattr(deal, "time_msc", getattr(deal, "time", ""))),
			str(getattr(deal, "entry", "")),
			str(_deal_value(deal, "volume")),
			str(_deal_value(deal, "profit")),
			str(_deal_value(deal, "commission")),
			str(_deal_value(deal, "swap")),
			str(_deal_value(deal, "fee")),
		)
	)


def _is_exit_deal(deal: Any) -> bool:
	entry = int(getattr(deal, "entry", -1))
	return entry in {
		getattr(mt5, "DEAL_ENTRY_OUT", 1),
		getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
		getattr(mt5, "DEAL_ENTRY_INOUT", 2),
	}


def _deal_value(deal: Any, name: str) -> float:
	try:
		return float(getattr(deal, name, 0.0) or 0.0)
	except (TypeError, ValueError):
		return 0.0


def _append_outcome(path: Path, row: Dict[str, object]) -> None:
	file_exists = path.exists()
	with open(path, "a", newline="", encoding="utf-8") as handle:
		writer = csv.DictWriter(handle, fieldnames=OUTCOME_HEADERS)
		if not file_exists:
			writer.writeheader()
		writer.writerow(row)


def _process_closed_trade_outcomes(
	service_folder: Path,
	*,
	now_utc: Optional[datetime] = None,
) -> int:
	"""Record each fully closed position once using MT5's realized deal history."""
	now = _normalize_datetime(now_utc or datetime.now(tz=timezone.utc))
	open_positions = mt5.positions_get()
	if open_positions is None:
		raise RuntimeError(f"Failed to load open positions for outcome tracking: {mt5.last_error()}")

	state = _load_state(service_folder)
	pending: Dict[str, Any] = state["pending_positions"]
	_seed_untracked_open_positions(pending, open_positions)
	if not pending:
		_save_state(service_folder, state)
		return 0

	raw_last_scan = state.get("last_history_scan_utc")
	try:
		last_scan = (
			_normalize_datetime(datetime.fromisoformat(raw_last_scan))
			if isinstance(raw_last_scan, str) and raw_last_scan
			else None
		)
	except ValueError as exc:
		raise RuntimeError(f"Invalid last MT5 deal scan timestamp: {raw_last_scan}") from exc
	if last_scan is None:
		try:
			start_times = [
				_normalize_datetime(datetime.fromisoformat(entry["entry_time_utc"]))
				for entry in pending.values()
				if isinstance(entry, dict) and isinstance(entry.get("entry_time_utc"), str)
			]
		except ValueError as exc:
			raise RuntimeError("Invalid entry timestamp in trade outcome state") from exc
		if not start_times:
			raise RuntimeError("Pending trade outcome state has no valid entry timestamps")
		start_utc = min(start_times) - timedelta(minutes=5)
	else:
		start_utc = last_scan - timedelta(minutes=5)
	deals = mt5.history_deals_get(start_utc, now)
	if deals is None:
		raise RuntimeError(f"Failed to load MT5 deal history for outcome tracking: {mt5.last_error()}")

	open_ids = {_position_id(position) for position in open_positions}
	pending_ids = {int(key) for key in pending}
	for deal in deals:
		position_id = _deal_position_id(deal)
		if position_id not in pending_ids:
			continue
		metadata = pending[str(position_id)]
		processed_deal_ids = set(metadata.get("processed_deal_ids", []))
		deal_key = _deal_dedupe_key(deal)
		if deal_key in processed_deal_ids:
			continue
		processed_deal_ids.add(deal_key)
		metadata["processed_deal_ids"] = sorted(processed_deal_ids)
		metadata["gross_profit"] = float(metadata.get("gross_profit", 0.0)) + _deal_value(deal, "profit")
		metadata["commission"] = float(metadata.get("commission", 0.0)) + _deal_value(deal, "commission")
		metadata["swap"] = float(metadata.get("swap", 0.0)) + _deal_value(deal, "swap")
		metadata["fee"] = float(metadata.get("fee", 0.0)) + _deal_value(deal, "fee")
		if _is_exit_deal(deal):
			metadata["exit_deal_count"] = int(metadata.get("exit_deal_count", 0)) + 1
			metadata["exit_volume"] = float(metadata.get("exit_volume", 0.0)) + _deal_value(deal, "volume")
			exit_time = _deal_datetime(deal)
			current_exit_time = metadata.get("exit_time_utc")
			if exit_time is not None and (
				not isinstance(current_exit_time, str)
				or exit_time >= _normalize_datetime(datetime.fromisoformat(current_exit_time))
			):
				metadata["exit_time_utc"] = exit_time.isoformat()
				metadata["exit_reason"] = int(getattr(deal, "reason", -1))

	state["last_history_scan_utc"] = now.isoformat()

	outcome_path = _trade_logs_dir(service_folder) / OUTCOME_FILE_NAME
	written_ids: Optional[set[int]] = None
	completed: list[int] = []
	for position_id in pending_ids:
		if position_id in open_ids:
			continue
		metadata = pending[str(position_id)]
		if int(metadata.get("exit_deal_count", 0)) <= 0:
			continue
		exit_time_raw = metadata.get("exit_time_utc")
		exit_time = (
			_normalize_datetime(datetime.fromisoformat(exit_time_raw))
			if isinstance(exit_time_raw, str)
			else now
		)
		entry_time = _normalize_datetime(datetime.fromisoformat(metadata["entry_time_utc"]))
		gross_profit = float(metadata.get("gross_profit", 0.0))
		commission = float(metadata.get("commission", 0.0))
		swap = float(metadata.get("swap", 0.0))
		fee = float(metadata.get("fee", 0.0))
		net_profit = gross_profit + commission + swap + fee
		risk_usd = metadata.get("initial_risk_usd")
		r_multiple = (
			net_profit / float(risk_usd)
			if risk_usd is not None and float(risk_usd) > 0
			else ""
		)
		exit_reason = metadata.get("exit_reason", "")
		row: Dict[str, object] = {
			"position_id": position_id,
			"strategy_id": metadata.get("strategy_id", "unknown"),
			"symbol": metadata.get("symbol", ""),
			"action": metadata.get("action", ""),
			"entry_time_utc": entry_time.isoformat(),
			"exit_time_utc": exit_time.isoformat(),
			"holding_seconds": max(0.0, (exit_time - entry_time).total_seconds()),
			"entry_volume": metadata.get("entry_volume", ""),
			"exit_volume": metadata.get("exit_volume", 0.0),
			"gross_profit": gross_profit,
			"commission": commission,
			"swap": swap,
			"fee": fee,
			"net_profit": net_profit,
			"initial_risk_usd": risk_usd if risk_usd is not None else "",
			"r_multiple": r_multiple,
			"exit_deal_count": metadata.get("exit_deal_count", 0),
			"exit_reason": exit_reason,
		}
		if written_ids is None:
			written_ids = _load_written_position_ids(outcome_path)
		if position_id not in written_ids:
			_append_outcome(outcome_path, row)
			written_ids.add(position_id)
		completed.append(position_id)

	for position_id in completed:
		pending.pop(str(position_id), None)
	_save_state(service_folder, state)
	return len(completed)


def process_closed_trade_outcomes(
	service_folder: Path,
	*,
	now_utc: Optional[datetime] = None,
) -> int:
	with _STATE_LOCK:
		return _process_closed_trade_outcomes(service_folder, now_utc=now_utc)
