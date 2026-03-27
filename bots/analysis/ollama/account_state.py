"""Shared MT5 account state helpers."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict

import MetaTrader5 as mt5


DEFAULT_ACCOUNT_BALANCE_CAP = 5000.0


def get_account_balance_cap() -> float:
	"""Return the strategy balance cap configured via environment."""
	raw_value = os.getenv("TRADING_ACCOUNT_BALANCE_CAP", str(DEFAULT_ACCOUNT_BALANCE_CAP))
	try:
		value = float(raw_value)
		if value <= 0:
			return DEFAULT_ACCOUNT_BALANCE_CAP
		return value
	except (TypeError, ValueError):
		return DEFAULT_ACCOUNT_BALANCE_CAP


def get_effective_balance(balance: float, *, cap: float | None = None) -> float:
	"""Return the balance used by the strategy after applying the safety reserve cap."""
	if cap is None:
		cap = get_account_balance_cap()
	return min(float(balance), cap)


def get_balance_reserve(balance: float, *, cap: float | None = None) -> float:
	"""Return the part of balance kept outside strategy calculations as reserve."""
	if cap is None:
		cap = get_account_balance_cap()
	return max(float(balance) - cap, 0.0)


def get_effective_free_margin(
	balance: float,
	margin_free: float,
	*,
	cap: float | None = None,
) -> float:
	"""Return free margin reduced by any balance amount held in reserve above the cap."""
	if cap is None:
		cap = get_account_balance_cap()
	reserve = get_balance_reserve(balance, cap=cap)
	return max(float(margin_free) - reserve, 0.0)


def get_account_info_raw() -> Any:
	"""Return raw MT5 account info or raise when unavailable."""
	account = mt5.account_info()
	if account is None:
		raise RuntimeError(f"Failed to get account info: {mt5.last_error()}")
	return account


def get_account_login(default: str = "N/A") -> str:
	"""Return the active MT5 account login for logging purposes."""
	try:
		return str(get_account_info_raw().login)
	except RuntimeError:
		return default


def get_account_state(*, include_timestamp: bool = False, include_margin_percent: bool = False) -> Dict[str, Any]:
	"""Return the current MT5 account state with optional derived fields."""
	account = get_account_info_raw()
	raw_balance = float(account.balance)
	raw_margin_free = float(account.margin_free)
	balance_cap = get_account_balance_cap()
	balance_reserve = get_balance_reserve(raw_balance, cap=balance_cap)
	effective_balance = get_effective_balance(raw_balance, cap=balance_cap)
	effective_margin_free = get_effective_free_margin(raw_balance, raw_margin_free, cap=balance_cap)

	account_state: Dict[str, Any] = {
		"balance": effective_balance,
		"equity": float(account.equity),
		"margin": float(account.margin),
		"margin_free": effective_margin_free,
		"raw_balance": raw_balance,
		"raw_margin_free": raw_margin_free,
		"balance_cap": balance_cap,
		"balance_reserve": balance_reserve,
	}

	if include_timestamp:
		account_state["timestamp"] = datetime.now(tz=timezone.utc).isoformat()

	if include_margin_percent:
		balance = account_state["balance"]
		account_state["margin_percent"] = (account_state["margin_free"] / balance * 100) if balance > 0 else 0

	return account_state