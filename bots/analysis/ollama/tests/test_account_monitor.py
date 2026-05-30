"""Unit tests for account monitor trading trigger logic."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from account_monitor import check_stop_condition, run_position_management_monitor


class AccountMonitorTests(unittest.TestCase):
	@patch.dict(
		os.environ,
		{
			"PRIMARY_STRATEGY_ACTIVATION_MARGIN_PERCENT": "20",
			"PARALLEL_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT": "5",
		},
		clear=False,
	)
	def test_trigger_uses_raw_free_margin_against_capped_balance(self) -> None:
		account_info = {
			"balance": 5000.0,
			"margin_free": 872.51,
			"raw_margin_free": 800.00,
		}

		self.assertTrue(check_stop_condition(account_info))

	@patch.dict(
		os.environ,
		{
			"PRIMARY_STRATEGY_ACTIVATION_MARGIN_PERCENT": "20",
			"PARALLEL_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT": "5",
		},
		clear=False,
	)
	def test_trigger_stays_blocked_below_threshold(self) -> None:
		account_info = {
			"balance": 5000.0,
			"margin_free": 872.51,
			"raw_margin_free": 700.00,
		}

		self.assertFalse(check_stop_condition(account_info))

	@patch.dict(os.environ, {"TRADING_TRIGGER_MARGIN_THRESHOLD": "20"}, clear=False)
	def test_explicit_trigger_override_is_respected(self) -> None:
		account_info = {
			"balance": 5000.0,
			"margin_free": 872.51,
			"raw_margin_free": 800.00,
		}

		self.assertFalse(check_stop_condition(account_info))

	@patch("account_monitor.run_loss_cleanup_strategy_if_due")
	@patch("account_monitor.run_swap_rollover_cleanup_strategy_if_due")
	@patch("account_monitor.run_profit_protection_strategy_if_due")
	@patch("account_monitor.get_account_state_snapshot")
	def test_position_management_monitor_writes_heartbeat_log(
		self,
		mock_get_account_state_snapshot,
		mock_profit_protection,
		mock_swap_cleanup,
		mock_loss_cleanup,
	) -> None:
		mock_get_account_state_snapshot.return_value = {
			"timestamp": "2026-05-22T13:00:00+00:00",
			"balance": 5000.0,
			"equity": 4900.0,
			"margin_free": 1200.0,
			"raw_margin_free": 1200.0,
		}

		with tempfile.TemporaryDirectory() as temp_dir:
			stop_event = threading.Event()
			with patch.dict(os.environ, {"SERVICE_DEST_FOLDER": temp_dir}, clear=False):
				with patch("account_monitor.time.sleep", side_effect=lambda _seconds: stop_event.set()):
					run_position_management_monitor(check_interval_seconds=1, stop_event=stop_event)

			log_file = Path(temp_dir) / "trade_logs" / "position_management_monitor.jsonl"
			self.assertTrue(log_file.exists())
			entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]

		self.assertTrue(any(entry["event"] == "position_management_monitor_started" for entry in entries))
		self.assertTrue(any(entry["event"] == "position_management_monitor_tick" for entry in entries))
		self.assertTrue(any(entry["event"] == "position_management_monitor_stopped" for entry in entries))
		mock_profit_protection.assert_called()
		mock_swap_cleanup.assert_called()
		mock_loss_cleanup.assert_called()


if __name__ == "__main__":
	unittest.main()