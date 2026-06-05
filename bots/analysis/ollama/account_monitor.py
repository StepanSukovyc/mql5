"""Account status monitor - checks balance, equity, margin every minute."""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from account_state import get_account_state
from loss_cleanup_strategy import run_loss_cleanup_strategy_if_due
from monthly_loss_cleanup_strategy import run_monthly_loss_cleanup_strategy_if_due
from profit_protection_strategy import run_profit_protection_strategy_if_due
from mt5_connection import initialize_mt5, shutdown_mt5
from reversal_pattern_strategy import is_reversal_strategy_enabled
from strategy_context import get_parallel_strategy_context, get_primary_strategy_context, get_reversal_strategy_context
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


def _get_trade_logs_dir() -> Optional[Path]:
	service_dest_folder = os.environ.get("SERVICE_DEST_FOLDER", "").strip()
	if not service_dest_folder:
		return None
	log_dir = Path(service_dest_folder) / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	return log_dir


def _log_position_management_event(event: str, **payload: object) -> None:
	log_dir = _get_trade_logs_dir()
	if log_dir is None:
		return
	entry = {
		"timestamp": datetime.now(tz=timezone.utc).isoformat(),
		"event": event,
		**payload,
	}
	with open(log_dir / "position_management_monitor.jsonl", "a", encoding="utf-8") as handle:
		handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str) + "\n")

def get_account_state_snapshot() -> dict:
	"""Get current account status: balance, equity, margin, available margin."""
	return get_account_state(include_timestamp=True)


def _get_trading_trigger_margin_ratio(account_info: dict) -> float:
	"""Return the ratio used to decide when trading may start again."""
	balance = float(account_info.get('balance', 0.0) or 0.0)
	if balance <= 0:
		return 0.0

	# Trading re-entry should look at actual free margin against the capped strategy balance.
	raw_margin_free = float(account_info.get('raw_margin_free', account_info.get('margin_free', 0.0)) or 0.0)
	return raw_margin_free / balance


def print_account_status(account_info: dict) -> None:
	"""Print account status to console (single line)."""
	timestamp = account_info["timestamp"]
	
	# Calculate free margin percentage
	free_margin_percent = _get_trading_trigger_margin_ratio(account_info) * 100
	balance_text = f"{account_info['balance']:.2f}"
	free_margin_text = f"{account_info['margin_free']:.2f}"
	if account_info.get('balance_reserve', 0) > 0:
		balance_text += f" (raw {account_info['raw_balance']:.2f})"
		free_margin_text += f" (raw {account_info['raw_margin_free']:.2f})"
	
	# Single line output
	print(f"[{timestamp}] Balance: {balance_text} | Equity: {account_info['equity']:.2f} | Margin: {account_info['margin']:.2f} | Free: {free_margin_text} ({free_margin_percent:.2f}%)")


def _get_margin_threshold() -> float:
	"""
	Get the account-monitor trigger threshold as a ratio.

	If TRADING_TRIGGER_MARGIN_THRESHOLD is set, it wins.
	Otherwise the monitor should wake up when at least one strategy can trade,
	so the default is the minimum activation threshold across active profiles.
	"""
	default_threshold_percent = min(
		get_primary_strategy_context().activation_margin_percent,
		get_parallel_strategy_context().activation_margin_percent,
	)
	if is_reversal_strategy_enabled():
		default_threshold_percent = min(default_threshold_percent, get_reversal_strategy_context().activation_margin_percent)
	threshold_str = os.environ.get('TRADING_TRIGGER_MARGIN_THRESHOLD', str(default_threshold_percent))
	try:
		threshold_percent = float(threshold_str)
		return threshold_percent / 100  # Convert percentage to decimal
	except ValueError:
		return default_threshold_percent / 100


def check_stop_condition(account_info: dict) -> bool:
	"""
	Check if monitoring should stop.
	
	Stop condition: Stop if actual free margin exceeds the decision trigger
	threshold % of capped strategy balance.
	"""
	margin_ratio = _get_trading_trigger_margin_ratio(account_info)
	threshold = _get_margin_threshold()
	threshold_percent = threshold * 100
	
	# Stop if available margin exceeds threshold
	if margin_ratio > threshold:
		print(f"\n⚠️  Stop condition met: Available margin exceeded {threshold_percent:.0f}% of balance!")
		return True
	
	return False


def run_position_management_monitor(check_interval_seconds: int = 60, stop_event: Optional[threading.Event] = None) -> None:
	"""Continuously run profit/cleanup management independently of entry monitoring."""
	print(f"\n🛡️  Position management monitor started. Checking every {check_interval_seconds} seconds...")
	_log_position_management_event(
		"position_management_monitor_started",
		check_interval_seconds=check_interval_seconds,
	)

	cycle_count = 0
	try:
		while True:
			if stop_event and stop_event.is_set():
				_log_position_management_event(
					"position_management_monitor_stopped",
					cycle_count=cycle_count,
				)
				print(f"✅ Position management monitor stopped after {cycle_count} checks.")
				break

			cycle_count += 1
			try:
				account_info = get_account_state_snapshot()
				_log_position_management_event(
					"position_management_monitor_tick",
					cycle_count=cycle_count,
					check_interval_seconds=check_interval_seconds,
					account_timestamp=account_info.get("timestamp"),
					balance=account_info.get("balance"),
					equity=account_info.get("equity"),
					margin_free=account_info.get("margin_free"),
					raw_margin_free=account_info.get("raw_margin_free"),
				)
				run_profit_protection_strategy_if_due()
				run_swap_rollover_cleanup_strategy_if_due(account_info)
				run_loss_cleanup_strategy_if_due(account_info)
				run_monthly_loss_cleanup_strategy_if_due(account_info)
			except Exception as exc:
				_log_position_management_event(
					"position_management_monitor_error",
					cycle_count=cycle_count,
					error=str(exc),
				)
				print(f"❌ Error during position management check: {exc}")
				break

			for _ in range(check_interval_seconds):
				if stop_event and stop_event.is_set():
					_log_position_management_event(
						"position_management_monitor_stopped",
						cycle_count=cycle_count,
					)
					print(f"✅ Position management monitor stopped after {cycle_count} checks.")
					return
				time.sleep(1)
	except KeyboardInterrupt:
		_log_position_management_event(
			"position_management_monitor_interrupted",
			cycle_count=cycle_count,
		)
		print(f"\n✅ Position management monitor interrupted after {cycle_count} checks.")


def run_account_monitor(check_interval_seconds: int = 60, max_duration_seconds: Optional[int] = None, stop_event: Optional[threading.Event] = None, trading_trigger_event: Optional[threading.Event] = None, run_management_tasks: bool = True) -> None:
	"""
	Monitor account status every N seconds until stop condition is met.
	
	Args:
		check_interval_seconds: How often to check account (default 60 = 1 minute)
		max_duration_seconds: Maximum duration in seconds (None = no limit)
		stop_event: Threading event to signal shutdown (None = no external stop signal)
		trading_trigger_event: Threading event to signal when to start trading logic (None = this feature disabled)
		run_management_tasks: Whether to run profit/cleanup management on each monitor check
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
				if run_management_tasks:
					run_profit_protection_strategy_if_due()
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
