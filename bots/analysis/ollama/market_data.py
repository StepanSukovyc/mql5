"""Shared market data collection utilities for MT5.

This module provides common functions for fetching and processing
market data from MetaTrader 5, including technical indicators (RSI, MA).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import MetaTrader5 as mt5
from mt5_symbols import get_current_price


def simple_moving_average(values: List[float], period: int) -> List[Optional[float]]:
    """Return SMA values; non-computable positions are None."""
    if period <= 0:
        raise ValueError("MA period must be > 0")

    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out

    rolling_sum = sum(values[:period])
    out[period - 1] = rolling_sum / period

    for idx in range(period, len(values)):
        rolling_sum += values[idx]
        rolling_sum -= values[idx - period]
        out[idx] = rolling_sum / period

    return out


def rsi_wilder(values: List[float], period: int) -> List[Optional[float]]:
    """Return RSI values by Wilder smoothing; non-computable positions are None."""
    if period <= 0:
        raise ValueError("RSI period must be > 0")

    out: List[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = abs(min(delta, 0.0))
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))

    return out


def to_iso_utc(unix_timestamp: int) -> str:
    """Convert Unix timestamp to ISO UTC string."""
    return datetime.fromtimestamp(int(unix_timestamp), tz=timezone.utc).isoformat()


def candle_rows_to_json_rows(rows: Iterable[object]) -> List[Dict[str, object]]:
    """Convert MT5 candle data to JSON-serializable format."""
    output: List[Dict[str, object]] = []
    for row in rows:
        output.append(
            {
                "time": to_iso_utc(row["time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "tick_volume": int(row["tick_volume"]),
                "spread": int(row["spread"]),
                "real_volume": int(row["real_volume"]),
            }
        )
    return output


def indicator_rows(
    candle_rows: Iterable[object], ma_period: int, rsi_period: int
) -> Dict[str, List[Dict[str, object]]]:
    """Calculate RSI and MA indicators from candle data."""
    rows = list(candle_rows)
    closes = [float(x["close"]) for x in rows]
    times = [to_iso_utc(x["time"]) for x in rows]

    ma_values = simple_moving_average(closes, period=ma_period)
    rsi_values = rsi_wilder(closes, period=rsi_period)

    ma_series: List[Dict[str, object]] = []
    rsi_series: List[Dict[str, object]] = []

    for ts, ma_value, rsi_value in zip(times, ma_values, rsi_values):
        if ma_value is not None:
            ma_series.append({"time": ts, "value": round(ma_value, 6)})
        if rsi_value is not None:
            rsi_series.append({"time": ts, "value": round(rsi_value, 6)})

    return {"rsi": rsi_series, "ma": ma_series}


def get_symbols(suffix: str, blacklist: Optional[Iterable[str]] = None) -> List[str]:
    """Get MT5 symbols filtered by suffix and blacklist patterns."""
    symbols = mt5.symbols_get()
    if symbols is None:
        err = mt5.last_error()
        raise RuntimeError(f"mt5.symbols_get failed: {err}")

    normalized_suffix = (suffix or "").strip().lower()
    blacklist_patterns = [item.strip().lower() for item in (blacklist or []) if item and item.strip()]

    filtered_symbols: List[str] = []
    for symbol in symbols:
        symbol_name = symbol.name
        symbol_lower = symbol_name.lower()

        if normalized_suffix and not symbol_lower.endswith(normalized_suffix):
            continue

        if any(fnmatch(symbol_lower, pattern) for pattern in blacklist_patterns):
            continue

        filtered_symbols.append(symbol_name)

    return sorted(filtered_symbols)


def copy_rates(symbol: str, timeframe: int, date_from: datetime, date_to: datetime):
    """Copy MT5 rate data for specified symbol and timeframe."""
    data = mt5.copy_rates_range(symbol, timeframe, date_from, date_to)
    if data is None:
        err = mt5.last_error()
        raise RuntimeError(
            f"copy_rates_range failed for {symbol}, timeframe={timeframe}: {err}"
        )
    return data


def collect_symbol_payload(
    symbol: str, 
    lookback_periods: int, 
    rsi_period: int, 
    ma_period: int
) -> Dict[str, object]:
    """
    Collect candles and oscillators for all timeframes.
    
    Args:
        symbol: Trading symbol (e.g., EURUSD_ecn)
        lookback_periods: Number of periods to fetch for each timeframe
        rsi_period: RSI calculation period
        ma_period: MA calculation period
    
    Returns:
        Dict with symbol data including candles and oscillators
    """
    timeframes = {
        "1h": mt5.TIMEFRAME_H1,
        "4h": mt5.TIMEFRAME_H4,
        "day": mt5.TIMEFRAME_D1,
        "week": mt5.TIMEFRAME_W1,
        "month": mt5.TIMEFRAME_MN1,
    }
    
    date_to = datetime.now(tz=timezone.utc)
    date_from = date_to - timedelta(days=730)  # 2 years to be safe for all timeframes
    
    payload = {
        "symbol": symbol,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "lookback_periods": lookback_periods,
        "current_price": None,
        "candles": {},
        "oscillators": {},
    }
    
    # Get current price
    payload["current_price"] = get_current_price(symbol)
    
    # Fetch data for each timeframe
    for tf_name, tf_value in timeframes.items():
        try:
            rates = copy_rates(symbol, tf_value, date_from, date_to)
            
            # Take only last N periods
            rates = rates[-lookback_periods:] if len(rates) > lookback_periods else rates
            
            candles = candle_rows_to_json_rows(rates)
            oscillators = indicator_rows(rates, ma_period=ma_period, rsi_period=rsi_period)
            
            payload["candles"][tf_name] = candles
            payload["oscillators"][tf_name] = oscillators
            
        except Exception as exc:
            print(f"[{symbol}] Warning: Failed to fetch {tf_name} data: {exc}")
            payload["candles"][tf_name] = []
            payload["oscillators"][tf_name] = {"rsi": [], "ma": []}
    
    return payload
