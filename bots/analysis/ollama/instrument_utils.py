"""Helpers for instrument classification and runtime risk configuration."""

from __future__ import annotations

import os
from fnmatch import fnmatch
from typing import Iterable, List, Optional


def _parse_csv(value: Optional[str]) -> List[str]:
	if not value:
		return []
	return [item.strip() for item in value.split(",") if item.strip()]


def _parse_float_env(name: str, default: float, *, minimum: float) -> float:
	raw = os.getenv(name)
	if raw is None:
		return default

	try:
		value = float(raw)
		if value < minimum:
			raise ValueError
		return value
	except (TypeError, ValueError):
		return default


def _parse_int_env(name: str, default: int, *, minimum: int) -> int:
	raw = os.getenv(name)
	if raw is None:
		return default

	try:
		value = int(raw)
		if value < minimum:
			raise ValueError
		return value
	except (TypeError, ValueError):
		return default


def _parse_bool_env(name: str, default: bool) -> bool:
	raw = os.getenv(name)
	if raw is None:
		return default
	return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_crypto_symbol_patterns() -> List[str]:
	"""Return wildcard patterns that identify crypto instruments."""
	configured = _parse_csv(os.getenv("MT5_CRYPTO_SYMBOL_PATTERNS"))
	if configured:
		return configured
	return ["BTCUSD*", "ETHUSD*", "LTCUSD*", "BCHUSD*"]


def symbol_matches_patterns(symbol: str, patterns: Optional[Iterable[str]] = None) -> bool:
	"""Return True when symbol matches at least one wildcard pattern."""
	if not symbol:
		return False

	symbol_lower = symbol.lower()
	for pattern in patterns or []:
		normalized = pattern.strip().lower()
		if normalized and fnmatch(symbol_lower, normalized):
			return True
	return False


def is_crypto_symbol(symbol: str, patterns: Optional[Iterable[str]] = None) -> bool:
	"""Return True when symbol should use the crypto trading profile."""
	return symbol_matches_patterns(symbol, patterns or get_crypto_symbol_patterns())


def get_base_prediction_threshold() -> float:
	"""Return the minimum BUY/SELL confidence for standard instruments."""
	return _parse_float_env("MT5_MIN_SIGNAL_PERCENT", 35.0, minimum=0.0)


def get_crypto_prediction_threshold() -> float:
	"""Return the stricter minimum BUY/SELL confidence for crypto instruments."""
	default_value = max(get_base_prediction_threshold(), 65.0)
	return _parse_float_env("MT5_CRYPTO_MIN_SIGNAL_PERCENT", default_value, minimum=0.0)


def get_crypto_lot_multiplier() -> float:
	"""Return multiplier applied to Gemini lot sizes for crypto trades."""
	return _parse_float_env("MT5_CRYPTO_LOT_MULTIPLIER", 0.25, minimum=0.01)


def get_crypto_tp_distance_percent() -> float:
	"""Return the maximum conservative TP distance used for crypto trades."""
	return _parse_float_env("MT5_CRYPTO_TP_DISTANCE_PERCENT", 2.0, minimum=0.01)


def get_crypto_max_open_positions() -> int:
	"""Return maximum number of concurrent crypto positions allowed."""
	return _parse_int_env("MT5_CRYPTO_MAX_OPEN_POSITIONS", 1, minimum=0)


def is_crypto_full_tp_mode_allowed() -> bool:
	"""Return whether crypto may use full Gemini TP mode."""
	return _parse_bool_env("MT5_CRYPTO_ALLOW_FULL_TP_MODE", False)


def get_symbol_prompt_guidance(symbol: str) -> str:
	"""Return extra AI prompt guidance for special instrument groups."""
	if not is_crypto_symbol(symbol):
		return ""

	return (
		"\n\nSPECIÁLNÍ REŽIM PRO CRYPTO:\n"
		"- Jde o crypto instrument s vyšší volatilitou a častějšími falešnými průrazy.\n"
		"- Pokud signál není výrazně silný a konzistentní napříč timeframe, preferuj HOLD.\n"
		"- Buď konzervativní při hodnocení BUY/SELL a počítej s vyšším spreadem a slippage."
	)