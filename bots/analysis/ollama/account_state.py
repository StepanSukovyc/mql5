"""Shared MT5 account state helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import MetaTrader5 as mt5


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

	account_state: Dict[str, Any] = {
		"balance": float(account.balance),
		"equity": float(account.equity),
		"margin": float(account.margin),
		"margin_free": float(account.margin_free),
	}

	if include_timestamp:
		account_state["timestamp"] = datetime.now(tz=timezone.utc).isoformat()

	if include_margin_percent:
		balance = account_state["balance"]
		account_state["margin_percent"] = (account_state["margin_free"] / balance * 100) if balance > 0 else 0

	return account_state