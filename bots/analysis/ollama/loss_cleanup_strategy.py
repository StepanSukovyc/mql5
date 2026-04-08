"""Hourly cleanup strategy for closing selected stale losing positions."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import MetaTrader5 as mt5
import pytz

from trade_execution import close_position_by_ticket


PRAGUE_TZ = pytz.timezone("Europe/Prague")
DEFAULT_ENABLED = True
DEFAULT_RUN_HOUR = 12
DEFAULT_RUN_MINUTE = 45
DEFAULT_DRY_RUN = True
ACCOUNT_BALANCE_BUFFER_RATIO = 0.01
MIN_POSITION_AGE = timedelta(days=7)
FEE_PER_001_LOT = 0.10
LOSS_CLEANUP_STATE_FILE = "loss_cleanup_state.json"
LOSS_CLEANUP_LOG_HEADERS = [
	"timestamp",
	"dry_run",
	"daily_clean_profit",
	"current_open_profit",
	"effective_profit_budget",
	"balance_buffer",
	"z_limit",
	"counted_closing_deals",
	"latest_counted_deal_time_utc",
	"latest_counted_deal_time_prague",
	"symbol",
	"ticket",
	"volume",
	"profit",
	"swap",
	"fee",
	"loss_amount",
	"closed",
	"message",
	"daily_realized_profit",
]

_LAST_EVALUATED_DAY_KEY: Optional[str] = None


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


def _get_strategy_run_hour() -> int:
	raw_value = _load_dotenv_value("LOSS_CLEANUP_STRATEGY_HOUR")
	if raw_value is None:
		return DEFAULT_RUN_HOUR

	try:
		hour = int(raw_value)
		if 0 <= hour <= 23:
			return hour
	except ValueError:
		pass

	print(
		f"⚠️  Nevalidni LOSS_CLEANUP_STRATEGY_HOUR='{raw_value}', "
		f"pouzivam {DEFAULT_RUN_HOUR}"
	)
	return DEFAULT_RUN_HOUR


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


def _get_prague_day_bounds_utc(day_prague: date) -> tuple[datetime, datetime]:
	day_start_prague = PRAGUE_TZ.localize(datetime.combine(day_prague, datetime.min.time()))
	day_end_prague = day_start_prague + timedelta(days=1)
	return day_start_prague.astimezone(timezone.utc), day_end_prague.astimezone(timezone.utc)


def _get_previous_prague_day_bounds_utc(now_utc: datetime) -> tuple[date, datetime, datetime]:
	yesterday_prague = now_utc.astimezone(PRAGUE_TZ).date() - timedelta(days=1)
	start_utc, end_utc = _get_prague_day_bounds_utc(yesterday_prague)
	return yesterday_prague, start_utc, end_utc


def _get_current_prague_day_key(now_prague: datetime) -> str:
	return now_prague.strftime("%Y%m%d")


def _is_after_daily_run_time(now_prague: datetime, run_hour: int, run_minute: int) -> bool:
	return (now_prague.hour, now_prague.minute) >= (run_hour, run_minute)


def _get_closing_entries() -> set[int]:
	return {
		getattr(mt5, "DEAL_ENTRY_OUT", 1),
		getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
		getattr(mt5, "DEAL_ENTRY_INOUT", 2),
	}


def _get_orders_between(start_utc: datetime, end_utc: datetime) -> tuple[Any, ...]:
	orders = mt5.history_orders_get(start_utc, end_utc)
	if orders is None:
		raise RuntimeError(f"Failed to get order history: {mt5.last_error()}")
	return tuple(orders)


def _get_deals_between(start_utc: datetime, end_utc: datetime) -> tuple[tuple[Any, ...], set[int]]:
	deals = mt5.history_deals_get(start_utc, end_utc)
	if deals is None:
		raise RuntimeError(f"Failed to get deal history: {mt5.last_error()}")
	return tuple(deals), _get_closing_entries()


def _get_today_closed_position_ids(deals: tuple[Any, ...], closing_entries: set[int]) -> set[int]:
	closed_position_ids: set[int] = set()

	for deal in deals:
		position_id = int(getattr(deal, "position_id", 0) or 0)
		if position_id <= 0:
			continue
		if getattr(deal, "entry", None) not in closing_entries:
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


def _get_deal_realized_profit(deal: Any) -> float:
	return (
		float(getattr(deal, "profit", 0.0) or 0.0)
		+ float(getattr(deal, "swap", 0.0) or 0.0)
		+ float(getattr(deal, "commission", 0.0) or 0.0)
		+ float(getattr(deal, "fee", 0.0) or 0.0)
	)


def _get_actual_deal_fee(deal: Any) -> float:
	return float(getattr(deal, "fee", 0.0) or 0.0)


def _calculate_daily_realized_profit(deals: tuple[Any, ...]) -> float:
	realized_profit = 0.0
	for deal in deals:
		realized_profit += _get_deal_realized_profit(deal)

	return realized_profit


def _calculate_current_open_profit(raw_balance: float, equity: float) -> float:
	return round(float(equity) - float(raw_balance), 2)


def _calculate_effective_profit_budget(daily_realized_profit: float, current_open_profit: float) -> float:
	return round(daily_realized_profit + min(current_open_profit, 0.0), 2)


def _would_keep_realized_profit_non_negative(
	effective_profit_budget: float,
	loss_amount: float,
) -> bool:
	remaining_realized_profit = round(effective_profit_budget - loss_amount, 2)
	return remaining_realized_profit >= 0


def _get_latest_counted_deal_time(
	deals: tuple[Any, ...],
	closing_entries: set[int],
	closed_position_ids: set[int],
) -> Optional[datetime]:
	latest_time: Optional[datetime] = None
	for deal in deals:
		position_id = int(getattr(deal, "position_id", 0) or 0)
		if position_id not in closed_position_ids:
			continue
		if getattr(deal, "entry", None) not in closing_entries:
			continue
		deal_time = datetime.fromtimestamp(int(getattr(deal, "time", 0)), tz=timezone.utc)
		if latest_time is None or deal_time > latest_time:
			latest_time = deal_time

	return latest_time


def _get_loss_cleanup_state_path(service_folder: Optional[Path]) -> Path:
	if service_folder is not None:
		state_dir = service_folder / "trade_logs"
	else:
		state_dir = Path(__file__).resolve().parent
	state_dir.mkdir(parents=True, exist_ok=True)
	return state_dir / LOSS_CLEANUP_STATE_FILE


def _get_persisted_last_run_day_key(service_folder: Optional[Path]) -> Optional[str]:
	state_path = _get_loss_cleanup_state_path(service_folder)
	if not state_path.exists():
		return None

	try:
		payload = json.loads(state_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return None

	day_key = payload.get("last_run_prague_day_key")
	return str(day_key) if day_key else None


def _persist_last_run_day_key(service_folder: Optional[Path], day_key: str) -> None:
	state_path = _get_loss_cleanup_state_path(service_folder)
	state_path.write_text(
		json.dumps({"last_run_prague_day_key": day_key}, ensure_ascii=True, indent=2),
		encoding="utf-8",
	)


def _ensure_loss_cleanup_log_schema(log_file: Path) -> None:
	if not log_file.exists():
		return

	with open(log_file, "r", newline="", encoding="utf-8") as handle:
		rows = list(csv.reader(handle))

	if not rows:
		return

	headers = rows[0]
	headers = ["daily_clean_profit" if header == "daily_gross_profit" else header for header in headers]
	if headers == LOSS_CLEANUP_LOG_HEADERS:
		return

	updated_headers = LOSS_CLEANUP_LOG_HEADERS
	updated_rows = []
	for row in rows[1:]:
		padded_row = list(row[: len(updated_headers)])
		if len(padded_row) < len(updated_headers):
			padded_row.extend([""] * (len(updated_headers) - len(padded_row)))
		updated_rows.append(padded_row)

	with open(log_file, "w", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		writer.writerow(updated_headers)
		writer.writerows(updated_rows)


def _ensure_daily_deals_snapshot_schema(log_file: Path, headers: list[str]) -> None:
	if not log_file.exists():
		return

	with open(log_file, "r", newline="", encoding="utf-8") as handle:
		rows = list(csv.reader(handle))

	if not rows:
		return

	existing_headers = rows[0]
	if existing_headers == headers:
		return

	index_by_header = {header: index for index, header in enumerate(existing_headers)}
	updated_rows = []
	for row in rows[1:]:
		actual_fee = ""
		modeled_fee = ""
		if "actual_fee" in index_by_header:
			actual_fee = row[index_by_header["actual_fee"]] if index_by_header["actual_fee"] < len(row) else ""
		elif "fee" in index_by_header:
			modeled_fee = row[index_by_header["fee"]] if index_by_header["fee"] < len(row) else ""

		realized_component = ""
		profit_raw = row[index_by_header["profit"]] if "profit" in index_by_header and index_by_header["profit"] < len(row) else ""
		swap_raw = row[index_by_header["swap"]] if "swap" in index_by_header and index_by_header["swap"] < len(row) else ""
		commission_raw = row[index_by_header["commission"]] if "commission" in index_by_header and index_by_header["commission"] < len(row) else ""
		if profit_raw or swap_raw or commission_raw or actual_fee:
			try:
				realized_component = (
					f"{float(profit_raw or 0.0) + float(swap_raw or 0.0) + float(commission_raw or 0.0) + float(actual_fee or 0.0):.2f}"
				)
			except ValueError:
				realized_component = ""

		updated_row = []
		for header in headers:
			if header == "actual_fee":
				updated_row.append(actual_fee)
			elif header == "modeled_fee":
				updated_row.append(modeled_fee)
			elif header == "realized_component":
				updated_row.append(realized_component)
			else:
				index = index_by_header.get(header)
				updated_row.append(row[index] if index is not None and index < len(row) else "")
		updated_rows.append(updated_row)

	with open(log_file, "w", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		writer.writerow(headers)
		writer.writerows(updated_rows)


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
		"snapshot_timestamp_prague",
		"day_start_utc",
		"day_start_prague",
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
		"actual_fee",
		"modeled_fee",
		"realized_component",
		"position_closed_today",
		"included_in_daily_clean_profit",
	]
	_ensure_daily_deals_snapshot_schema(log_file, headers)
	file_exists = log_file.exists()

	with open(log_file, "a", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		if not file_exists:
			writer.writerow(headers)

		for deal in deals:
			deal_time_utc = datetime.fromtimestamp(int(getattr(deal, "time", 0)), tz=timezone.utc)
			deal_time_prague = deal_time_utc.astimezone(PRAGUE_TZ)
			entry = getattr(deal, "entry", None)
			position_id = int(getattr(deal, "position_id", 0) or 0)
			actual_fee = _get_actual_deal_fee(deal)
			modeled_fee = _get_position_fee(float(getattr(deal, 'volume', 0.0) or 0.0))
			realized_component = _get_deal_realized_profit(deal)
			writer.writerow(
				[
					now_utc.isoformat(),
					now_utc.astimezone(PRAGUE_TZ).isoformat(),
					day_start_utc.isoformat(),
					day_start_utc.astimezone(PRAGUE_TZ).isoformat(),
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
					f"{actual_fee:.2f}",
					f"{modeled_fee:.2f}",
					f"{realized_component:.2f}",
					str(position_id in closed_position_ids),
					str(position_id in closed_position_ids and entry in closing_entries),
				]
			)


def _find_cleanup_candidate(
	z_limit: float,
	now_utc: datetime,
	effective_profit_budget: float,
) -> Optional[CleanupCandidate]:
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
		if not _would_keep_realized_profit_non_negative(effective_profit_budget, loss_amount):
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
	daily_realized_profit: float,
	current_open_profit: float,
	effective_profit_budget: float,
	balance_buffer: float,
	z_limit: float,
	counted_closing_deals: int,
	latest_counted_deal_time: Optional[datetime],
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
	file_exists = log_file.exists()

	with open(log_file, "a", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		if not file_exists:
			writer.writerow(LOSS_CLEANUP_LOG_HEADERS)

		writer.writerow(
			[
				now_utc.isoformat(),
				str(_get_strategy_dry_run()),
				f"{daily_clean_profit:.2f}",
				f"{current_open_profit:.2f}",
				f"{effective_profit_budget:.2f}",
				f"{balance_buffer:.2f}",
				f"{z_limit:.2f}",
				counted_closing_deals,
				latest_counted_deal_time.isoformat() if latest_counted_deal_time else "",
				latest_counted_deal_time.astimezone(PRAGUE_TZ).isoformat() if latest_counted_deal_time else "",
				candidate.symbol if candidate else "",
				candidate.ticket if candidate else "",
				f"{candidate.volume:.2f}" if candidate else "",
				f"{candidate.profit:.2f}" if candidate else "",
				f"{candidate.swap:.2f}" if candidate else "",
				f"{candidate.fee:.2f}" if candidate else "",
				f"{candidate.loss_amount:.2f}" if candidate else "",
				str(closed),
				message,
				f"{daily_realized_profit:.2f}",
			]
		)


def run_loss_cleanup_strategy_if_due(account_info: Optional[dict[str, Any]] = None) -> None:
	"""Evaluate the daily loss-cleanup strategy after its configured Prague-time trigger."""
	global _LAST_EVALUATED_DAY_KEY

	if not _get_strategy_enabled():
		return

	now_utc = datetime.now(tz=timezone.utc)
	now_prague = now_utc.astimezone(PRAGUE_TZ)
	run_hour = _get_strategy_run_hour()
	run_minute = _get_strategy_run_minute()
	dry_run = _get_strategy_dry_run()
	day_key = _get_current_prague_day_key(now_prague)

	if not _is_after_daily_run_time(now_prague, run_hour, run_minute):
		return
	if _LAST_EVALUATED_DAY_KEY == day_key:
		return

	service_folder = _get_service_folder()
	if _get_persisted_last_run_day_key(service_folder) == day_key:
		return

	_LAST_EVALUATED_DAY_KEY = day_key
	_persist_last_run_day_key(service_folder, day_key)

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
				daily_realized_profit=0.0,
				current_open_profit=0.0,
				effective_profit_budget=0.0,
				balance_buffer=0.0,
				z_limit=0.0,
				counted_closing_deals=0,
				latest_counted_deal_time=None,
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
			equity = float(account.equity)
		else:
			raw_balance = float(account_info.get("raw_balance", account_info.get("balance", 0.0)))
			equity = float(account_info.get("equity", raw_balance))

		reference_day_prague, day_start_utc, day_end_utc = _get_previous_prague_day_bounds_utc(now_utc)
		orders = _get_orders_between(day_start_utc, day_end_utc)
		deals, closing_entries = _get_deals_between(day_start_utc, day_end_utc)
		closed_position_ids = _get_today_closed_position_ids(deals, closing_entries)
		daily_clean_profit = round(_calculate_daily_clean_profit(deals, closing_entries, closed_position_ids), 2)
		daily_realized_profit = round(_calculate_daily_realized_profit(deals), 2)
		current_open_profit = _calculate_current_open_profit(raw_balance, equity)
		effective_profit_budget = _calculate_effective_profit_budget(daily_realized_profit, current_open_profit)
		balance_buffer = round(raw_balance * ACCOUNT_BALANCE_BUFFER_RATIO, 2)
		z_limit = round(effective_profit_budget - balance_buffer, 2)
		closing_deals_count = sum(
			1
			for deal in deals
			if int(getattr(deal, "position_id", 0) or 0) in closed_position_ids
			and getattr(deal, "entry", None) in closing_entries
		)
		latest_counted_deal_time = _get_latest_counted_deal_time(deals, closing_entries, closed_position_ids)

		_log_daily_deals_snapshot(
			service_folder=service_folder,
			now_utc=now_utc,
			day_start_utc=day_start_utc,
			deals=deals,
			closing_entries=closing_entries,
			closed_position_ids=closed_position_ids,
		)

		print("\n🧹 Daily loss cleanup strategy")
		print(f"   Time (Prague): {now_prague.strftime('%Y-%m-%d %H:%M:%S')}")
		print(f"   Reference day (Prague): {reference_day_prague.isoformat()}")
		print(f"   Dry run: {'ON' if dry_run else 'OFF'}")
		print(
			f"   MT5 reference-day data: orders={len(orders)}, deals={len(deals)}, "
			f"closed_positions={len(closed_position_ids)}, closing_deals={closing_deals_count}"
		)
		print(f"   Previous-day closed-deal clean profit (profit only): {daily_clean_profit:.2f}")
		print(
			"   Previous-day realized result from MT5 deal history "
			f"(profit + swap + commission + actual MT5 fee): {daily_realized_profit:.2f}"
		)
		print(f"   Current open P/L (equity - raw balance): {current_open_profit:.2f}")
		print(f"   Effective profit budget after open P/L: {effective_profit_budget:.2f}")
		if latest_counted_deal_time is not None:
			print(
				"   Reference-day snapshot includes closing deals up to "
				f"{latest_counted_deal_time.astimezone(PRAGUE_TZ).strftime('%Y-%m-%d %H:%M:%S')} Prague time"
			)
		print(f"   Balance buffer (1%): {balance_buffer:.2f}")
		print(f"   Z limit (after open P/L and buffer): {z_limit:.2f}")

		if z_limit <= 0:
			message = "Z <= 0 after current open P/L and buffer, no position can be closed safely"
			print(f"   {message}")
			_log_cleanup_action(
				service_folder=service_folder,
				now_utc=now_utc,
				daily_clean_profit=daily_clean_profit,
				daily_realized_profit=daily_realized_profit,
				current_open_profit=current_open_profit,
				effective_profit_budget=effective_profit_budget,
				balance_buffer=balance_buffer,
				z_limit=z_limit,
				counted_closing_deals=closing_deals_count,
				latest_counted_deal_time=latest_counted_deal_time,
				candidate=None,
				closed=False,
				message=message,
			)
			return

		candidate = _find_cleanup_candidate(z_limit, now_utc, effective_profit_budget)
		if candidate is None:
			message = "No losing position older than 7 days fits below safe Z budget"
			print(f"   {message}")
			_log_cleanup_action(
				service_folder=service_folder,
				now_utc=now_utc,
				daily_clean_profit=daily_clean_profit,
				daily_realized_profit=daily_realized_profit,
				current_open_profit=current_open_profit,
				effective_profit_budget=effective_profit_budget,
				balance_buffer=balance_buffer,
				z_limit=z_limit,
				counted_closing_deals=closing_deals_count,
				latest_counted_deal_time=latest_counted_deal_time,
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
		remaining_realized_profit = round(effective_profit_budget - candidate.loss_amount, 2)
		print(f"   Remaining effective profit budget after close: {remaining_realized_profit:.2f}")

		if not _would_keep_realized_profit_non_negative(effective_profit_budget, candidate.loss_amount):
			message = "Candidate rejected: closing it would make effective profit budget negative"
			print(f"   {message}")
			_log_cleanup_action(
				service_folder=service_folder,
				now_utc=now_utc,
				daily_clean_profit=daily_clean_profit,
				daily_realized_profit=daily_realized_profit,
				current_open_profit=current_open_profit,
				effective_profit_budget=effective_profit_budget,
				balance_buffer=balance_buffer,
				z_limit=z_limit,
				counted_closing_deals=closing_deals_count,
				latest_counted_deal_time=latest_counted_deal_time,
				candidate=candidate,
				closed=False,
				message=message,
			)
			return

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
			daily_realized_profit=daily_realized_profit,
			current_open_profit=current_open_profit,
			effective_profit_budget=effective_profit_budget,
			balance_buffer=balance_buffer,
			z_limit=z_limit,
			counted_closing_deals=closing_deals_count,
			latest_counted_deal_time=latest_counted_deal_time,
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
			daily_realized_profit=0.0,
			current_open_profit=0.0,
			effective_profit_budget=0.0,
			balance_buffer=0.0,
			z_limit=0.0,
			counted_closing_deals=0,
			latest_counted_deal_time=None,
			candidate=None,
			closed=False,
			message=f"Strategy failed: {exc}",
		)