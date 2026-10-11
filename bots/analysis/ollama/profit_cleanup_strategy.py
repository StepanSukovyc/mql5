"""Daily strategy for closing positions with sufficient net profit."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Optional

import MetaTrader5 as mt5

from swap_rollover import PRAGUE_TZ, get_swap_block_window
from trade_execution import close_position_by_ticket


DEFAULT_ENABLED = True
DEFAULT_DRY_RUN = True
ACCOUNT_BALANCE_STEP = 500.0
MIN_NET_PROFIT_TO_CLOSE = 0.50
FEE_PER_001_LOT = 0.10
DAILY_RUN_START = time(hour=22)
DAILY_RUN_END = time(hour=22, minute=10)
PROFIT_CLEANUP_LOG_HEADERS = [
	"timestamp",
	"dry_run",
	"balance",
	"reference_volume",
	"symbol",
	"ticket",
	"position_volume",
	"profit",
	"swap",
	"fee",
	"net_profit",
	"target_profit",
	"closed",
	"message",
]

_LAST_EVALUATED_MINUTE_KEY: Optional[str] = None


@dataclass
class ProfitCleanupCandidate:
	ticket: int
	symbol: str
	position_type: int
	volume: float
	profit: float
	swap: float
	fee: float
	net_profit: float
	target_profit: float


@dataclass
class ProfitCleanupMetrics:
	balance: float
	reference_volume: float
	position_volume: float
	profit: float
	swap: float
	fee: float
	net_profit: float
	target_profit: float
	eligible: bool


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


def _get_strategy_enabled() -> bool:
	value = os.getenv("PROFIT_CLEANUP_STRATEGY_ENABLED") or _load_dotenv_value("PROFIT_CLEANUP_STRATEGY_ENABLED")
	return _to_bool(value, default=DEFAULT_ENABLED)


def _get_strategy_dry_run() -> bool:
	value = os.getenv("PROFIT_CLEANUP_STRATEGY_DRY_RUN") or _load_dotenv_value("PROFIT_CLEANUP_STRATEGY_DRY_RUN")
	return _to_bool(value, default=DEFAULT_DRY_RUN)


def _get_service_folder() -> Optional[Path]:
	raw_value = os.getenv("SERVICE_DEST_FOLDER") or _load_dotenv_value("SERVICE_DEST_FOLDER")
	if not raw_value:
		return None
	return Path(raw_value)


def _get_position_fee(volume: float) -> float:
	return round((float(volume) / 0.01) * FEE_PER_001_LOT, 2)


def _get_reference_volume(balance: float) -> float:
	reference_volume = ((int(float(balance) / ACCOUNT_BALANCE_STEP) + 1) * 0.01)
	if reference_volume <= 0:
		return 0.01
	return round(reference_volume, 2)


def calculate_profit_cleanup_metrics(balance: float, position_volume: float, profit: float, swap: float) -> ProfitCleanupMetrics:
	"""Return net profit and eligibility against the fixed minimum close profit."""
	fee = _get_position_fee(position_volume)
	net_profit = round(float(profit) + float(swap) - fee, 2)
	target_profit = MIN_NET_PROFIT_TO_CLOSE
	return ProfitCleanupMetrics(
		balance=float(balance),
		reference_volume=_get_reference_volume(balance),
		position_volume=float(position_volume),
		profit=float(profit),
		swap=float(swap),
		fee=fee,
		net_profit=net_profit,
		target_profit=target_profit,
		eligible=net_profit >= target_profit,
	)


def _find_candidates(balance: float) -> list[ProfitCleanupCandidate]:
	positions = mt5.positions_get()
	if positions is None:
		raise RuntimeError(f"Failed to get open positions: {mt5.last_error()}")

	candidates: list[ProfitCleanupCandidate] = []
	for position in positions:
		volume = float(getattr(position, "volume", 0.0) or 0.0)
		if volume <= 0:
			continue

		metrics = calculate_profit_cleanup_metrics(
			balance=balance,
			position_volume=volume,
			profit=float(getattr(position, "profit", 0.0) or 0.0),
			swap=float(getattr(position, "swap", 0.0) or 0.0),
		)
		if not metrics.eligible:
			continue

		candidates.append(
			ProfitCleanupCandidate(
				ticket=int(position.ticket),
				symbol=str(position.symbol),
				position_type=int(position.type),
				volume=volume,
				profit=metrics.profit,
				swap=metrics.swap,
				fee=metrics.fee,
				net_profit=metrics.net_profit,
				target_profit=metrics.target_profit,
			)
		)

	return sorted(candidates, key=lambda candidate: candidate.net_profit, reverse=True)


def _log_cleanup_action(
	*,
	service_folder: Optional[Path],
	now_utc: datetime,
	balance: float,
	reference_volume: float,
	candidate: Optional[ProfitCleanupCandidate],
	closed: bool,
	message: str,
) -> None:
	if service_folder is None:
		return

	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	log_file = log_dir / "profit_cleanup.csv"
	file_exists = log_file.exists()

	with open(log_file, "a", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		if not file_exists:
			writer.writerow(PROFIT_CLEANUP_LOG_HEADERS)

		writer.writerow(
			[
				now_utc.isoformat(),
				str(_get_strategy_dry_run()),
				f"{balance:.2f}",
				f"{reference_volume:.2f}",
				candidate.symbol if candidate else "",
				candidate.ticket if candidate else "",
				f"{candidate.volume:.2f}" if candidate else "",
				f"{candidate.profit:.2f}" if candidate else "",
				f"{candidate.swap:.2f}" if candidate else "",
				f"{candidate.fee:.2f}" if candidate else "",
				f"{candidate.net_profit:.2f}" if candidate else "",
				f"{candidate.target_profit:.4f}" if candidate else "",
				str(closed),
				message,
			]
		)


def run_profit_cleanup_strategy_if_due(
	account_info: Optional[dict[str, Any]] = None,
	*,
	now_utc: Optional[datetime] = None,
) -> None:
	"""Evaluate profitable positions once per minute during the Prague 22:00 window."""
	global _LAST_EVALUATED_MINUTE_KEY

	if not _get_strategy_enabled():
		return

	now_utc = now_utc or datetime.now(tz=timezone.utc)
	if now_utc.tzinfo is None:
		now_utc = now_utc.replace(tzinfo=timezone.utc)
	else:
		now_utc = now_utc.astimezone(timezone.utc)
	minute_key = now_utc.strftime("%Y%m%d%H%M")
	if _LAST_EVALUATED_MINUTE_KEY == minute_key:
		return

	_LAST_EVALUATED_MINUTE_KEY = minute_key
	now_prague = now_utc.astimezone(PRAGUE_TZ)
	if not DAILY_RUN_START <= now_prague.time().replace(tzinfo=None) < DAILY_RUN_END:
		return

	service_folder = _get_service_folder()
	dry_run = _get_strategy_dry_run()

	try:
		window = get_swap_block_window(now_utc=now_utc)
		if window.contains(now_utc):
			return

		if account_info is None:
			account = mt5.account_info()
			if account is None:
				raise RuntimeError(f"Failed to get account info: {mt5.last_error()}")
			balance = float(account.balance)
		else:
			balance = float(account_info.get("raw_balance", account_info.get("balance", 0.0)))

		reference_volume = _get_reference_volume(balance)
		candidates = _find_candidates(balance)
		if not candidates:
			_log_cleanup_action(
				service_folder=service_folder,
				now_utc=now_utc,
				balance=balance,
				reference_volume=reference_volume,
				candidate=None,
				closed=False,
				message="No eligible profitable positions in scheduled cleanup window",
			)
			return

		print("\n💰 Scheduled profit cleanup strategy")
		print(f"   Time (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
		print(f"   Dry run: {'ON' if dry_run else 'OFF'}")
		print(f"   Balance B: {balance:.2f}")
		print(f"   Reference volume: {reference_volume:.2f}")
		print(f"   Eligible positions: {len(candidates)}")

		for candidate in candidates:
			print(
				f"   Candidate: {candidate.symbol} ticket={candidate.ticket} "
				f"net_profit={candidate.net_profit:.2f} "
				f"target={candidate.target_profit:.4f} volume={candidate.volume:.2f}"
			)

			if dry_run:
				closed = False
				message = "DRY-RUN: position matches profit cleanup rules and would be closed"
			else:
				closed = close_position_by_ticket(
					position_ticket=candidate.ticket,
					symbol=candidate.symbol,
					position_type=candidate.position_type,
					volume=candidate.volume,
					comment="Profit cleanup strategy",
				)
				message = "Position closed" if closed else "Position close failed"

			print(f"   {message}")
			_log_cleanup_action(
				service_folder=service_folder,
				now_utc=now_utc,
				balance=balance,
				reference_volume=reference_volume,
				candidate=candidate,
				closed=closed,
				message=message,
			)
	except Exception as exc:  # pylint: disable=broad-except
		print(f"⚠️  Minute profit cleanup strategy failed: {exc}")
		_log_cleanup_action(
			service_folder=service_folder,
			now_utc=now_utc,
			balance=0.0,
			reference_volume=0.0,
			candidate=None,
			closed=False,
			message=f"Strategy failed: {exc}",
		)