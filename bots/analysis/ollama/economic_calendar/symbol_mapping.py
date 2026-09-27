from __future__ import annotations

import re

from .models import SymbolExposure


_FOREX = {"EUR", "USD", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"}
_KNOWN_ASSETS = {"XAUUSD": ("USD", "GOLD"), "XAGUSD": ("USD", "SILVER"), "BTCUSD": ("USD", "CRYPTO"), "ETHUSD": ("USD", "CRYPTO")}


def normalize_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(symbol).upper().split("_")[0])


def get_symbol_exposure(symbol: str, configured_map: dict[str, list[str]] | None = None) -> SymbolExposure:
    normalized = normalize_symbol(symbol)
    configured = (configured_map or {}).get(normalized)
    if configured:
        currencies = tuple(value for value in configured if value in _FOREX)
        assets = tuple(value for value in configured if value not in _FOREX)
        return SymbolExposure(symbol, normalized, currencies, assets, "MAPPED" if currencies else "PARTIAL")
    if normalized in _KNOWN_ASSETS:
        currency, asset = _KNOWN_ASSETS[normalized]
        return SymbolExposure(symbol, normalized, (currency,), (asset,), "PARTIAL")
    if len(normalized) == 6 and normalized[:3] in _FOREX and normalized[3:] in _FOREX:
        return SymbolExposure(symbol, normalized, (normalized[:3], normalized[3:]), (), "MAPPED")
    return SymbolExposure(symbol, normalized, (), (), "UNMAPPED")