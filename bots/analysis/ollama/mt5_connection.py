"""Shared MetaTrader 5 connection helpers."""

from __future__ import annotations

from typing import Optional

import MetaTrader5 as mt5


def initialize_mt5(
	*,
	login: Optional[int] = None,
	password: Optional[str] = None,
	server: Optional[str] = None,
) -> None:
	"""Initialize the MT5 connection, optionally with explicit credentials."""
	if login and password and server:
		ok = mt5.initialize(login=login, password=password, server=server)
	else:
		ok = mt5.initialize()

	if not ok:
		raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")


def shutdown_mt5() -> None:
	"""Close the MT5 connection."""
	mt5.shutdown()