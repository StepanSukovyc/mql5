"""Helpers for instrument classification and runtime risk configuration."""

from __future__ import annotations

import os
from fnmatch import fnmatch
from typing import Iterable, List, Optional

import MetaTrader5 as mt5

from mt5_symbols import get_symbol_info


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


def get_cfd_symbol_patterns() -> List[str]:
	"""Return optional wildcard patterns that should always be treated as CFD."""
	return _parse_csv(os.getenv("MT5_CFD_SYMBOL_PATTERNS"))


def is_cfd_symbol(symbol: str, patterns: Optional[Iterable[str]] = None) -> bool:
	"""Return True when symbol should use the CFD trading profile."""
	if not symbol or is_crypto_symbol(symbol):
		return False

	if symbol_matches_patterns(symbol, patterns or get_cfd_symbol_patterns()):
		return True

	symbol_info = get_symbol_info(symbol)
	if symbol_info is None:
		return False

	trade_calc_mode = getattr(symbol_info, "trade_calc_mode", None)
	cfd_calc_modes = {
		getattr(mt5, "SYMBOL_CALC_MODE_CFD", None),
		getattr(mt5, "SYMBOL_CALC_MODE_CFDINDEX", None),
		getattr(mt5, "SYMBOL_CALC_MODE_CFDLEVERAGE", None),
	}
	if trade_calc_mode in cfd_calc_modes:
		return True

	path = str(getattr(symbol_info, "path", "") or "").lower()
	description = str(getattr(symbol_info, "description", "") or "").lower()
	return "cfd" in path or "cfd" in description


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


def get_cfd_tp_max_distance_percent() -> float:
	"""Return the maximum fee-aware TP distance allowed for CFD trades."""
	return _parse_float_env("MT5_CFD_TP_MAX_DISTANCE_PERCENT", 3.0, minimum=0.01)


def get_cfd_min_net_profit_usd() -> float:
	"""Return minimum target net profit after modeled fees for CFD trades."""
	return _parse_float_env("MT5_CFD_MIN_NET_PROFIT_USD", 0.10, minimum=0.0)


def get_crypto_max_open_positions() -> int:
	"""Return maximum number of concurrent crypto positions allowed."""
	return _parse_int_env("MT5_CRYPTO_MAX_OPEN_POSITIONS", 1, minimum=0)


def is_crypto_full_tp_mode_allowed() -> bool:
	"""Return whether crypto may use full Gemini TP mode."""
	return _parse_bool_env("MT5_CRYPTO_ALLOW_FULL_TP_MODE", False)


def is_cfd_full_tp_mode_allowed() -> bool:
	"""Return whether CFD instruments may use fee-aware TP in full Gemini mode."""
	return _parse_bool_env("MT5_CFD_ALLOW_FULL_TP_MODE", True)


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