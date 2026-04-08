"""Hourly MT5 market data collector.

The script starts immediately and then repeats every N seconds
(default 3600 = one hour).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import MetaTrader5 as mt5
from account_monitor import run_account_monitor
from trading_logic import run_trading_logic
from final_decision import make_final_trading_decision
from mt5_connection import initialize_mt5, shutdown_mt5
from ollama_service import ollama_service_loop
from swap_rollover import get_swap_block_window
from market_data import (
	collect_symbol_payload,
	get_symbols,
)


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


def write_symbol_file(dest_folder: Path, symbol: str, payload: Dict[str, object], pretty: bool) -> None:
	dest_folder.mkdir(parents=True, exist_ok=True)
	out_path = dest_folder / f"{symbol}.json"
	if pretty:
		out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
	else:
		out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
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
			payload = collect_symbol_payload(
				symbol, 
				cfg.lookback_periods, 
				cfg.rsi_period, 
				cfg.ma_period
			)
			write_symbol_file(cfg.service_dest_folder, symbol, payload, pretty=cfg.pretty_json)
			ok_count += 1
		except Exception as exc:  # pylint: disable=broad-except
			err_count += 1
			print(f"[{symbol}] ERROR: {exc}")

	print(f"Cycle done. Success={ok_count}, Errors={err_count}, Total={len(symbols)}")


def run_scheduler(cfg: Config, trading_trigger_event: threading.Event) -> None:
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
		# Check if trading logic was triggered by monitor
		if trading_trigger_event.is_set():
			print(f"\n🚀 Trading trigger signal received from monitor!")
			break
		
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
		
		# Interruptible sleep - check should_stop and trading_trigger every second
		sleep_remaining = sleep_seconds
		while sleep_remaining > 0 and not should_stop["value"]:
			if trading_trigger_event.is_set():
				print(f"🚀 Trading trigger signal received during sleep!")
				should_stop["value"] = True
				break
			time.sleep(min(1.0, sleep_remaining))
			sleep_remaining -= 1.0


def find_predictions_folder_for_current_hour(service_folder: Path) -> Optional[Path]:
	"""
	Find a predictions folder from the current hour (same hour in UTC).
	
	Current time example: 2026-03-05 19:45 UTC → look for folder 20260305_19*
	Folder example: 20260305_190000, 20260305_191500, etc.
	
	Args:
		service_folder: SERVICE_DEST_FOLDER path
	
	Returns:
		Path to predictions folder if found, None otherwise
	"""
	now = datetime.now(tz=timezone.utc)
	current_hour = now.hour
	current_date_str = now.strftime("%Y%m%d")
	
	# Look for folders matching pattern: YYYYMMDD_HH****
	hour_pattern = f"{current_date_str}_{current_hour:02d}"
	
	print(f"🔍 Looking for predictions from current hour ({hour_pattern})...")

	candidates: List[Path] = []
	for folder in service_folder.iterdir():
		if not folder.is_dir():
			continue
		
		folder_name = folder.name
		# Check if folder matches pattern: YYYYMMDD_HHMMSS
		if folder_name.startswith(hour_pattern) and len(folder_name) == 15 and folder_name[8] == "_":
			predikce_folder = folder / "predikce"
			if predikce_folder.exists() and any(predikce_folder.glob("*.json")):
				candidates.append(predikce_folder)

	if candidates:
		# Timestamp format YYYYMMDD_HHMMSS is lexicographically sortable.
		latest = max(candidates, key=lambda p: p.parent.name)
		print(f"✅ Found existing predictions in: {latest.parent.name}/predikce/")
		return latest
	
	print(f"⚠️  No predictions found for current hour {hour_pattern}")
	return None


def process_existing_predictions(predictions_folder: Path) -> bool:
	"""
	Process existing predictions from current hour (filter them).
	
	Args:
		predictions_folder: Path to predikce folder
	
	Returns:
		True if predictions exist after filtering
	"""
	from trading_logic import filter_predictions
	
	print(f"\n📊 Processing existing predictions from {predictions_folder}...")
	
	# Count before filtering
	before_count = len(list(predictions_folder.glob("*.json")))
	print(f"   Before: {before_count} predictions")
	
	# Filter
	deleted_count = filter_predictions(predictions_folder)
	
	# Count after filtering
	after_count = len(list(predictions_folder.glob("*.json")))
	print(f"   After: {after_count} predictions (deleted {deleted_count})")
	
	return after_count > 0


def is_in_restricted_trading_hours() -> bool:
	"""
	Check if current time is in the broker-derived swap rollover block window.

	Trading is paused from 30 minutes before to 30 minutes after the detected
	swap rollover time.
	"""
	now_utc = datetime.now(tz=timezone.utc)
	window = get_swap_block_window(now_utc=now_utc)
	return window.contains(now_utc)


def wait_until_trading_allowed() -> None:
	"""
	Wait until the broker-derived swap rollover block window has passed.
	"""
	while True:
		now_utc = datetime.now(tz=timezone.utc)
		window = get_swap_block_window(now_utc=now_utc)
		if not window.contains(now_utc):
			break

		sleep_seconds = (window.end_utc - now_utc).total_seconds()
		if sleep_seconds > 0:
			print("\n⏸️  TRADING PAUSE - Swap rollover block window")
			print(f"   Current UTC:      {now_utc.strftime('%H:%M:%S')}")
			print(
				"   Block window:     "
				f"{window.start_utc.strftime('%H:%M')} - {window.end_utc.strftime('%H:%M')} UTC"
			)
			print(f"   Rollover center:  {window.rollover_at_utc.strftime('%H:%M')} UTC")
			print(f"   Source:           {window.rollover_time.source}")
			print(f"   Resuming trades:  {window.end_utc.strftime('%H:%M:%S')} UTC")
			print(f"   Sleeping for:     {int(sleep_seconds)} seconds (~{int(sleep_seconds/60)} minutes)")
			print("   Reason:           Avoid swap on rollover-adjacent trading\n")
			
			# Sleep in 10-second intervals to allow graceful shutdown
			remaining = sleep_seconds
			while remaining > 0:
				time.sleep(min(10, remaining))
				remaining -= 10
			break
		else:
			time.sleep(1)


def main() -> int:
	try:
		cfg = Config.from_env()
	except Exception as exc:  # pylint: disable=broad-except
		print(f"Config error: {exc}")
		return 2

	# Event to signal Ollama service shutdown
	ollama_stop_event = threading.Event()
	ollama_thread = None

	try:
		initialize_mt5(login=cfg.mt5_login, password=cfg.mt5_password, server=cfg.mt5_server)
		print("Connected to MetaTrader 5.")
		
		print("\n" + "="*60)
		print("🤖 Obchodní Automat - Nekonečný cyklus")
		print("="*60)
		print("Monitoring → Predictions → Final Decision → Trade → Repeat")
		print("Ukončení: Ctrl+C")
		print("="*60 + "\n")
		
		# Start Ollama service in a separate thread
		def ollama_wrapper():
			"""Wrapper to run Ollama service."""
			ollama_service_loop(cfg.service_dest_folder, ollama_stop_event)
		
		ollama_thread = threading.Thread(
			target=ollama_wrapper,
			name="OllamaService",
			daemon=False
		)
		ollama_thread.start()
		print("🔮 Ollama Service thread spuštěn...\n")
		
		cycle_count = 0
		
		# Infinite trading loop
		while True:
			# Check if we're in the broker-derived swap block window.
			if is_in_restricted_trading_hours():
				wait_until_trading_allowed()
			
			cycle_count += 1
			print(f"\n{'='*60}")
			print(f"🔄 Cyklus #{cycle_count}")
			print(f"{'='*60}")
			
			# Thread-safe events for coordination
			monitor_stop_event = threading.Event()
			trading_trigger_event = threading.Event()
			
			def monitor_wrapper():
				"""Wrapper to run account monitor."""
				run_account_monitor(
					check_interval_seconds=60, 
					stop_event=monitor_stop_event,
					trading_trigger_event=trading_trigger_event
				)
			
			# Start account monitor in a background thread
			monitor_thread = threading.Thread(
				target=monitor_wrapper,
				daemon=False
			)
			monitor_thread.start()
			print("📊 Account monitor started...")
			
			# Wait for monitor to complete (will signal trading_trigger_event if margin > threshold)
			monitor_thread.join()
			
			# Check if trading trigger was set by monitor
			if trading_trigger_event.is_set():
				# Double-check we're not in the broker-derived swap block window before proceeding.
				if is_in_restricted_trading_hours():
					window = get_swap_block_window()
					print("\n🛑 TRADING BLOCKED - Swap rollover block window")
					print(
						"   Discarding prepared signals and waiting until "
						f"{window.end_utc.strftime('%H:%M:%S')} UTC..."
					)
					wait_until_trading_allowed()
				else:
					print("\n🚀 Stop condition met - proceeding with trading...")
					
					predictions_folder = None
					
					# Check if predictions from current hour already exist
					existing_predictions = find_predictions_folder_for_current_hour(cfg.service_dest_folder)
					
					if existing_predictions:
						# Use existing predictions from current hour
						print("💡 Using existing predictions from current hour")
						has_predictions = process_existing_predictions(existing_predictions)
						if has_predictions:
							predictions_folder = existing_predictions
						else:
							print("⚠️  Existing predictions were filtered out, restarting cycle...")
					else:
						# Need to download data and get new predictions
						print("📥 Downloading market data for current hour...")
						run_cycle(cfg)
						
						print("🤖 Getting predictions from Gemini AI...")
						try:
							success, pred_folder = run_trading_logic(cfg.service_dest_folder)
							predictions_folder = pred_folder
							if success:
								print("✅ Trading logic completed successfully")
							else:
								print("⚠️  Trading logic completed with warnings")
						except Exception as trading_exc:
							print(f"❌ Trading logic failed: {trading_exc}")
					
					# Make final trading decision if we have predictions
					if predictions_folder:
						print("\n🎯 Making final trading decision...")
						try:
							make_final_trading_decision(predictions_folder, cfg.service_dest_folder)
							print("\n✅ Cycle completed. Restarting monitoring...")
						except Exception as decision_exc:
							print(f"❌ Final decision failed: {decision_exc}")
					else:
						print("\n⚠️  No predictions available, restarting cycle...")
			else:
				print("\n⏸️  Stop condition not met - restarting monitoring...")
			
			# Brief pause before next cycle
			time.sleep(2)
	
	except KeyboardInterrupt:
		print("\n\n🛑 Stopping trading automat (Ctrl+C detected)...")
		print("🛑 Zastavuji Ollama Service...")
		ollama_stop_event.set()
		if ollama_thread and ollama_thread.is_alive():
			ollama_thread.join(timeout=5)
		return 0
	except Exception as exc:  # pylint: disable=broad-except
		print(f"Fatal error: {exc}")
		return 1
	finally:
		# Ensure Ollama service is stopped
		ollama_stop_event.set()
		if ollama_thread and ollama_thread.is_alive():
			print("🛑 Čekám na ukončení Ollama Service...")
			ollama_thread.join(timeout=10)
		
		shutdown_mt5()
		print("MetaTrader 5 connection closed.")


if __name__ == "__main__":
	sys.exit(main())
