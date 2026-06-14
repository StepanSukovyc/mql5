from __future__ import annotations

import os
from typing import List, Optional


def get_float_env(name: str, default: float, *, minimum: Optional[float] = None) -> float:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		value = float(raw)
		if minimum is not None and value < minimum:
			raise ValueError
		return value
	except (TypeError, ValueError):
		return default


def get_bool_env(name: str, default: bool) -> bool:
	raw = os.getenv(name)
	if raw is None:
		return default
	return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_int_env(name: str, default: int, *, minimum: Optional[int] = None) -> int:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		value = int(raw)
		if minimum is not None and value < minimum:
			raise ValueError
		return value
	except (TypeError, ValueError):
		return default


def parse_csv_env(name: str, default: str) -> List[str]:
	raw = os.getenv(name, default)
	return [item.strip() for item in raw.split(",") if item.strip()]