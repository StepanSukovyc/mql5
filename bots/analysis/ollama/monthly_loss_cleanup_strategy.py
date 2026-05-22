"""Rolling 30-day loss-cleanup advisory strategy.

Computes the realized-profit surplus over a 30-calendar-day rolling window
(weighted by actual active trading days) and recommends which stale losing
positions could be safely closed.

The strategy NEVER closes positions itself — it only writes a JSON
recommendation file at ``trade_logs/monthly_loss_cleanup_recommendations.json``.

Configuration (.env keys, all optional):
  MONTHLY_LOSS_CLEANUP_ENABLED            bool   default True
  MONTHLY_LOSS_CLEANUP_HOUR               int    default 13   (Prague time)
  MONTHLY_LOSS_CLEANUP_MINUTE             int    default 0    (Prague time)
  MONTHLY_LOSS_CLEANUP_DAILY_TARGET_USD   float  default 50.0
  MONTHLY_LOSS_CLEANUP_MIN_ACTIVE_DAYS    int    default 15   (floor for target calc)
  MONTHLY_LOSS_CLEANUP_MIN_POSITION_AGE_DAYS int default 30
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import MetaTrader5 as mt5
import pytz

from swap_rollover import get_swap_block_window


PRAGUE_TZ = pytz.timezone("Europe/Prague")

DEFAULT_ENABLED = True
DEFAULT_RUN_HOUR = 13
DEFAULT_RUN_MINUTE = 0
DEFAULT_DAILY_TARGET_USD = 50.0
DEFAULT_MIN_ACTIVE_DAYS = 15
DEFAULT_MIN_POSITION_AGE_DAYS = 30

FEE_PER_001_LOT = 0.10

RECOMMENDATION_FILE = "monthly_loss_cleanup_recommendations.json"
STATE_FILE = "monthly_loss_cleanup_state.json"

_LAST_EVALUATED_DAY_KEY: Optional[str] = None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

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


def _get_enabled() -> bool:
	return _to_bool(_load_dotenv_value("MONTHLY_LOSS_CLEANUP_ENABLED"), default=DEFAULT_ENABLED)


def _get_run_hour() -> int:
	raw = _load_dotenv_value("MONTHLY_LOSS_CLEANUP_HOUR")
	if raw is None:
		return DEFAULT_RUN_HOUR
	try:
		hour = int(raw)
		if 0 <= hour <= 23:
			return hour
	except ValueError:
		pass
	print(f"⚠️  Nevalidni MONTHLY_LOSS_CLEANUP_HOUR='{raw}', pouzivam {DEFAULT_RUN_HOUR}")
	return DEFAULT_RUN_HOUR


def _get_run_minute() -> int:
	raw = _load_dotenv_value("MONTHLY_LOSS_CLEANUP_MINUTE")
	if raw is None:
		return DEFAULT_RUN_MINUTE
	try:
		minute = int(raw)
		if 0 <= minute <= 59:
			return minute
	except ValueError:
		pass
	print(f"⚠️  Nevalidni MONTHLY_LOSS_CLEANUP_MINUTE='{raw}', pouzivam {DEFAULT_RUN_MINUTE}")
	return DEFAULT_RUN_MINUTE


def _get_daily_target_usd() -> float:
	raw = _load_dotenv_value("MONTHLY_LOSS_CLEANUP_DAILY_TARGET_USD")
	if raw is None:
		return DEFAULT_DAILY_TARGET_USD
	try:
		v = float(raw)
		if v > 0:
			return v
	except ValueError:
		pass
	print(f"⚠️  Nevalidni MONTHLY_LOSS_CLEANUP_DAILY_TARGET_USD='{raw}', pouzivam {DEFAULT_DAILY_TARGET_USD}")
	return DEFAULT_DAILY_TARGET_USD


def _get_min_active_days() -> int:
	raw = _load_dotenv_value("MONTHLY_LOSS_CLEANUP_MIN_ACTIVE_DAYS")
	if raw is None:
		return DEFAULT_MIN_ACTIVE_DAYS
	try:
		v = int(raw)
		if v >= 1:
			return v
	except ValueError:
		pass
	print(f"⚠️  Nevalidni MONTHLY_LOSS_CLEANUP_MIN_ACTIVE_DAYS='{raw}', pouzivam {DEFAULT_MIN_ACTIVE_DAYS}")
	return DEFAULT_MIN_ACTIVE_DAYS


def _get_min_position_age_days() -> int:
	raw = _load_dotenv_value("MONTHLY_LOSS_CLEANUP_MIN_POSITION_AGE_DAYS")
	if raw is None:
		return DEFAULT_MIN_POSITION_AGE_DAYS
	try:
		v = int(raw)
		if v >= 1:
			return v
	except ValueError:
		pass
	print(
		f"⚠️  Nevalidni MONTHLY_LOSS_CLEANUP_MIN_POSITION_AGE_DAYS='{raw}', "
		f"pouzivam {DEFAULT_MIN_POSITION_AGE_DAYS}"
	)
	return DEFAULT_MIN_POSITION_AGE_DAYS


def _get_service_folder() -> Optional[Path]:
	raw = _load_dotenv_value("SERVICE_DEST_FOLDER")
	if not raw:
		return None
	return Path(raw)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _get_position_fee(volume: float) -> float:
	return round((float(volume) / 0.01) * FEE_PER_001_LOT, 2)


def _get_closing_entries() -> set[int]:
	return {
		getattr(mt5, "DEAL_ENTRY_OUT", 1),
		getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
		getattr(mt5, "DEAL_ENTRY_INOUT", 2),
	}


def _get_deals_between(start_utc: datetime, end_utc: datetime) -> tuple[Any, ...]:
	deals = mt5.history_deals_get(start_utc, end_utc)
	if deals is None:
		raise RuntimeError(f"Failed to get deal history: {mt5.last_error()}")
	return tuple(deals)


def _get_current_prague_day_key(now_prague: datetime) -> str:
	return now_prague.strftime("%Y%m%d")


def _is_after_run_time(now_prague: datetime, run_hour: int, run_minute: int) -> bool:
	return (now_prague.hour, now_prague.minute) >= (run_hour, run_minute)


def _is_in_restricted_trading_hours(now_utc: datetime) -> bool:
	return get_swap_block_window(now_utc=now_utc).contains(now_utc)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _get_state_path(service_folder: Optional[Path]) -> Path:
	if service_folder is not None:
		state_dir = service_folder / "trade_logs"
	else:
		state_dir = Path(__file__).resolve().parent
	state_dir.mkdir(parents=True, exist_ok=True)
	return state_dir / STATE_FILE


def _get_persisted_last_run_day_key(service_folder: Optional[Path]) -> Optional[str]:
	state_path = _get_state_path(service_folder)
	if not state_path.exists():
		return None
	try:
		payload = json.loads(state_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return None
	day_key = payload.get("last_run_prague_day_key")
	return str(day_key) if day_key else None


def _persist_last_run_day_key(service_folder: Optional[Path], day_key: str) -> None:
	state_path = _get_state_path(service_folder)
	state_path.write_text(
		json.dumps({"last_run_prague_day_key": day_key}, ensure_ascii=True, indent=2),
		encoding="utf-8",
	)


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

@dataclass
class PositionCandidate:
	ticket: int
	symbol: str
	volume: float
	opened_at_utc: str
	opened_at_prague: str
	age_days: int
	profit: float
	swap: float
	fee: float
	loss_amount: float
	covered_by_surplus: bool
	recommended: bool
	surplus_remaining_after: float


def _calculate_30d_realized_profit(
	deals: tuple[Any, ...],
	closing_entries: set[int],
) -> tuple[float, int]:
	"""Return (realized_profit, active_trading_days) from the deal list.

	Realized profit = sum of (profit + swap + commission + fee) for all
	closing deals.  Active trading days = number of unique UTC calendar dates
	that contain at least one closing deal.
	"""
	realized = 0.0
	active_dates: set[date] = set()

	for deal in deals:
		entry = getattr(deal, "entry", None)
		if entry not in closing_entries:
			continue
		realized += (
			float(getattr(deal, "profit", 0.0) or 0.0)
			+ float(getattr(deal, "swap", 0.0) or 0.0)
			+ float(getattr(deal, "commission", 0.0) or 0.0)
			+ float(getattr(deal, "fee", 0.0) or 0.0)
		)
		deal_time = datetime.fromtimestamp(int(getattr(deal, "time", 0)), tz=timezone.utc)
		active_dates.add(deal_time.date())

	return round(realized, 2), len(active_dates)


def _build_candidates(
	now_utc: datetime,
	min_age_days: int,
	surplus: float,
) -> list[PositionCandidate]:
	"""Find all open positions older than *min_age_days* that are currently losing.

	Candidates are sorted from largest to smallest loss.  Greedy selection
	marks them *recommended* as long as the remaining surplus covers the loss.
	"""
	positions = mt5.positions_get()
	if positions is None:
		raise RuntimeError(f"Failed to get open positions: {mt5.last_error()}")

	cutoff = now_utc - timedelta(days=min_age_days)
	raw: list[PositionCandidate] = []

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

		raw.append(
			PositionCandidate(
				ticket=int(position.ticket),
				symbol=str(position.symbol),
				volume=float(position.volume),
				opened_at_utc=opened_at.isoformat(),
				opened_at_prague=opened_at.astimezone(PRAGUE_TZ).isoformat(),
				age_days=int((now_utc - opened_at).days),
				profit=profit,
				swap=swap,
				fee=fee,
				loss_amount=loss_amount,
				covered_by_surplus=False,
				recommended=False,
				surplus_remaining_after=0.0,
			)
		)

	# Sort largest loss first for greedy selection
	raw.sort(key=lambda c: c.loss_amount, reverse=True)

	remaining = surplus
	for candidate in raw:
		covered = candidate.loss_amount <= remaining
		candidate.covered_by_surplus = covered
		if covered:
			candidate.recommended = True
			remaining = round(remaining - candidate.loss_amount, 2)
		candidate.surplus_remaining_after = remaining

	return raw


# ---------------------------------------------------------------------------
# Recommendation file
# ---------------------------------------------------------------------------

def _get_recommendation_path(service_folder: Optional[Path]) -> Path:
	if service_folder is not None:
		log_dir = service_folder / "trade_logs"
	else:
		log_dir = Path(__file__).resolve().parent
	log_dir.mkdir(parents=True, exist_ok=True)
	return log_dir / RECOMMENDATION_FILE


def _write_recommendations(
	*,
	service_folder: Optional[Path],
	now_utc: datetime,
	window_start_utc: datetime,
	realized_profit_30d: float,
	active_trading_days: int,
	min_active_days_floor: int,
	effective_active_days: int,
	daily_target_usd: float,
	target: float,
	surplus: float,
	min_position_age_days: int,
	candidates: list[PositionCandidate],
	message: str,
) -> None:
	rec_path = _get_recommendation_path(service_folder)

	payload: dict[str, Any] = {
		"generated_at_utc": now_utc.isoformat(),
		"generated_at_prague": now_utc.astimezone(PRAGUE_TZ).isoformat(),
		"window_start_utc": window_start_utc.isoformat(),
		"window_end_utc": now_utc.isoformat(),
		"realized_profit_30d": realized_profit_30d,
		"active_trading_days": active_trading_days,
		"min_active_days_floor": min_active_days_floor,
		"effective_active_days": effective_active_days,
		"daily_target_usd": daily_target_usd,
		"target": target,
		"surplus": surplus,
		"min_position_age_days": min_position_age_days,
		"message": message,
		"candidates": [asdict(c) for c in candidates],
		"recommended_to_close": [asdict(c) for c in candidates if c.recommended],
	}

	rec_path.write_text(
		json.dumps(payload, ensure_ascii=False, indent=2),
		encoding="utf-8",
	)
	print(f"   Doporuceni zapsano do: {rec_path}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_monthly_loss_cleanup_strategy_if_due(account_info: Optional[dict[str, Any]] = None) -> None:
	"""Evaluate the rolling-30-day advisory strategy once per Prague day after the configured time."""
	global _LAST_EVALUATED_DAY_KEY

	if not _get_enabled():
		return

	now_utc = datetime.now(tz=timezone.utc)
	now_prague = now_utc.astimezone(PRAGUE_TZ)
	run_hour = _get_run_hour()
	run_minute = _get_run_minute()
	day_key = _get_current_prague_day_key(now_prague)

	if not _is_after_run_time(now_prague, run_hour, run_minute):
		return
	if _LAST_EVALUATED_DAY_KEY == day_key:
		return

	service_folder = _get_service_folder()
	if _get_persisted_last_run_day_key(service_folder) == day_key:
		_LAST_EVALUATED_DAY_KEY = day_key
		return

	_LAST_EVALUATED_DAY_KEY = day_key
	_persist_last_run_day_key(service_folder, day_key)

	daily_target_usd = _get_daily_target_usd()
	min_active_days = _get_min_active_days()
	min_position_age_days = _get_min_position_age_days()

	print("\n📋 Monthly rolling loss-cleanup advisory strategy")
	print(f"   Time (Prague): {now_prague.strftime('%Y-%m-%d %H:%M:%S')}")

	window_start_utc = now_utc - timedelta(days=30)

	try:
		if _is_in_restricted_trading_hours(now_utc):
			window = get_swap_block_window(now_utc=now_utc)
			message = (
				"Swap rollover block window "
				f"{window.start_utc.strftime('%H:%M')}-{window.end_utc.strftime('%H:%M')} UTC, advisory skipped"
			)
			print(f"   {message}")
			_write_recommendations(
				service_folder=service_folder,
				now_utc=now_utc,
				window_start_utc=window_start_utc,
				realized_profit_30d=0.0,
				active_trading_days=0,
				min_active_days_floor=min_active_days,
				effective_active_days=0,
				daily_target_usd=daily_target_usd,
				target=0.0,
				surplus=0.0,
				min_position_age_days=min_position_age_days,
				candidates=[],
				message=message,
			)
			return

		closing_entries = _get_closing_entries()
		deals = _get_deals_between(window_start_utc, now_utc)
		realized_profit_30d, active_trading_days = _calculate_30d_realized_profit(deals, closing_entries)
		effective_active_days = max(active_trading_days, min_active_days)
		target = round(effective_active_days * daily_target_usd, 2)
		surplus = round(realized_profit_30d - target, 2)

		print(f"   Okno: {window_start_utc.strftime('%Y-%m-%d')} – {now_utc.strftime('%Y-%m-%d')} UTC")
		print(f"   Realizovany zisk (30 dni): {realized_profit_30d:.2f} USD")
		print(f"   Aktivni obchodni dny: {active_trading_days}  (floor: {min_active_days} → efektivni: {effective_active_days})")
		print(f"   Denni cil: {daily_target_usd:.2f} USD  ×  {effective_active_days} dni = target {target:.2f} USD")
		print(f"   Surplus: {surplus:.2f} USD")

		if surplus <= 0:
			message = f"Surplus {surplus:.2f} USD <= 0, zadna pozice neni doporucena k uzavreni"
			print(f"   {message}")
			_write_recommendations(
				service_folder=service_folder,
				now_utc=now_utc,
				window_start_utc=window_start_utc,
				realized_profit_30d=realized_profit_30d,
				active_trading_days=active_trading_days,
				min_active_days_floor=min_active_days,
				effective_active_days=effective_active_days,
				daily_target_usd=daily_target_usd,
				target=target,
				surplus=surplus,
				min_position_age_days=min_position_age_days,
				candidates=[],
				message=message,
			)
			return

		candidates = _build_candidates(now_utc, min_position_age_days, surplus)
		recommended = [c for c in candidates if c.recommended]

		if not candidates:
			message = (
				f"Surplus {surplus:.2f} USD > 0, ale zadna ztrátová pozice starsi nez "
				f"{min_position_age_days} dni neni otevrena"
			)
		elif not recommended:
			message = (
				f"Surplus {surplus:.2f} USD > 0, ale zadna ztrátová pozice nevejde do surplusu "
				f"(nejmensi ztrata: {candidates[-1].loss_amount:.2f} USD)"
			)
		else:
			message = (
				f"Doporuceno k uzavreni {len(recommended)} pozic "
				f"(celkova ztrata: {sum(c.loss_amount for c in recommended):.2f} USD, "
				f"surplus po uzavreni: {recommended[-1].surplus_remaining_after:.2f} USD)"
			)

		print(f"   Kandidáti celkem: {len(candidates)}")
		print(f"   {message}")
		for c in recommended:
			print(
				f"   ✅ Doporucenicka: {c.symbol} ticket={c.ticket} "
				f"ztrata={c.loss_amount:.2f} USD  vek={c.age_days} dni"
			)

		_write_recommendations(
			service_folder=service_folder,
			now_utc=now_utc,
			window_start_utc=window_start_utc,
			realized_profit_30d=realized_profit_30d,
			active_trading_days=active_trading_days,
			min_active_days_floor=min_active_days,
			effective_active_days=effective_active_days,
			daily_target_usd=daily_target_usd,
			target=target,
			surplus=surplus,
			min_position_age_days=min_position_age_days,
			candidates=candidates,
			message=message,
		)

	except Exception as exc:  # pylint: disable=broad-except
		print(f"⚠️  Monthly loss cleanup advisory failed: {exc}")
		_write_recommendations(
			service_folder=service_folder,
			now_utc=now_utc,
			window_start_utc=window_start_utc,
			realized_profit_30d=0.0,
			active_trading_days=0,
			min_active_days_floor=min_active_days,
			effective_active_days=0,
			daily_target_usd=daily_target_usd,
			target=0.0,
			surplus=0.0,
			min_position_age_days=min_position_age_days,
			candidates=[],
			message=f"Strategy failed: {exc}",
		)
