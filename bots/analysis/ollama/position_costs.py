"""Helpers for costs recorded in MetaTrader position deal history."""

from __future__ import annotations

from typing import Any

import MetaTrader5 as mt5


def get_position_charged_costs(position: Any) -> float:
	position_id = int(
		getattr(position, "identifier", 0) or getattr(position, "ticket", 0) or 0
	)
	if position_id <= 0:
		raise ValueError("Cannot load deal costs for a position without a valid identifier")

	deals = mt5.history_deals_get(position=position_id)
	if deals is None:
		raise RuntimeError(
			f"Failed to get deal costs for position {position_id}: {mt5.last_error()}"
		)
	if not deals:
		raise RuntimeError(f"No deal history returned for position {position_id}")

	return round(
		sum(
			float(getattr(deal, "commission", 0.0) or 0.0)
			+ float(getattr(deal, "fee", 0.0) or 0.0)
			for deal in deals
		),
		2,
	)
