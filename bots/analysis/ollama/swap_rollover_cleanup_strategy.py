"""Close profitable open positions during the swap rollover block window."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import MetaTrader5 as mt5

from swap_rollover import get_swap_block_window
from trade_execution import close_position_by_ticket


DEFAULT_ENABLED = True
DEFAULT_DRY_RUN = True
FEE_PER_001_LOT = 0.10
MIN_NET_PROFIT_TO_CLOSE = 0.10
ROLLOVER_CLEANUP_LOG_HEADERS = [
	"timestamp",
	"dry_run",
	"balance",
	"rollover_source",
	"window_start_utc",
	"window_end_utc",
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
class SwapRolloverCleanupCandidate:
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
class SwapRolloverCleanupMetrics:
	balance: float
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
	return _to_bool(_load_dotenv_value("SWAP_ROLLOVER_CLEANUP_STRATEGY_ENABLED"), default=DEFAULT_ENABLED)


def _get_strategy_dry_run() -> bool:
	return _to_bool(_load_dotenv_value("SWAP_ROLLOVER_CLEANUP_STRATEGY_DRY_RUN"), default=DEFAULT_DRY_RUN)


def _get_service_folder() -> Optional[Path]:
	raw_value = _load_dotenv_value("SERVICE_DEST_FOLDER")
	if not raw_value:
		return None
	return Path(raw_value)


def _get_position_fee(volume: float) -> float:
	return round((float(volume) / 0.01) * FEE_PER_001_LOT, 2)


def calculate_swap_rollover_cleanup_metrics(
	balance: float,
	position_volume: float,
	profit: float,
	swap: float,
) -> SwapRolloverCleanupMetrics:
	"""Return all derived values used by the rollover-window cleanup strategy."""
	fee = _get_position_fee(position_volume)
	net_profit = round(float(profit) + float(swap) - fee, 2)
	target_profit = MIN_NET_PROFIT_TO_CLOSE
	return SwapRolloverCleanupMetrics(
		balance=float(balance),
		position_volume=float(position_volume),
		profit=float(profit),
		swap=float(swap),
		fee=fee,
		net_profit=net_profit,
		target_profit=target_profit,
		eligible=net_profit >= target_profit,
	)


def _find_candidates(balance: float) -> list[SwapRolloverCleanupCandidate]:
	positions = mt5.positions_get()
	if positions is None:
		raise RuntimeError(f"Failed to get open positions: {mt5.last_error()}")

	candidates: list[SwapRolloverCleanupCandidate] = []
	for position in positions:
		volume = float(getattr(position, "volume", 0.0) or 0.0)
		profit = float(getattr(position, "profit", 0.0) or 0.0)
		if volume <= 0 or profit <= 0:
			continue

		metrics = calculate_swap_rollover_cleanup_metrics(
			balance=balance,
			position_volume=volume,
			profit=profit,
			swap=float(getattr(position, "swap", 0.0) or 0.0),
		)
		if not metrics.eligible:
			continue

		candidates.append(
			SwapRolloverCleanupCandidate(
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
	rollover_source: str,
	window_start_utc: datetime,
	window_end_utc: datetime,
	candidate: Optional[SwapRolloverCleanupCandidate],
	closed: bool,
	message: str,
) -> None:
	if service_folder is None:
		return

	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	log_file = log_dir / "swap_rollover_cleanup.csv"
	file_exists = log_file.exists()

	with open(log_file, "a", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		if not file_exists:
			writer.writerow(ROLLOVER_CLEANUP_LOG_HEADERS)

		writer.writerow(
			[
				now_utc.isoformat(),
				str(_get_strategy_dry_run()),
				f"{balance:.2f}",
				rollover_source,
				window_start_utc.isoformat(),
				window_end_utc.isoformat(),
				candidate.symbol if candidate else "",
				candidate.ticket if candidate else "",
				f"{candidate.volume:.2f}" if candidate else "",
				f"{candidate.profit:.2f}" if candidate else "",
				f"{candidate.swap:.2f}" if candidate else "",
				f"{candidate.fee:.2f}" if candidate else "",
				f"{candidate.net_profit:.2f}" if candidate else "",
				f"{candidate.target_profit:.2f}" if candidate else "",
				str(closed),
				message,
			]
		)


def run_swap_rollover_cleanup_strategy_if_due(account_info: Optional[dict[str, Any]] = None) -> None:
	"""Evaluate the swap rollover cleanup strategy at most once per minute."""
	global _LAST_EVALUATED_MINUTE_KEY

	if not _get_strategy_enabled():
		return

	now_utc = datetime.now(tz=timezone.utc)
	minute_key = now_utc.strftime("%Y%m%d%H%M")
	if _LAST_EVALUATED_MINUTE_KEY == minute_key:
		return

	_LAST_EVALUATED_MINUTE_KEY = minute_key
	service_folder = _get_service_folder()
	dry_run = _get_strategy_dry_run()

	try:
		window = get_swap_block_window(now_utc=now_utc)
		if not window.contains(now_utc):
			return

		if account_info is None:
			account = mt5.account_info()
			if account is None:
				raise RuntimeError(f"Failed to get account info: {mt5.last_error()}")
			balance = float(account.balance)
		else:
			balance = float(account_info.get("raw_balance", account_info.get("balance", 0.0)))

		candidates = _find_candidates(balance)
		if not candidates:
			return

		print("\n💰 Swap rollover cleanup strategy")
		print(f"   Time (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
		print(
			"   Swap block window (UTC): "
			f"{window.start_utc.strftime('%H:%M')} - {window.end_utc.strftime('%H:%M')} "
			f"around rollover {window.rollover_at_utc.strftime('%H:%M')}"
		)
		print(f"   Rollover source: {window.rollover_time.source}")
		print(f"   Dry run: {'ON' if dry_run else 'OFF'}")
		print(f"   Balance B: {balance:.2f}")
		print(f"   Minimum net profit: {MIN_NET_PROFIT_TO_CLOSE:.2f}")
		print(f"   Eligible positions: {len(candidates)}")

		for candidate in candidates:
			print(
				f"   Candidate: {candidate.symbol} ticket={candidate.ticket} "
				f"net_profit={candidate.net_profit:.2f} "
				f"threshold={candidate.target_profit:.2f} volume={candidate.volume:.2f}"
			)

			if dry_run:
				closed = False
				message = "DRY-RUN: profitable position would be closed before swap rollover"
			else:
				closed = close_position_by_ticket(
					position_ticket=candidate.ticket,
					symbol=candidate.symbol,
					position_type=candidate.position_type,
					volume=candidate.volume,
					comment="Swap rollover cleanup",
				)
				message = "Position closed" if closed else "Position close failed"

			print(f"   {message}")
			_log_cleanup_action(
				service_folder=service_folder,
				now_utc=now_utc,
				balance=balance,
				rollover_source=window.rollover_time.source,
				window_start_utc=window.start_utc,
				window_end_utc=window.end_utc,
				candidate=candidate,
				closed=closed,
				message=message,
			)
	except Exception as exc:  # pylint: disable=broad-except
		print(f"⚠️  Swap rollover cleanup strategy failed: {exc}")
		window = get_swap_block_window(now_utc=now_utc)
		_log_cleanup_action(
			service_folder=service_folder,
			now_utc=now_utc,
			balance=0.0,
			rollover_source=window.rollover_time.source,
			window_start_utc=window.start_utc,
			window_end_utc=window.end_utc,
			candidate=None,
			closed=False,
			message=f"Strategy failed: {exc}",
		)