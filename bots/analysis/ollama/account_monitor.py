"""Account status monitor - checks balance, equity, margin every minute."""

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import MetaTrader5 as mt5

from account_state import get_account_state
from profit_protection_strategy import run_profit_protection_strategy_if_due
from mt5_connection import initialize_mt5, shutdown_mt5
from reversal_pattern_strategy import is_reversal_strategy_enabled
from strategy_context import get_parallel_strategy_context, get_primary_strategy_context, get_reversal_strategy_context, get_scalping_strategy_context
from liquidity_sweep_scalping_strategy import is_scalping_strategy_enabled
from profit_cleanup_strategy import run_profit_cleanup_strategy_if_due
from swap_rollover_cleanup_strategy import run_swap_rollover_cleanup_strategy_if_due
from trade_outcomes import process_closed_trade_outcomes
from daily_profit_exit_strategy import run_daily_profit_exit_strategy_if_due
from virtual_stop_loss_strategy import run_virtual_stop_loss_strategy_if_due
from position_costs import get_position_charged_costs
from trade_execution import close_position_by_ticket


_OLD_LOSS_CLEANUP_LOCK = threading.Lock()
_LAST_OLD_LOSS_CLEANUP_BUCKET: Optional[int] = None
DEFAULT_MAX_POSITION_LOSS = 333.0


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


def _remove_negative_swap_take_profits() -> None:
	"""Remove broker-side TP from any open position with accrued negative swap."""
	positions = mt5.positions_get()
	if positions is None:
		raise RuntimeError(f"Failed to get positions for negative-swap TP removal: {mt5.last_error()}")

	for position in positions:
		if not (position.swap < 0 and position.tp > 0):
			continue

		# Refresh the position so we preserve its latest SL and recheck eligibility.
		current_positions = mt5.positions_get(ticket=position.ticket)
		if current_positions is None:
			message = f"Failed to refresh position: {mt5.last_error()}"
			_log_position_management_event(
				"negative_swap_tp_removal_error", ticket=position.ticket,
				symbol=position.symbol, error=message,
			)
			print(f"Negative-swap TP removal failed for ticket {position.ticket}: {message}")
			continue
		if not current_positions:
			continue
		current = current_positions[0]
		if not (current.swap < 0 and current.tp > 0):
			continue

		request = {
			"action": mt5.TRADE_ACTION_SLTP,
			"position": current.ticket,
			"symbol": current.symbol,
			"sl": current.sl,
			"tp": 0.0,
		}
		result = mt5.order_send(request)
		success = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
		error = ""
		if result is None:
			error = f"MT5 order_send failed: {mt5.last_error()}"
		elif not success:
			error = f"{result.retcode} - {result.comment}"
		_log_position_management_event(
			"negative_swap_tp_removed" if success else "negative_swap_tp_removal_error",
			ticket=current.ticket, symbol=current.symbol, swap=current.swap,
			previous_tp=current.tp, sl=current.sl, error=error,
		)
		if success:
			print(f"Take profit removed: ticket={current.ticket}, symbol={current.symbol}, swap={current.swap}")
		else:
			print(f"Negative-swap TP removal failed for ticket {current.ticket}: {error}")


def run_old_loss_cleanup_if_due(*, now_utc: Optional[datetime] = None) -> None:
	"""Close positions past the age limit or beyond the absolute net-loss limit."""
	global _LAST_OLD_LOSS_CLEANUP_BUCKET

	now = now_utc or datetime.now(tz=timezone.utc)
	if now.tzinfo is None:
		raise ValueError("Old loss cleanup requires a timezone-aware timestamp")
	bucket = int(now.timestamp()) // 300
	with _OLD_LOSS_CLEANUP_LOCK:
		if _LAST_OLD_LOSS_CLEANUP_BUCKET == bucket:
			return
		_LAST_OLD_LOSS_CLEANUP_BUCKET = bucket

		raw_days = os.getenv("OLD_LOSS_MAX_AGE_DAYS", "77")
		max_age_days: Optional[int]
		try:
			max_age_days = int(raw_days)
			if max_age_days <= 0:
				raise ValueError
		except ValueError:
			max_age_days = None
			message = f"Invalid OLD_LOSS_MAX_AGE_DAYS: {raw_days!r}; age-based closing is disabled"
			_log_position_management_event("old_loss_cleanup_configuration_error", error=message)
			print(message)

		raw_max_loss = os.getenv("OLD_LOSS_MAX_AMOUNT", str(DEFAULT_MAX_POSITION_LOSS))
		max_position_loss: Optional[float]
		try:
			max_position_loss = float(raw_max_loss)
			if not math.isfinite(max_position_loss) or max_position_loss <= 0:
				raise ValueError
		except ValueError:
			max_position_loss = None
			message = f"Invalid OLD_LOSS_MAX_AMOUNT: {raw_max_loss!r}; loss-limit closing is disabled"
			_log_position_management_event("old_loss_cleanup_configuration_error", error=message)
			print(message)

		positions = mt5.positions_get()
		if positions is None:
			raise RuntimeError(f"Failed to get positions for old loss cleanup: {mt5.last_error()}")

		for position in positions:
			try:
				current_positions = mt5.positions_get(ticket=position.ticket)
				if current_positions is None:
					raise RuntimeError(f"Failed to refresh position: {mt5.last_error()}")
				if not current_positions:
					continue
				current = current_positions[0]
				opened_at = float(current.time)
				if not math.isfinite(opened_at) or opened_at <= 0:
					raise ValueError("Invalid refreshed position opening time")

				net_profit = round(float(current.profit) + float(current.swap) + get_position_charged_costs(current), 2)
				if not math.isfinite(net_profit):
					raise ValueError("Invalid position net profit")
				age_days = (now.timestamp() - opened_at) / 86400
				loss_limit_triggered = (
					max_position_loss is not None
					and net_profit < -max_position_loss
				)
				age_limit_triggered = (
					max_age_days is not None
					and age_days > max_age_days
					and net_profit < 0
				)
				if not loss_limit_triggered and not age_limit_triggered:
					continue

				close_reason = "loss_limit" if loss_limit_triggered else "age_limit"
				closed = close_position_by_ticket(
					position_ticket=int(current.ticket),
					symbol=str(current.symbol),
					position_type=int(current.type),
					volume=float(current.volume),
					comment="Position loss limit cleanup" if loss_limit_triggered else "Old loss cleanup",
				)
				_log_position_management_event(
					"old_loss_cleanup_closed" if closed else "old_loss_cleanup_close_failed",
					ticket=current.ticket, symbol=current.symbol, net_profit=net_profit,
					age_days=age_days, max_age_days=max_age_days,
					close_reason=close_reason, max_position_loss=max_position_loss,
				)
				if not closed:
					print(f"Old loss cleanup failed to close ticket {current.ticket}")
			except (AttributeError, TypeError, ValueError, RuntimeError, OverflowError) as exc:
				_log_position_management_event(
					"old_loss_cleanup_position_error",
					ticket=getattr(position, "ticket", None), error=str(exc),
				)
				print(f"Old loss cleanup failed for ticket {getattr(position, 'ticket', None)}: {exc}")


def _run_management_tasks(account_info: dict, stop_event: Optional[threading.Event] = None) -> None:
	"""Run independent position-management tasks without letting one disable the others."""
	def _track_closed_trade_outcomes() -> None:
		service_dest_folder = os.environ.get("SERVICE_DEST_FOLDER", "").strip()
		if not service_dest_folder:
			raise RuntimeError("SERVICE_DEST_FOLDER is required for closed trade outcome tracking")
		process_closed_trade_outcomes(Path(service_dest_folder))

	tasks = (
		("negative_swap_tp_removal", _remove_negative_swap_take_profits),
		("closed_trade_outcomes", _track_closed_trade_outcomes),
		("profit_cleanup", lambda: run_profit_cleanup_strategy_if_due(account_info)),
		("profit_protection", run_profit_protection_strategy_if_due),
		("virtual_stop_loss", run_virtual_stop_loss_strategy_if_due),
		("old_loss_cleanup", run_old_loss_cleanup_if_due),
		("swap_rollover_cleanup", lambda: run_swap_rollover_cleanup_strategy_if_due(account_info)),
		("daily_profit_exit", lambda: run_daily_profit_exit_strategy_if_due(stop_event)),
	)
	for task_name, task in tasks:
		try:
			task()
		except Exception as exc:
			_log_position_management_event("position_management_task_error", task=task_name, error=str(exc))
			print(f"❌ Position management task {task_name} failed: {exc}")

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

	The monitor wakes up when at least one enabled entry strategy can trade.
	"""
	default_threshold_percent = min(
		get_primary_strategy_context().activation_margin_percent,
		get_parallel_strategy_context().activation_margin_percent,
	)
	if is_reversal_strategy_enabled():
		default_threshold_percent = min(default_threshold_percent, get_reversal_strategy_context().activation_margin_percent)
	if is_scalping_strategy_enabled():
		default_threshold_percent = min(default_threshold_percent, get_scalping_strategy_context().activation_margin_percent)
	threshold_str = os.environ.get('TRADING_TRIGGER_MARGIN_THRESHOLD', str(default_threshold_percent))
	try:
		threshold_percent = float(threshold_str)
	except ValueError:
		threshold_percent = default_threshold_percent

	return threshold_percent / 100  # Convert percentage to decimal


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
				_run_management_tasks(account_info, stop_event)
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
					_run_management_tasks(account_info, stop_event)
				
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
