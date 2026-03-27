"""Shared MetaTrader 5 open-position helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List

import MetaTrader5 as mt5

from mt5_symbols import get_current_price


def get_open_positions() -> List[Dict]:
	"""Return all open MT5 positions serialized for downstream consumers."""
	positions = mt5.positions_get()
	if positions is None:
		raise RuntimeError(f"Failed to get positions: {mt5.last_error()}")

	open_positions = []
	for pos in positions:
		current_price = get_current_price(pos.symbol, default=float(pos.price_open))
		open_positions.append(
			{
				"symbol": pos.symbol,
				"type": "BUY" if pos.type == 0 else "SELL",
				"open_time": datetime.fromtimestamp(pos.time, tz=timezone.utc).isoformat(),
				"volume": float(pos.volume),
				"open_price": float(pos.price_open),
				"current_price": current_price,
				"pnl": float(pos.profit),
				"swap": float(pos.swap),
			}
		)

	return open_positions