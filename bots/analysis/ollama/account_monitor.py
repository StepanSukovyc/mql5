"""Account status monitor - checks balance, equity, margin every minute."""

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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

def get_account_info() -> dict:
	"""Get current account status: balance, equity, margin, available margin."""
	account = mt5.account_info()
	if account is None:
		raise RuntimeError(f"Failed to get account info: {mt5.last_error()}")
	
	return {
		"timestamp": datetime.now(tz=timezone.utc).isoformat(),
		"balance": float(account.balance),
		"equity": float(account.equity),
		"margin": float(account.margin),
		"margin_free": float(account.margin_free),
	}


def print_account_status(account_info: dict) -> None:
	"""Print account status to console (single line)."""
	timestamp = account_info["timestamp"]
	
	# Calculate free margin percentage
	free_margin_percent = (account_info['margin_free'] / account_info['balance'] * 100) if account_info['balance'] > 0 else 0
	
	# Single line output
	print(f"[{timestamp}] Balance: {account_info['balance']:.2f} | Equity: {account_info['equity']:.2f} | Margin: {account_info['margin']:.2f} | Free: {account_info['margin_free']:.2f} ({free_margin_percent:.2f}%)")


def _get_margin_threshold() -> float:
	"""
	Get the margin threshold percentage from .env (default 20%).
	Value is expected as integer (20), will be converted to decimal (0.20).
	"""
	threshold_str = os.environ.get('TRADING_MARGIN_THRESHOLD', '20')
	try:
		threshold_percent = float(threshold_str)
		return threshold_percent / 100  # Convert percentage to decimal
	except ValueError:
		return 0.20  # Default to 20%


def check_stop_condition(account_info: dict) -> bool:
	"""
	Check if monitoring should stop.
	
	Stop condition: Stop if margin_free exceeds threshold % of balance.
	Threshold is loaded from TRADING_MARGIN_THRESHOLD env variable (default 20%).
	"""
	margin_ratio = account_info['margin_free'] / account_info['balance'] if account_info['balance'] > 0 else 0
	threshold = _get_margin_threshold()
	threshold_percent = threshold * 100
	
	# Stop if available margin exceeds threshold
	if margin_ratio > threshold:
		print(f"\n⚠️  Stop condition met: Available margin exceeded {threshold_percent:.0f}% of balance!")
		return True
	
	return False


def run_account_monitor(check_interval_seconds: int = 60, max_duration_seconds: Optional[int] = None, stop_event: Optional[threading.Event] = None, trading_trigger_event: Optional[threading.Event] = None) -> None:
	"""
	Monitor account status every N seconds until stop condition is met.
	
	Args:
		check_interval_seconds: How often to check account (default 60 = 1 minute)
		max_duration_seconds: Maximum duration in seconds (None = no limit)
		stop_event: Threading event to signal shutdown (None = no external stop signal)
		trading_trigger_event: Threading event to signal when to start trading logic (None = this feature disabled)
	"""
	print(f"\n🔍 Account Monitor started. Checking every {check_interval_seconds} seconds...")
	
	start_time = time.time()
	check_count = 0
	
	try:
		while True:
			# Check if external stop signal was sent
			if stop_event and stop_event.is_set():
				print(f"✅ Monitoring stopped (shutdown signal received) after {check_count} checks.")
				break
			
			check_count += 1
			
			try:
				account_info = get_account_info()
				print_account_status(account_info)
				
				# Check if we should trigger trading logic
				if check_stop_condition(account_info):
					print(f"✅ Monitoring stopped after {check_count} checks.")
					if trading_trigger_event:
						trading_trigger_event.set()
						print(f"🚀 Trading trigger event SET")
					break
				
			except Exception as exc:
				print(f"❌ Error during account check: {exc}")
				# If MT5 is no longer connected, stop gracefully
				break
			
			# Check if max duration exceeded
			if max_duration_seconds and (time.time() - start_time) > max_duration_seconds:
				print(f"✅ Monitoring stopped: max duration ({max_duration_seconds}s) reached after {check_count} checks.")
				break
			
			# Interruptible sleep with stop event check
			for _ in range(check_interval_seconds):
				if stop_event and stop_event.is_set():
					print(f"✅ Monitoring stopped (shutdown signal received) after {check_count} checks.")
					return
				time.sleep(1)
	
	except KeyboardInterrupt:
		print(f"\n✅ Monitoring interrupted after {check_count} checks.")


if __name__ == "__main__":
	# Load env if running standalone
	base_dir = Path(__file__).resolve().parent
	_load_dotenv(base_dir / ".env")
	_load_dotenv(base_dir.parent / ".env")
	_load_dotenv(Path.cwd() / ".env")
	
	# Initialize MT5
	ok = mt5.initialize()
	if not ok:
		raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
	
	try:
		run_account_monitor(check_interval_seconds=60)
	finally:
		mt5.shutdown()
		print("MetaTrader 5 connection closed.")
