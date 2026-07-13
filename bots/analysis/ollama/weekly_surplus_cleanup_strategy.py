"""Weekly surplus cleanup strategy.

Every Friday (configurable weekday) after a configured UTC hour, the strategy
computes the week's realized P&L (Monday 00:00 UTC → now).  When the week's
profit exceeds a minimum income threshold the surplus is used to close the
oldest stale losing open positions, greedily, until the surplus budget is
exhausted.

The week is identified by an ISO week key (YYYY-WNN).  The strategy runs at
most once per ISO week and persists the last-run key so restarts do not
re-trigger within the same week.

Configuration (.env keys, all optional):
  WEEKLY_CLEANUP_ENABLED                  bool   default True
  WEEKLY_CLEANUP_DRY_RUN                  bool   default True
  WEEKLY_CLEANUP_RUN_WEEKDAY              int    0=Mon … 6=Sun, default 4 (Fri)
  WEEKLY_CLEANUP_RUN_HOUR_UTC             int    0-23, default 15
  WEEKLY_CLEANUP_MIN_PROFIT_USD           float  fixed USD floor (overrides percent calc)
  WEEKLY_CLEANUP_MIN_INCOME_PERCENT       float  % of TRADING_ACCOUNT_BALANCE_CAP,
                                                 default 10.0; used when MIN_PROFIT_USD=0
  WEEKLY_CLEANUP_MIN_POSITION_AGE_DAYS    int    default 7
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

import MetaTrader5 as mt5

from account_state import get_account_balance_cap
from swap_rollover import get_swap_block_window
from trade_execution import close_position_by_ticket


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ENABLED = True
DEFAULT_DRY_RUN = True
DEFAULT_RUN_WEEKDAY = 4          # Friday (Monday=0)
DEFAULT_RUN_HOUR_UTC = 15
DEFAULT_MIN_INCOME_PERCENT = 10.0
DEFAULT_MIN_POSITION_AGE_DAYS = 7

FEE_PER_001_LOT = 0.10

STATE_FILE = "weekly_surplus_cleanup_state.json"
LOG_FILE = "weekly_surplus_cleanup.csv"
LOG_HEADERS = [
    "timestamp",
    "dry_run",
    "week_key",
    "weekly_profit",
    "min_income",
    "surplus",
    "symbol",
    "ticket",
    "volume",
    "opened_at_utc",
    "age_days",
    "profit",
    "swap",
    "fee",
    "loss_amount",
    "budget_before",
    "budget_after",
    "closed",
    "message",
]

_LAST_EVALUATED_WEEK_KEY: Optional[str] = None


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


def _to_int(value: Optional[str], *, default: int, lo: int, hi: int, name: str) -> int:
    if value is None:
        return default
    try:
        v = int(value)
        if lo <= v <= hi:
            return v
    except ValueError:
        pass
    print(f"⚠️  Nevalidní {name}='{value}', používám {default}")
    return default


def _to_float(value: Optional[str], *, default: float, name: str) -> float:
    if value is None:
        return default
    try:
        v = float(value)
        if v >= 0:
            return v
    except ValueError:
        pass
    print(f"⚠️  Nevalidní {name}='{value}', používám {default}")
    return default


def _get_enabled() -> bool:
    return _to_bool(_load_dotenv_value("WEEKLY_CLEANUP_ENABLED"), default=DEFAULT_ENABLED)


def _get_dry_run() -> bool:
    return _to_bool(_load_dotenv_value("WEEKLY_CLEANUP_DRY_RUN"), default=DEFAULT_DRY_RUN)


def _get_run_weekday() -> int:
    return _to_int(
        _load_dotenv_value("WEEKLY_CLEANUP_RUN_WEEKDAY"),
        default=DEFAULT_RUN_WEEKDAY, lo=0, hi=6,
        name="WEEKLY_CLEANUP_RUN_WEEKDAY",
    )


def _get_run_hour_utc() -> int:
    return _to_int(
        _load_dotenv_value("WEEKLY_CLEANUP_RUN_HOUR_UTC"),
        default=DEFAULT_RUN_HOUR_UTC, lo=0, hi=23,
        name="WEEKLY_CLEANUP_RUN_HOUR_UTC",
    )


def _get_min_position_age_days() -> int:
    return _to_int(
        _load_dotenv_value("WEEKLY_CLEANUP_MIN_POSITION_AGE_DAYS"),
        default=DEFAULT_MIN_POSITION_AGE_DAYS, lo=1, hi=365,
        name="WEEKLY_CLEANUP_MIN_POSITION_AGE_DAYS",
    )


def _get_min_income_usd() -> float:
    """Return the minimum weekly income in USD.

    Priority:
      1. WEEKLY_CLEANUP_MIN_PROFIT_USD if > 0
      2. TRADING_ACCOUNT_BALANCE_CAP × WEEKLY_CLEANUP_MIN_INCOME_PERCENT / 100
    """
    explicit = _to_float(
        _load_dotenv_value("WEEKLY_CLEANUP_MIN_PROFIT_USD"),
        default=0.0,
        name="WEEKLY_CLEANUP_MIN_PROFIT_USD",
    )
    if explicit > 0:
        return explicit

    percent = _to_float(
        _load_dotenv_value("WEEKLY_CLEANUP_MIN_INCOME_PERCENT"),
        default=DEFAULT_MIN_INCOME_PERCENT,
        name="WEEKLY_CLEANUP_MIN_INCOME_PERCENT",
    )
    balance_cap = get_account_balance_cap()
    return round(balance_cap * percent / 100.0, 2)


def _get_service_folder() -> Optional[Path]:
    raw = _load_dotenv_value("SERVICE_DEST_FOLDER")
    if not raw:
        return None
    return Path(raw)


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

def _get_iso_week_key(dt_utc: datetime) -> str:
    """Return 'YYYY-WNN' for the ISO week that contains dt_utc."""
    iso = dt_utc.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _get_week_start_utc(dt_utc: datetime) -> datetime:
    """Return Monday 00:00:00 UTC of the ISO week that contains dt_utc."""
    days_since_monday = dt_utc.weekday()          # Monday=0
    monday = dt_utc - timedelta(days=days_since_monday)
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _is_trigger_time(now_utc: datetime, run_weekday: int, run_hour: int) -> bool:
    """True when it is the configured weekday and the hour has been reached."""
    return now_utc.weekday() == run_weekday and now_utc.hour >= run_hour


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


def _get_persisted_last_run_week_key(service_folder: Optional[Path]) -> Optional[str]:
    path = _get_state_path(service_folder)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    v = payload.get("last_run_week_key")
    return str(v) if v else None


def _persist_last_run_week_key(service_folder: Optional[Path], week_key: str) -> None:
    path = _get_state_path(service_folder)
    path.write_text(
        json.dumps({"last_run_week_key": week_key}, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


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


def _calculate_weekly_realized_profit(
    deals: tuple[Any, ...],
    closing_entries: set[int],
) -> float:
    """Sum profit+swap+commission+fee for all closing deals in the window."""
    total = 0.0
    for deal in deals:
        if getattr(deal, "entry", None) not in closing_entries:
            continue
        total += (
            float(getattr(deal, "profit", 0.0) or 0.0)
            + float(getattr(deal, "swap", 0.0) or 0.0)
            + float(getattr(deal, "commission", 0.0) or 0.0)
            + float(getattr(deal, "fee", 0.0) or 0.0)
        )
    return round(total, 2)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

@dataclass
class CleanupCandidate:
    ticket: int
    symbol: str
    position_type: int
    volume: float
    opened_at: datetime
    age_days: int
    profit: float
    swap: float
    fee: float
    loss_amount: float  # positive value: how much closing this position costs


def _find_candidates(now_utc: datetime, min_age_days: int) -> List[CleanupCandidate]:
    """Return all open positions that are stale and currently in net loss."""
    positions = mt5.positions_get()
    if positions is None:
        raise RuntimeError(f"Failed to get open positions: {mt5.last_error()}")

    cutoff = now_utc - timedelta(days=min_age_days)
    candidates: List[CleanupCandidate] = []

    for pos in positions:
        opened_at = datetime.fromtimestamp(int(pos.time), tz=timezone.utc)
        if opened_at > cutoff:
            continue

        profit = float(pos.profit)
        swap = float(pos.swap)
        fee = _get_position_fee(float(pos.volume))
        net = profit + swap - fee
        loss_amount = round(-net, 2)

        if loss_amount <= 0:
            continue  # not losing

        age_days = int((now_utc - opened_at).total_seconds() // 86400)
        candidates.append(CleanupCandidate(
            ticket=int(pos.ticket),
            symbol=str(pos.symbol),
            position_type=int(pos.type),
            volume=float(pos.volume),
            opened_at=opened_at,
            age_days=age_days,
            profit=profit,
            swap=swap,
            fee=fee,
            loss_amount=loss_amount,
        ))

    # Sort: oldest first, then smallest loss (maximises number of positions cleared)
    candidates.sort(key=lambda c: (c.opened_at, c.loss_amount))
    return candidates


def _greedy_select(candidates: List[CleanupCandidate], budget: float) -> List[CleanupCandidate]:
    """Greedily pick positions to close without exceeding budget."""
    selected: List[CleanupCandidate] = []
    remaining = budget
    for c in candidates:
        if c.loss_amount <= remaining:
            selected.append(c)
            remaining -= c.loss_amount
    return selected


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _ensure_log_schema(log_file: Path) -> None:
    if not log_file.exists():
        return
    with open(log_file, "r", newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if rows and rows[0] == LOG_HEADERS:
        return
    # Rewrite with correct headers (pad/truncate rows)
    with open(log_file, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(LOG_HEADERS)
        for row in rows[1:]:
            padded = list(row[: len(LOG_HEADERS)])
            padded += [""] * (len(LOG_HEADERS) - len(padded))
            writer.writerow(padded)


def _log_action(
    *,
    service_folder: Optional[Path],
    now_utc: datetime,
    week_key: str,
    weekly_profit: float,
    min_income: float,
    surplus: float,
    candidate: Optional[CleanupCandidate],
    budget_before: float,
    budget_after: float,
    closed: bool,
    message: str,
) -> None:
    if service_folder is None:
        return
    log_dir = service_folder / "trade_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_FILE
    _ensure_log_schema(log_file)
    file_exists = log_file.exists()

    with open(log_file, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if not file_exists:
            writer.writerow(LOG_HEADERS)
        writer.writerow([
            now_utc.isoformat(),
            str(_get_dry_run()),
            week_key,
            f"{weekly_profit:.2f}",
            f"{min_income:.2f}",
            f"{surplus:.2f}",
            candidate.symbol if candidate else "",
            candidate.ticket if candidate else "",
            f"{candidate.volume:.2f}" if candidate else "",
            candidate.opened_at.isoformat() if candidate else "",
            candidate.age_days if candidate else "",
            f"{candidate.profit:.2f}" if candidate else "",
            f"{candidate.swap:.2f}" if candidate else "",
            f"{candidate.fee:.2f}" if candidate else "",
            f"{candidate.loss_amount:.2f}" if candidate else "",
            f"{budget_before:.2f}",
            f"{budget_after:.2f}",
            str(closed),
            message,
        ])


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_weekly_surplus_cleanup_strategy_if_due(
    account_info: Optional[dict[str, Any]] = None,
) -> None:
    """Evaluate and execute the weekly surplus cleanup strategy when due."""
    global _LAST_EVALUATED_WEEK_KEY

    if not _get_enabled():
        return

    now_utc = datetime.now(tz=timezone.utc)
    run_weekday = _get_run_weekday()
    run_hour = _get_run_hour_utc()

    if not _is_trigger_time(now_utc, run_weekday, run_hour):
        return

    week_key = _get_iso_week_key(now_utc)

    if _LAST_EVALUATED_WEEK_KEY == week_key:
        return

    service_folder = _get_service_folder()
    if _get_persisted_last_run_week_key(service_folder) == week_key:
        _LAST_EVALUATED_WEEK_KEY = week_key
        return

    # Mark as evaluated immediately to prevent re-entry within same cycle
    _LAST_EVALUATED_WEEK_KEY = week_key
    _persist_last_run_week_key(service_folder, week_key)

    dry_run = _get_dry_run()
    weekday_name = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"][run_weekday]
    print(f"\n📅 Weekly surplus cleanup strategy ({weekday_name} {now_utc.strftime('%Y-%m-%d %H:%M')} UTC)")
    print(f"   Týden: {week_key}  |  dry_run={dry_run}")

    try:
        # --- Swap block guard ---
        if _is_in_restricted_trading_hours(now_utc):
            window = get_swap_block_window(now_utc=now_utc)
            msg = (
                f"Swap rollover block "
                f"{window.start_utc.strftime('%H:%M')}-{window.end_utc.strftime('%H:%M')} UTC, cleanup skipped"
            )
            print(f"   ⏸️  {msg}")
            _log_action(
                service_folder=service_folder,
                now_utc=now_utc,
                week_key=week_key,
                weekly_profit=0.0,
                min_income=0.0,
                surplus=0.0,
                candidate=None,
                budget_before=0.0,
                budget_after=0.0,
                closed=False,
                message=msg,
            )
            return

        # --- Compute weekly realized profit ---
        week_start_utc = _get_week_start_utc(now_utc)
        closing_entries = _get_closing_entries()
        deals = _get_deals_between(week_start_utc, now_utc)
        weekly_profit = _calculate_weekly_realized_profit(deals, closing_entries)

        min_income = _get_min_income_usd()
        surplus = round(weekly_profit - min_income, 2)

        print(f"   Týdenní profit:   {weekly_profit:+.2f} USD")
        print(f"   Minimální příjem: {min_income:.2f} USD")
        print(f"   Přebytek (budget):{surplus:+.2f} USD")

        if surplus <= 0:
            msg = f"Týdenní profit {weekly_profit:.2f} USD nepřesahuje minimum {min_income:.2f} USD – cleanup přeskočen"
            print(f"   ⚠️  {msg}")
            _log_action(
                service_folder=service_folder,
                now_utc=now_utc,
                week_key=week_key,
                weekly_profit=weekly_profit,
                min_income=min_income,
                surplus=surplus,
                candidate=None,
                budget_before=surplus,
                budget_after=surplus,
                closed=False,
                message=msg,
            )
            return

        # --- Find stale losing positions ---
        min_age_days = _get_min_position_age_days()
        candidates = _find_candidates(now_utc, min_age_days)

        if not candidates:
            msg = f"Žádné ztratové pozice starší {min_age_days} dní"
            print(f"   ℹ️  {msg}")
            _log_action(
                service_folder=service_folder,
                now_utc=now_utc,
                week_key=week_key,
                weekly_profit=weekly_profit,
                min_income=min_income,
                surplus=surplus,
                candidate=None,
                budget_before=surplus,
                budget_after=surplus,
                closed=False,
                message=msg,
            )
            return

        print(f"   Kandidátů na uzavření: {len(candidates)}")
        for c in candidates:
            print(f"     • #{c.ticket} {c.symbol} ({c.age_days}d) ztráta={c.loss_amount:.2f} USD")

        # --- Greedy selection ---
        to_close = _greedy_select(candidates, surplus)

        if not to_close:
            msg = (
                f"Žádná pozice se nevejde do budgetu {surplus:.2f} USD "
                f"(nejmenší ztráta = {candidates[0].loss_amount:.2f} USD)"
            )
            print(f"   ℹ️  {msg}")
            _log_action(
                service_folder=service_folder,
                now_utc=now_utc,
                week_key=week_key,
                weekly_profit=weekly_profit,
                min_income=min_income,
                surplus=surplus,
                candidate=None,
                budget_before=surplus,
                budget_after=surplus,
                closed=False,
                message=msg,
            )
            return

        print(f"   Vybráno k uzavření: {len(to_close)} pozice")

        # --- Close selected positions ---
        budget = surplus
        for candidate in to_close:
            budget_before = budget
            budget_after = round(budget - candidate.loss_amount, 2)

            action = "BUY" if candidate.position_type == mt5.POSITION_TYPE_BUY else "SELL"
            print(
                f"\n   🔒 {'[DRY RUN] ' if dry_run else ''}Uzavírám #{candidate.ticket} "
                f"{candidate.symbol} {action} {candidate.volume:.2f}lot "
                f"| ztráta={candidate.loss_amount:.2f} USD | věk={candidate.age_days}d"
            )

            if dry_run:
                closed = False
                msg = "dry_run – pozice by byla uzavřena"
            else:
                closed = close_position_by_ticket(
                    position_ticket=candidate.ticket,
                    symbol=candidate.symbol,
                    position_type=candidate.position_type,
                    volume=candidate.volume,
                    comment="weekly_surplus_cleanup",
                    magic=0,
                )
                msg = "pozice uzavřena" if closed else "uzavření selhalo"
                if closed:
                    budget = budget_after
                    print(f"   ✅ Uzavřeno. Zbývající budget: {budget:.2f} USD")
                else:
                    print(f"   ❌ Uzavření selhalo: {candidate.symbol} #{candidate.ticket}")

            _log_action(
                service_folder=service_folder,
                now_utc=now_utc,
                week_key=week_key,
                weekly_profit=weekly_profit,
                min_income=min_income,
                surplus=surplus,
                candidate=candidate,
                budget_before=budget_before,
                budget_after=budget_after if closed or dry_run else budget_before,
                closed=closed,
                message=msg,
            )

        print(f"\n   ✅ Weekly cleanup hotov. Zbývající budget: {budget:.2f} USD")

    except Exception as exc:
        print(f"   ❌ Chyba v weekly surplus cleanup: {exc}")
        _log_action(
            service_folder=service_folder,
            now_utc=now_utc,
            week_key=week_key,
            weekly_profit=0.0,
            min_income=0.0,
            surplus=0.0,
            candidate=None,
            budget_before=0.0,
            budget_after=0.0,
            closed=False,
            message=f"exception: {exc}",
        )
