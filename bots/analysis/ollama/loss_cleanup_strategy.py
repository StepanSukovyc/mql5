"""Hourly cleanup strategy for closing selected stale losing positions."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import MetaTrader5 as mt5
import pytz

from trade_execution import close_position_by_ticket


PRAGUE_TZ = pytz.timezone("Europe/Prague")
DEFAULT_ENABLED = True
DEFAULT_RUN_MINUTE = 45
DEFAULT_DRY_RUN = True
ACCOUNT_BALANCE_BUFFER_RATIO = 0.01
MIN_POSITION_AGE = timedelta(days=7)
FEE_PER_001_LOT = 0.10

_LAST_EVALUATED_HOUR_KEY: Optional[str] = None


@dataclass
class CleanupCandidate:
	ticket: int
	symbol: str
	position_type: int
	volume: float
	opened_at: datetime
	profit: float
	swap: float
	fee: float
	loss_amount: float


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
	return _to_bool(_load_dotenv_value("LOSS_CLEANUP_STRATEGY_ENABLED"), default=DEFAULT_ENABLED)


def _get_strategy_dry_run() -> bool:
	return _to_bool(_load_dotenv_value("LOSS_CLEANUP_STRATEGY_DRY_RUN"), default=DEFAULT_DRY_RUN)


def _get_strategy_run_minute() -> int:
	raw_value = _load_dotenv_value("LOSS_CLEANUP_STRATEGY_MINUTE")
	if raw_value is None:
		return DEFAULT_RUN_MINUTE

	try:
		minute = int(raw_value)
		if 0 <= minute <= 59:
			return minute
	except ValueError:
		pass

	print(
		f"⚠️  Nevalidni LOSS_CLEANUP_STRATEGY_MINUTE='{raw_value}', "
		f"pouzivam {DEFAULT_RUN_MINUTE}"
	)
	return DEFAULT_RUN_MINUTE


def _get_service_folder() -> Optional[Path]:
	raw_value = _load_dotenv_value("SERVICE_DEST_FOLDER")
	if not raw_value:
		return None
	return Path(raw_value)


def _is_in_restricted_trading_hours(now_prague: datetime) -> bool:
	return now_prague.hour == 23 and 0 <= now_prague.minute < 30


def _get_position_fee(volume: float) -> float:
	return round((float(volume) / 0.01) * FEE_PER_001_LOT, 2)


def _get_day_start_utc(now_utc: datetime) -> datetime:
	now_prague = now_utc.astimezone(PRAGUE_TZ)
	day_start_prague = now_prague.replace(hour=0, minute=0, second=0, microsecond=0)
	return day_start_prague.astimezone(timezone.utc)


def _get_closing_entries() -> set[int]:
	return {
		getattr(mt5, "DEAL_ENTRY_OUT", 1),
		getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
		getattr(mt5, "DEAL_ENTRY_INOUT", 2),
	}


def _get_daily_orders(now_utc: datetime) -> tuple[datetime, tuple[Any, ...]]:
	day_start_utc = _get_day_start_utc(now_utc)
	orders = mt5.history_orders_get(day_start_utc, now_utc)
	if orders is None:
		raise RuntimeError(f"Failed to get daily order history: {mt5.last_error()}")
	return day_start_utc, tuple(orders)


def _get_daily_deals(now_utc: datetime) -> tuple[datetime, tuple[Any, ...], set[int]]:
	day_start_utc = _get_day_start_utc(now_utc)
	deals = mt5.history_deals_get(day_start_utc, now_utc)
	if deals is None:
		raise RuntimeError(f"Failed to get daily deal history: {mt5.last_error()}")
	return day_start_utc, tuple(deals), _get_closing_entries()


def _get_open_position_ids() -> set[int]:
	positions = mt5.positions_get()
	if positions is None:
		raise RuntimeError(f"Failed to get open positions: {mt5.last_error()}")
	return {int(position.ticket) for position in positions}


def _get_today_closed_position_ids(orders: tuple[Any, ...], open_position_ids: set[int]) -> set[int]:
	filled_state = getattr(mt5, "ORDER_STATE_FILLED", 4)
	closed_position_ids: set[int] = set()

	for order in orders:
		position_id = int(getattr(order, "position_id", 0) or 0)
		if position_id <= 0:
			continue
		if position_id in open_position_ids:
			continue
		if int(getattr(order, "state", -1)) != filled_state:
			continue
		closed_position_ids.add(position_id)

	return closed_position_ids


def _calculate_daily_clean_profit(
	deals: tuple[Any, ...],
	closing_entries: set[int],
	closed_position_ids: set[int],
) -> float:
	clean_profit = 0.0
	for deal in deals:
		position_id = int(getattr(deal, "position_id", 0) or 0)
		if position_id not in closed_position_ids:
			continue
		entry = getattr(deal, "entry", None)
		if entry not in closing_entries:
			continue
		clean_profit += float(getattr(deal, "profit", 0.0) or 0.0)

	return clean_profit


def _ensure_loss_cleanup_log_schema(log_file: Path) -> None:
	if not log_file.exists():
		return

	with open(log_file, "r", newline="", encoding="utf-8") as handle:
		rows = list(csv.reader(handle))

	if not rows:
		return

	headers = rows[0]
	if "daily_gross_profit" not in headers:
		return

	updated_headers = ["daily_clean_profit" if header == "daily_gross_profit" else header for header in headers]
	with open(log_file, "w", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		writer.writerow(updated_headers)
		writer.writerows(rows[1:])


def _log_daily_deals_snapshot(
	*,
	service_folder: Optional[Path],
	now_utc: datetime,
	day_start_utc: datetime,
	deals: tuple[Any, ...],
	closing_entries: set[int],
	closed_position_ids: set[int],
) -> None:
	if service_folder is None:
		return

	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	log_file = log_dir / "loss_cleanup_daily_deals.csv"
	file_exists = log_file.exists()
	headers = [
		"snapshot_timestamp",
		"day_start_utc",
		"deal_time_utc",
		"deal_time_prague",
		"deal_ticket",
		"order",
		"position_id",
		"symbol",
		"entry",
		"type",
		"volume",
		"profit",
		"swap",
		"commission",
		"fee",
		"position_closed_today",
		"included_in_daily_clean_profit",
	]

	with open(log_file, "a", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		if not file_exists:
			writer.writerow(headers)

		for deal in deals:
			deal_time_utc = datetime.fromtimestamp(int(getattr(deal, "time", 0)), tz=timezone.utc)
			deal_time_prague = deal_time_utc.astimezone(PRAGUE_TZ)
			entry = getattr(deal, "entry", None)
			position_id = int(getattr(deal, "position_id", 0) or 0)
			writer.writerow(
				[
					now_utc.isoformat(),
					day_start_utc.isoformat(),
					deal_time_utc.isoformat(),
					deal_time_prague.isoformat(),
					getattr(deal, "ticket", ""),
					getattr(deal, "order", ""),
					getattr(deal, "position_id", ""),
					getattr(deal, "symbol", ""),
					entry,
					getattr(deal, "type", ""),
					f"{float(getattr(deal, 'volume', 0.0) or 0.0):.2f}",
					f"{float(getattr(deal, 'profit', 0.0) or 0.0):.2f}",
					f"{float(getattr(deal, 'swap', 0.0) or 0.0):.2f}",
					f"{float(getattr(deal, 'commission', 0.0) or 0.0):.2f}",
					f"{_get_position_fee(float(getattr(deal, 'volume', 0.0) or 0.0)):.2f}",
					str(position_id in closed_position_ids),
					str(position_id in closed_position_ids and entry in closing_entries),
				]
			)


def _find_cleanup_candidate(z_limit: float, now_utc: datetime) -> Optional[CleanupCandidate]:
	positions = mt5.positions_get()
	if positions is None:
		raise RuntimeError(f"Failed to get open positions: {mt5.last_error()}")

	cutoff = now_utc - MIN_POSITION_AGE
	best_candidate: Optional[CleanupCandidate] = None

	for position in positions:
		opened_at = datetime.fromtimestamp(int(position.time), tz=timezone.utc)
		if opened_at > cutoff:
			continue

		profit = float(position.profit)
		swap = float(position.swap)
		fee = _get_position_fee(float(position.volume))
		effective_result = profit + swap - fee
		loss_amount = round(-effective_result, 2)

		if loss_amount <= 0:
			continue
		if loss_amount >= z_limit:
			continue

		candidate = CleanupCandidate(
			ticket=int(position.ticket),
			symbol=str(position.symbol),
			position_type=int(position.type),
			volume=float(position.volume),
			opened_at=opened_at,
			profit=profit,
			swap=swap,
			fee=fee,
			loss_amount=loss_amount,
		)

		if best_candidate is None or candidate.loss_amount > best_candidate.loss_amount:
			best_candidate = candidate

	return best_candidate


def _log_cleanup_action(
	*,
	service_folder: Optional[Path],
	now_utc: datetime,
	daily_clean_profit: float,
	balance_buffer: float,
	z_limit: float,
	candidate: Optional[CleanupCandidate],
	closed: bool,
	message: str,
) -> None:
	if service_folder is None:
		return

	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	log_file = log_dir / "loss_cleanup.csv"
	_ensure_loss_cleanup_log_schema(log_file)
	headers = [
		"timestamp",
		"dry_run",
		"daily_clean_profit",
		"balance_buffer",
		"z_limit",
		"symbol",
		"ticket",
		"volume",
		"profit",
		"swap",
		"fee",
		"loss_amount",
		"closed",
		"message",
	]
	file_exists = log_file.exists()

	with open(log_file, "a", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		if not file_exists:
			writer.writerow(headers)

		writer.writerow(
			[
				now_utc.isoformat(),
				str(_get_strategy_dry_run()),
				f"{daily_clean_profit:.2f}",
				f"{balance_buffer:.2f}",
				f"{z_limit:.2f}",
				candidate.symbol if candidate else "",
				candidate.ticket if candidate else "",
				f"{candidate.volume:.2f}" if candidate else "",
				f"{candidate.profit:.2f}" if candidate else "",
				f"{candidate.swap:.2f}" if candidate else "",
				f"{candidate.fee:.2f}" if candidate else "",
				f"{candidate.loss_amount:.2f}" if candidate else "",
				str(closed),
				message,
			]
		)


def run_loss_cleanup_strategy_if_due(account_info: Optional[dict[str, Any]] = None) -> None:
	"""Evaluate the hourly loss-cleanup strategy when its configured minute is reached."""
	global _LAST_EVALUATED_HOUR_KEY

	if not _get_strategy_enabled():
		return

	now_utc = datetime.now(tz=timezone.utc)
	now_prague = now_utc.astimezone(PRAGUE_TZ)
	run_minute = _get_strategy_run_minute()
	dry_run = _get_strategy_dry_run()
	hour_key = now_prague.strftime("%Y%m%d%H")

	if now_prague.minute != run_minute:
		return
	if _LAST_EVALUATED_HOUR_KEY == hour_key:
		return

	_LAST_EVALUATED_HOUR_KEY = hour_key
	service_folder = _get_service_folder()

	try:
		if _is_in_restricted_trading_hours(now_prague):
			message = "Restricted trading hours 23:00-23:30 Prague time, cleanup skipped"
			print("\n🧹 Hourly loss cleanup strategy")
			print(f"   Time (Prague): {now_prague.strftime('%Y-%m-%d %H:%M:%S')}")
			print(f"   {message}")
			_log_cleanup_action(
				service_folder=service_folder,
				now_utc=now_utc,
				daily_clean_profit=0.0,
				balance_buffer=0.0,
				z_limit=0.0,
				candidate=None,
				closed=False,
				message=message,
			)
			return

		if account_info is None:
			account = mt5.account_info()
			if account is None:
				raise RuntimeError(f"Failed to get account info: {mt5.last_error()}")
			raw_balance = float(account.balance)
		else:
			raw_balance = float(account_info.get("raw_balance", account_info.get("balance", 0.0)))

		_, orders = _get_daily_orders(now_utc)
		day_start_utc, deals, closing_entries = _get_daily_deals(now_utc)
		open_position_ids = _get_open_position_ids()
		closed_position_ids = _get_today_closed_position_ids(orders, open_position_ids)
		daily_clean_profit = round(_calculate_daily_clean_profit(deals, closing_entries, closed_position_ids), 2)
		balance_buffer = round(raw_balance * ACCOUNT_BALANCE_BUFFER_RATIO, 2)
		z_limit = round(daily_clean_profit - balance_buffer, 2)
		closing_deals_count = sum(
			1
			for deal in deals
			if int(getattr(deal, "position_id", 0) or 0) in closed_position_ids
			and getattr(deal, "entry", None) in closing_entries
		)

		_log_daily_deals_snapshot(
			service_folder=service_folder,
			now_utc=now_utc,
			day_start_utc=day_start_utc,
			deals=deals,
			closing_entries=closing_entries,
			closed_position_ids=closed_position_ids,
		)

		print("\n🧹 Hourly loss cleanup strategy")
		print(f"   Time (Prague): {now_prague.strftime('%Y-%m-%d %H:%M:%S')}")
		print(f"   Dry run: {'ON' if dry_run else 'OFF'}")
		print(
			f"   MT5 daily data: orders={len(orders)}, deals={len(deals)}, "
			f"closed_positions={len(closed_position_ids)}, closing_deals={closing_deals_count}"
		)
		print(f"   Daily clean profit (excl. swap/fees): {daily_clean_profit:.2f}")
		print(f"   Balance buffer (1%): {balance_buffer:.2f}")
		print(f"   Z limit: {z_limit:.2f}")

		if z_limit <= 0:
			message = "Z <= 0, no position can be closed safely"
			print(f"   {message}")
			_log_cleanup_action(
				service_folder=service_folder,
				now_utc=now_utc,
				daily_clean_profit=daily_clean_profit,
				balance_buffer=balance_buffer,
				z_limit=z_limit,
				candidate=None,
				closed=False,
				message=message,
			)
			return

		candidate = _find_cleanup_candidate(z_limit, now_utc)
		if candidate is None:
			message = "No losing position older than 7 days fits below Z"
			print(f"   {message}")
			_log_cleanup_action(
				service_folder=service_folder,
				now_utc=now_utc,
				daily_clean_profit=daily_clean_profit,
				balance_buffer=balance_buffer,
				z_limit=z_limit,
				candidate=None,
				closed=False,
				message=message,
			)
			return

		print(
			f"   Candidate: {candidate.symbol} ticket={candidate.ticket} "
			f"loss={candidate.loss_amount:.2f} "
			f"(profit={candidate.profit:.2f}, swap={candidate.swap:.2f}, fee={candidate.fee:.2f})"
		)

		if dry_run:
			closed = False
			message = "DRY-RUN: position matches rules and would be closed"
		else:
			closed = close_position_by_ticket(
				position_ticket=candidate.ticket,
				symbol=candidate.symbol,
				position_type=candidate.position_type,
				volume=candidate.volume,
				comment="Loss cleanup strategy",
			)
			message = "Position closed" if closed else "Position close failed"

		print(f"   {message}")
		_log_cleanup_action(
			service_folder=service_folder,
			now_utc=now_utc,
			daily_clean_profit=daily_clean_profit,
			balance_buffer=balance_buffer,
			z_limit=z_limit,
			candidate=candidate,
			closed=closed,
			message=message,
		)
	except Exception as exc:  # pylint: disable=broad-except
		print(f"⚠️  Hourly loss cleanup strategy failed: {exc}")
		_log_cleanup_action(
			service_folder=service_folder,
			now_utc=now_utc,
			daily_clean_profit=0.0,
			balance_buffer=0.0,
			z_limit=0.0,
			candidate=None,
			closed=False,
			message=f"Strategy failed: {exc}",
		)