"""Hourly MT5 market data collector.

The script starts immediately and then repeats every N seconds
(default 3600 = one hour).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import MetaTrader5 as mt5


def _load_dotenv(dotenv_path: Path) -> None:
	"""Load .env values into process env if keys are not already set."""
	if not dotenv_path.exists():
		return

	for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, value = line.split("=", 1)
		key = key.strip()
		value = value.strip().strip('"').strip("'")
		if key and key not in os.environ:
			os.environ[key] = value


def _to_bool(value: str, default: bool = False) -> bool:
	if value is None:
		return default
	normalized = value.strip().lower()
	return normalized in {"1", "true", "yes", "y", "on"}


@dataclass
class Config:
	service_dest_folder: Path
	symbol_suffix: str
	lookback_periods: int
	run_interval_seconds: int
	rsi_period: int
	ma_period: int
	pretty_json: bool
	mt5_login: Optional[int]
	mt5_password: Optional[str]
	mt5_server: Optional[str]

	@classmethod
	def from_env(cls) -> "Config":
		base_dir = Path(__file__).resolve().parent
		_load_dotenv(base_dir / ".env")
		_load_dotenv(base_dir.parent / ".env")
		_load_dotenv(Path.cwd() / ".env")

		dest = os.getenv("SERVICE_DEST_FOLDER")
		if not dest:
			raise ValueError("Missing SERVICE_DEST_FOLDER in .env or environment.")

		mt5_login_raw = os.getenv("MT5_LOGIN")
		mt5_login = int(mt5_login_raw) if mt5_login_raw else None

		return cls(
			service_dest_folder=Path(dest),
			symbol_suffix=os.getenv("MT5_SYMBOL_SUFFIX", "_ecn"),
			lookback_periods=int(os.getenv("LOOKBACK_PERIODS", "30")),
			run_interval_seconds=int(os.getenv("RUN_INTERVAL_SECONDS", "3600")),
			rsi_period=int(os.getenv("RSI_PERIOD", "14")),
			ma_period=int(os.getenv("MA_PERIOD", "20")),
			pretty_json=_to_bool(os.getenv("PRETTY_JSON", "true"), default=True),
			mt5_login=mt5_login,
			mt5_password=os.getenv("MT5_PASSWORD"),
			mt5_server=os.getenv("MT5_SERVER"),
		)


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
	return datetime.fromtimestamp(int(unix_timestamp), tz=timezone.utc).isoformat()


def candle_rows_to_json_rows(rows: Iterable[object]) -> List[Dict[str, object]]:
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


def get_symbols(suffix: str) -> List[str]:
	symbols = mt5.symbols_get()
	if symbols is None:
		err = mt5.last_error()
		raise RuntimeError(f"mt5.symbols_get failed: {err}")

	suffix_lower = suffix.lower()
	return sorted([s.name for s in symbols if s.name.lower().endswith(suffix_lower)])


def copy_rates(symbol: str, timeframe: int, date_from: datetime, date_to: datetime):
	data = mt5.copy_rates_range(symbol, timeframe, date_from, date_to)
	if data is None:
		err = mt5.last_error()
		raise RuntimeError(
			f"copy_rates_range failed for {symbol}, timeframe={timeframe}: {err}"
		)
	return data


def collect_symbol_payload(symbol: str, cfg: Config) -> Dict[str, object]:
	"""Collect candles and oscillators for all timeframes (last N periods each)."""
	# Fetch last N periods for each timeframe
	timeframes = {
		"1h": mt5.TIMEFRAME_H1,
		"4h": mt5.TIMEFRAME_H4,
		"day": mt5.TIMEFRAME_D1,
		"week": mt5.TIMEFRAME_W1,
		"month": mt5.TIMEFRAME_MN1,
	}
	
	# Use a lookback window that's large enough to capture N periods for all timeframes
	# For monthly data, 365 days = ~12 months; for weekly ~52 weeks; for hourly ~month of hours
	date_to = datetime.now(tz=timezone.utc)
	date_from = date_to - timedelta(days=730)  # 2 years to be safe for all timeframes
	
	payload = {
		"symbol": symbol,
		"generated_at": datetime.now(tz=timezone.utc).isoformat(),
		"lookback_periods": cfg.lookback_periods,
		"current_price": None,
		"candles": {},
		"oscillators": {},
	}
	
	# Get current price
	tick = mt5.symbol_info_tick(symbol)
	payload["current_price"] = float(tick.bid) if tick else None
	
	# Fetch data for each timeframe
	for tf_name, tf_value in timeframes.items():
		try:
			rates = copy_rates(symbol, tf_value, date_from, date_to)
			
			# Take only last N periods
			rates = rates[-cfg.lookback_periods:] if len(rates) > cfg.lookback_periods else rates
			
			candles = candle_rows_to_json_rows(rates)
			oscillators = indicator_rows(rates, ma_period=cfg.ma_period, rsi_period=cfg.rsi_period)
			
			payload["candles"][tf_name] = candles
			payload["oscillators"][tf_name] = oscillators
			
		except Exception as exc:
			print(f"[{symbol}] Warning: Failed to fetch {tf_name} data: {exc}")
			payload["candles"][tf_name] = []
			payload["oscillators"][tf_name] = {"rsi": [], "ma": []}
	
	return payload


def write_symbol_file(dest_folder: Path, symbol: str, payload: Dict[str, object], pretty: bool) -> None:
	dest_folder.mkdir(parents=True, exist_ok=True)
	out_path = dest_folder / f"{symbol}.json"
	if pretty:
		out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
	else:
		out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def mt5_initialize(cfg: Config) -> None:
	ok: bool
	if cfg.mt5_login and cfg.mt5_password and cfg.mt5_server:
		ok = mt5.initialize(
			login=cfg.mt5_login,
			password=cfg.mt5_password,
			server=cfg.mt5_server,
		)
	else:
		ok = mt5.initialize()

	if not ok:
		raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")


def run_cycle(cfg: Config) -> None:
	symbols = get_symbols(cfg.symbol_suffix)
	if not symbols:
		print(f"No symbols found for suffix '{cfg.symbol_suffix}'.")
		return

	print(f"Found {len(symbols)} symbols. Starting export to {cfg.service_dest_folder}")

	ok_count = 0
	err_count = 0
	for symbol in symbols:
		try:
			payload = collect_symbol_payload(symbol, cfg)
			write_symbol_file(cfg.service_dest_folder, symbol, payload, pretty=cfg.pretty_json)
			ok_count += 1
		except Exception as exc:  # pylint: disable=broad-except
			err_count += 1
			print(f"[{symbol}] ERROR: {exc}")

	print(f"Cycle done. Success={ok_count}, Errors={err_count}, Total={len(symbols)}")


def run_scheduler(cfg: Config) -> None:
	should_stop = {"value": False}

	def _handle_stop(signum, _frame):
		should_stop["value"] = True
		print(f"\nReceived signal {signum}. Exiting gracefully...")

	signal.signal(signal.SIGINT, _handle_stop)
	if hasattr(signal, "SIGTERM"):
		signal.signal(signal.SIGTERM, _handle_stop)

	started_at = time.time()
	cycle_idx = 0

	while not should_stop["value"]:
		cycle_idx += 1
		cycle_started = datetime.now(tz=timezone.utc).isoformat()
		print(f"\n=== Cycle #{cycle_idx} started at {cycle_started} ===")

		try:
			run_cycle(cfg)
		except Exception as exc:  # pylint: disable=broad-except
			print(f"Cycle failed: {exc}")

		next_run_at = started_at + (cycle_idx * cfg.run_interval_seconds)
		sleep_seconds = max(0.0, next_run_at - time.time())

		if should_stop["value"]:
			break

		print(f"Waiting {int(sleep_seconds)} seconds for next cycle...")
		
		# Interruptible sleep - check should_stop every second
		sleep_remaining = sleep_seconds
		while sleep_remaining > 0 and not should_stop["value"]:
			time.sleep(min(1.0, sleep_remaining))
			sleep_remaining -= 1.0


def main() -> int:
	try:
		cfg = Config.from_env()
	except Exception as exc:  # pylint: disable=broad-except
		print(f"Config error: {exc}")
		return 2

	try:
		mt5_initialize(cfg)
		print("Connected to MetaTrader 5.")
		run_scheduler(cfg)
		return 0
	except Exception as exc:  # pylint: disable=broad-except
		print(f"Fatal error: {exc}")
		return 1
	finally:
		mt5.shutdown()
		print("MetaTrader 5 connection closed.")


if __name__ == "__main__":
	sys.exit(main())
