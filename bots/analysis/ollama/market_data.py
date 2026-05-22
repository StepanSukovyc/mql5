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
from instrument_utils import is_cfd_symbol
from mt5_symbols import get_current_price, get_symbol_info, get_symbol_tick


CFD_BLACKLIST_TOKEN = "__cfd__"


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


def exponential_moving_average(values: List[float], period: int) -> List[Optional[float]]:
    """Return EMA values; non-computable positions are None."""
    if period <= 0:
        raise ValueError("EMA period must be > 0")

    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out

    multiplier = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    ema_prev = seed

    for idx in range(period, len(values)):
        ema_prev = ((values[idx] - ema_prev) * multiplier) + ema_prev
        out[idx] = ema_prev

    return out


def bollinger_bands(
    values: List[float], period: int, stddev_multiplier: float = 2.0
) -> tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """Return Bollinger middle/upper/lower band values; non-computable positions are None."""
    if period <= 0:
        raise ValueError("Bollinger period must be > 0")

    middle: List[Optional[float]] = [None] * len(values)
    upper: List[Optional[float]] = [None] * len(values)
    lower: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return middle, upper, lower

    rolling_sum = sum(values[:period])
    rolling_squares = sum(value * value for value in values[:period])

    for idx in range(period - 1, len(values)):
        if idx >= period:
            outgoing = values[idx - period]
            incoming = values[idx]
            rolling_sum += incoming - outgoing
            rolling_squares += (incoming * incoming) - (outgoing * outgoing)

        mean = rolling_sum / period
        variance = max((rolling_squares / period) - (mean * mean), 0.0)
        stddev = variance ** 0.5
        middle[idx] = mean
        upper[idx] = mean + (stddev_multiplier * stddev)
        lower[idx] = mean - (stddev_multiplier * stddev)

    return middle, upper, lower


def volume_weighted_average_price(
    highs: List[float], lows: List[float], closes: List[float], volumes: List[float]
) -> List[Optional[float]]:
    """Return cumulative VWAP values from typical price and volume; empty-volume rows reuse the prior VWAP."""
    if not (len(highs) == len(lows) == len(closes) == len(volumes)):
        raise ValueError("highs, lows, closes, and volumes must have the same length")

    out: List[Optional[float]] = [None] * len(closes)
    cumulative_tpv = 0.0
    cumulative_volume = 0.0

    for idx, (high, low, close, volume) in enumerate(zip(highs, lows, closes, volumes)):
        typical_price = (high + low + close) / 3.0
        volume_value = max(float(volume), 0.0)
        cumulative_tpv += typical_price * volume_value
        cumulative_volume += volume_value
        if cumulative_volume > 0:
            out[idx] = cumulative_tpv / cumulative_volume
        elif idx > 0:
            out[idx] = out[idx - 1]

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


def average_true_range(
    highs: List[float], lows: List[float], closes: List[float], period: int
) -> List[Optional[float]]:
    """Return ATR values using Wilder smoothing; non-computable positions are None."""
    if period <= 0:
        raise ValueError("ATR period must be > 0")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, and closes must have the same length")

    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out

    true_ranges: List[float] = [0.0]
    for idx in range(1, len(closes)):
        high = highs[idx]
        low = lows[idx]
        prev_close = closes[idx - 1]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    atr_prev = sum(true_ranges[1 : period + 1]) / period
    out[period] = atr_prev

    for idx in range(period + 1, len(closes)):
        atr_prev = ((atr_prev * (period - 1)) + true_ranges[idx]) / period
        out[idx] = atr_prev

    return out


def average_directional_index(
    highs: List[float], lows: List[float], closes: List[float], period: int
) -> List[Optional[float]]:
    """Return ADX values using Wilder smoothing; non-computable positions are None."""
    if period <= 0:
        raise ValueError("ADX period must be > 0")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, and closes must have the same length")

    length = len(closes)
    out: List[Optional[float]] = [None] * length
    if length <= (period * 2):
        return out

    true_ranges: List[float] = [0.0]
    plus_dm: List[float] = [0.0]
    minus_dm: List[float] = [0.0]
    for idx in range(1, length):
        up_move = highs[idx] - highs[idx - 1]
        down_move = lows[idx - 1] - lows[idx]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(
            max(
                highs[idx] - lows[idx],
                abs(highs[idx] - closes[idx - 1]),
                abs(lows[idx] - closes[idx - 1]),
            )
        )

    tr_smooth = sum(true_ranges[1 : period + 1])
    plus_dm_smooth = sum(plus_dm[1 : period + 1])
    minus_dm_smooth = sum(minus_dm[1 : period + 1])

    dx_values: List[Optional[float]] = [None] * length
    for idx in range(period, length):
        if idx > period:
            tr_smooth = tr_smooth - (tr_smooth / period) + true_ranges[idx]
            plus_dm_smooth = plus_dm_smooth - (plus_dm_smooth / period) + plus_dm[idx]
            minus_dm_smooth = minus_dm_smooth - (minus_dm_smooth / period) + minus_dm[idx]

        if tr_smooth <= 0:
            dx_values[idx] = 0.0
            continue

        plus_di = 100.0 * (plus_dm_smooth / tr_smooth)
        minus_di = 100.0 * (minus_dm_smooth / tr_smooth)
        di_sum = plus_di + minus_di
        dx_values[idx] = 0.0 if di_sum <= 0 else 100.0 * abs(plus_di - minus_di) / di_sum

    seed_index = period * 2 - 1
    seed_values = [value for value in dx_values[period:seed_index + 1] if value is not None]
    if len(seed_values) < period:
        return out

    adx_prev = sum(seed_values) / period
    out[seed_index] = adx_prev

    for idx in range(seed_index + 1, length):
        current_dx = dx_values[idx]
        if current_dx is None:
            continue
        adx_prev = ((adx_prev * (period - 1)) + current_dx) / period
        out[idx] = adx_prev

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
    """Calculate RSI, SMA, EMA, ATR, ADX, Bollinger, and VWAP indicators from candle data."""
    rows = list(candle_rows)

    def _tick_volume(row: object) -> float:
        if isinstance(row, dict):
            return float(row.get("tick_volume", 0.0) or 0.0)
        dtype = getattr(row, "dtype", None)
        field_names = getattr(dtype, "names", None)
        if field_names and "tick_volume" in field_names:
            return float(row["tick_volume"])
        return 0.0

    closes = [float(x["close"]) for x in rows]
    highs = [float(x["high"]) for x in rows]
    lows = [float(x["low"]) for x in rows]
    volumes = [_tick_volume(x) for x in rows]
    times = [to_iso_utc(x["time"]) for x in rows]

    ma_values = simple_moving_average(closes, period=ma_period)
    ema20_values = exponential_moving_average(closes, period=20)
    ema50_values = exponential_moving_average(closes, period=50)
    ema200_values = exponential_moving_average(closes, period=200)
    rsi_values = rsi_wilder(closes, period=rsi_period)
    rsi2_values = rsi_wilder(closes, period=2)
    atr_values = average_true_range(highs, lows, closes, period=14)
    adx_values = average_directional_index(highs, lows, closes, period=14)
    bb_middle_values, bb_upper_values, bb_lower_values = bollinger_bands(closes, period=20, stddev_multiplier=2.0)
    vwap_values = volume_weighted_average_price(highs, lows, closes, volumes)

    ma_series: List[Dict[str, object]] = []
    rsi_series: List[Dict[str, object]] = []
    rsi2_series: List[Dict[str, object]] = []
    ema20_series: List[Dict[str, object]] = []
    ema50_series: List[Dict[str, object]] = []
    ema200_series: List[Dict[str, object]] = []
    atr_series: List[Dict[str, object]] = []
    adx_series: List[Dict[str, object]] = []
    bb_middle_series: List[Dict[str, object]] = []
    bb_upper_series: List[Dict[str, object]] = []
    bb_lower_series: List[Dict[str, object]] = []
    vwap_series: List[Dict[str, object]] = []

    for (
        ts,
        ma_value,
        rsi_value,
        rsi2_value,
        ema20_value,
        ema50_value,
        ema200_value,
        atr_value,
        adx_value,
        bb_middle_value,
        bb_upper_value,
        bb_lower_value,
        vwap_value,
    ) in zip(
        times,
        ma_values,
        rsi_values,
        rsi2_values,
        ema20_values,
        ema50_values,
        ema200_values,
        atr_values,
        adx_values,
        bb_middle_values,
        bb_upper_values,
        bb_lower_values,
        vwap_values,
    ):
        if ma_value is not None:
            ma_series.append({"time": ts, "value": round(ma_value, 6)})
        if rsi_value is not None:
            rsi_series.append({"time": ts, "value": round(rsi_value, 6)})
        if rsi2_value is not None:
            rsi2_series.append({"time": ts, "value": round(rsi2_value, 6)})
        if ema20_value is not None:
            ema20_series.append({"time": ts, "value": round(ema20_value, 6)})
        if ema50_value is not None:
            ema50_series.append({"time": ts, "value": round(ema50_value, 6)})
        if ema200_value is not None:
            ema200_series.append({"time": ts, "value": round(ema200_value, 6)})
        if atr_value is not None:
            atr_series.append({"time": ts, "value": round(atr_value, 6)})
        if adx_value is not None:
            adx_series.append({"time": ts, "value": round(adx_value, 6)})
        if bb_middle_value is not None:
            bb_middle_series.append({"time": ts, "value": round(bb_middle_value, 6)})
        if bb_upper_value is not None:
            bb_upper_series.append({"time": ts, "value": round(bb_upper_value, 6)})
        if bb_lower_value is not None:
            bb_lower_series.append({"time": ts, "value": round(bb_lower_value, 6)})
        if vwap_value is not None:
            vwap_series.append({"time": ts, "value": round(vwap_value, 6)})

    return {
        "rsi": rsi_series,
        "rsi2": rsi2_series,
        "ma": ma_series,
        "ema20": ema20_series,
        "ema50": ema50_series,
        "ema200": ema200_series,
        "atr14": atr_series,
        "adx14": adx_series,
        "bb_middle20": bb_middle_series,
        "bb_upper20": bb_upper_series,
        "bb_lower20": bb_lower_series,
        "vwap": vwap_series,
    }


def get_spread_snapshot(symbol: str) -> Dict[str, Optional[float]]:
    """Return current spread in raw price and points when available."""
    tick = get_symbol_tick(symbol)
    if tick is None:
        return {"bid": None, "ask": None, "spread_price": None, "spread_points": None}

    bid = float(tick.bid)
    ask = float(tick.ask)
    spread_price = ask - bid
    symbol_info = get_symbol_info(symbol)
    point = float(getattr(symbol_info, "point", 0.0) or 0.0) if symbol_info is not None else 0.0
    spread_points = (spread_price / point) if point > 0 else None
    return {
        "bid": bid,
        "ask": ask,
        "spread_price": round(spread_price, 8),
        "spread_points": round(spread_points, 2) if spread_points is not None else None,
    }


def get_symbols(suffix: str, blacklist: Optional[Iterable[str]] = None) -> List[str]:
    """Get MT5 symbols filtered by suffix and blacklist patterns."""
    symbols = mt5.symbols_get()
    if symbols is None:
        err = mt5.last_error()
        raise RuntimeError(f"mt5.symbols_get failed: {err}")

    normalized_suffix = (suffix or "").strip().lower()
    blacklist_patterns = [item.strip().lower() for item in (blacklist or []) if item and item.strip()]
    blacklist_cfd = CFD_BLACKLIST_TOKEN in blacklist_patterns
    if blacklist_cfd:
        blacklist_patterns = [pattern for pattern in blacklist_patterns if pattern != CFD_BLACKLIST_TOKEN]

    filtered_symbols: List[str] = []
    for symbol in symbols:
        symbol_name = symbol.name
        symbol_lower = symbol_name.lower()

        if normalized_suffix and not symbol_lower.endswith(normalized_suffix):
            continue

        if blacklist_cfd and is_cfd_symbol(symbol_name):
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
        "spread_snapshot": {},
        "candles": {},
        "oscillators": {},
    }
    
    # Get current price
    payload["current_price"] = get_current_price(symbol)
    payload["spread_snapshot"] = get_spread_snapshot(symbol)
    
    # Fetch data for each timeframe
    for tf_name, tf_value in timeframes.items():
        try:
            rates = copy_rates(symbol, tf_value, date_from, date_to)

            indicator_seed_periods = max(lookback_periods + 220, 260)
            indicator_rates = rates[-indicator_seed_periods:] if len(rates) > indicator_seed_periods else rates
            visible_rates = indicator_rates[-lookback_periods:] if len(indicator_rates) > lookback_periods else indicator_rates

            candles = candle_rows_to_json_rows(visible_rates)
            oscillators = indicator_rows(indicator_rates, ma_period=ma_period, rsi_period=rsi_period)

            if candles:
                first_visible_ts = candles[0]["time"]
                oscillators = {
                    key: [row for row in series if str(row.get("time")) >= first_visible_ts]
                    for key, series in oscillators.items()
                }
            
            payload["candles"][tf_name] = candles
            payload["oscillators"][tf_name] = oscillators
            
        except Exception as exc:
            print(f"[{symbol}] Warning: Failed to fetch {tf_name} data: {exc}")
            payload["candles"][tf_name] = []
            payload["oscillators"][tf_name] = {
                "rsi": [],
                "rsi2": [],
                "ma": [],
                "ema20": [],
                "ema50": [],
                "ema200": [],
                "atr14": [],
                "adx14": [],
                "bb_middle20": [],
                "bb_upper20": [],
                "bb_lower20": [],
                "vwap": [],
            }
    
    return payload
