"""Account status monitor - checks balance, equity, margin every minute."""

import os
import threading
import time
from pathlib import Path
from typing import Optional

from account_state import get_account_state
from loss_cleanup_strategy import run_loss_cleanup_strategy_if_due
from profit_cleanup_strategy import run_profit_cleanup_strategy_if_due
from mt5_connection import initialize_mt5, shutdown_mt5
from swap_rollover_cleanup_strategy import run_swap_rollover_cleanup_strategy_if_due


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

def get_account_state_snapshot() -> dict:
	"""Get current account status: balance, equity, margin, available margin."""
	return get_account_state(include_timestamp=True)


def print_account_status(account_info: dict) -> None:
	"""Print account status to console (single line)."""
	timestamp = account_info["timestamp"]
	
	# Calculate free margin percentage
	free_margin_percent = (account_info['margin_free'] / account_info['balance'] * 100) if account_info['balance'] > 0 else 0
	balance_text = f"{account_info['balance']:.2f}"
	free_margin_text = f"{account_info['margin_free']:.2f}"
	if account_info.get('balance_reserve', 0) > 0:
		balance_text += f" (raw {account_info['raw_balance']:.2f})"
		free_margin_text += f" (raw {account_info['raw_margin_free']:.2f})"
	
	# Single line output
	print(f"[{timestamp}] Balance: {balance_text} | Equity: {account_info['equity']:.2f} | Margin: {account_info['margin']:.2f} | Free: {free_margin_text} ({free_margin_percent:.2f}%)")


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
	
	Stop condition: Stop if effective margin_free exceeds threshold % of effective balance.
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
				account_info = get_account_state_snapshot()
				print_account_status(account_info)
				run_profit_cleanup_strategy_if_due(account_info)
				run_swap_rollover_cleanup_strategy_if_due(account_info)
				run_loss_cleanup_strategy_if_due(account_info)
				
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
	initialize_mt5()
	
	try:
		run_account_monitor(check_interval_seconds=60)
	finally:
		shutdown_mt5()
		print("MetaTrader 5 connection closed.")
