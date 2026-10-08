"""Unit tests for account monitor trading trigger logic."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import MetaTrader5 as mt5

from account_monitor import _remove_negative_swap_take_profits, _run_management_tasks, check_stop_condition, run_position_management_monitor


class AccountMonitorTests(unittest.TestCase):
	@patch.dict(
		os.environ,
		{
			"PRIMARY_STRATEGY_ACTIVATION_MARGIN_PERCENT": "20",
			"PARALLEL_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT": "5",
			"TRADING_TRIGGER_MARGIN_THRESHOLD": "15",
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
			"TRADING_TRIGGER_MARGIN_THRESHOLD": "15",
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

	@patch("account_monitor.run_swap_rollover_cleanup_strategy_if_due")
	@patch("account_monitor.run_profit_protection_strategy_if_due")
	@patch("account_monitor.get_account_state_snapshot")
	def test_position_management_monitor_writes_heartbeat_log(
		self,
		mock_get_account_state_snapshot,
		mock_profit_protection,
		mock_swap_cleanup,
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
				with patch("account_monitor._remove_negative_swap_take_profits") as mock_remove_tp, patch(
					"account_monitor.time.sleep", side_effect=lambda _seconds: stop_event.set()
				):
					run_position_management_monitor(check_interval_seconds=1, stop_event=stop_event)
					mock_remove_tp.assert_called_once()

			log_file = Path(temp_dir) / "trade_logs" / "position_management_monitor.jsonl"
			self.assertTrue(log_file.exists())
			entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]

		self.assertTrue(any(entry["event"] == "position_management_monitor_started" for entry in entries))
		self.assertTrue(any(entry["event"] == "position_management_monitor_tick" for entry in entries))
		self.assertTrue(any(entry["event"] == "position_management_monitor_stopped" for entry in entries))
		mock_profit_protection.assert_called()
		mock_swap_cleanup.assert_called()

	@patch("account_monitor.run_profit_protection_strategy_if_due", side_effect=RuntimeError("management failure"))
	@patch("account_monitor.run_swap_rollover_cleanup_strategy_if_due")
	def test_failed_management_task_does_not_prevent_remaining_tasks(
		self,
		mock_swap_cleanup,
		mock_profit_protection,
	) -> None:
		with patch("account_monitor._remove_negative_swap_take_profits"):
			_run_management_tasks({"balance": 5000.0})

		mock_swap_cleanup.assert_called_once()
		mock_profit_protection.assert_called_once()


class NegativeSwapTakeProfitTests(unittest.TestCase):
	def setUp(self) -> None:
		self.positions_get = self.enterContext(patch("account_monitor.mt5.positions_get"))
		self.order_send = self.enterContext(patch("account_monitor.mt5.order_send"))
		self.log_event = self.enterContext(patch("account_monitor._log_position_management_event"))
		self.enterContext(patch("account_monitor.mt5.last_error", return_value=(1, "terminal unavailable")))

	def position(self, **values: object) -> SimpleNamespace:
		return SimpleNamespace(**{
			"ticket": 123, "symbol": "EURUSD", "swap": -0.01,
			"tp": 1.2, "sl": 1.05, "magic": 0, **values,
		})

	def test_negative_swap_removes_tp_for_all_ownerships_and_preserves_latest_sl(self) -> None:
		for magic in (0, 234000, 234600, 999):
			with self.subTest(magic=magic):
				self.order_send.reset_mock()
				original = self.position(magic=magic)
				current = self.position(magic=magic, sl=1.08, swap=-0.000001)
				self.positions_get.side_effect = [(original,), (current,)]
				self.order_send.return_value = SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE)
				_remove_negative_swap_take_profits()
				self.order_send.assert_called_once_with({
					"action": mt5.TRADE_ACTION_SLTP, "position": 123,
					"symbol": "EURUSD", "sl": 1.08, "tp": 0.0,
				})
				self.log_event.assert_called_with(
					"negative_swap_tp_removed", ticket=123, symbol="EURUSD",
					swap=-0.000001, previous_tp=1.2, sl=1.08, error="",
				)

	def test_nonnegative_swap_or_missing_tp_leaves_position_unchanged(self) -> None:
		self.positions_get.return_value = (
			self.position(swap=0), self.position(swap=0.01),
			self.position(tp=0), self.position(swap=0.01, tp=0),
		)
		_remove_negative_swap_take_profits()
		self.positions_get.assert_called_once_with()
		self.order_send.assert_not_called()
		self.log_event.assert_not_called()

	def test_refresh_rechecks_swap_tp_and_position_existence(self) -> None:
		for refreshed in ((), (self.position(swap=0),), (self.position(tp=0),)):
			with self.subTest(refreshed=refreshed):
				self.positions_get.side_effect = [(self.position(),), refreshed]
				_remove_negative_swap_take_profits()
				self.order_send.assert_not_called()

	def test_failed_position_read_is_explicit(self) -> None:
		self.positions_get.return_value = None
		with self.assertRaisesRegex(RuntimeError, "terminal unavailable"):
			_remove_negative_swap_take_profits()
		self.order_send.assert_not_called()

	def test_refresh_failure_is_logged_without_sending_order(self) -> None:
		self.positions_get.side_effect = [(self.position(),), None]
		_remove_negative_swap_take_profits()
		self.order_send.assert_not_called()
		self.assertEqual(self.log_event.call_args.args[0], "negative_swap_tp_removal_error")

	def test_broker_failure_continues_and_is_retried_next_check(self) -> None:
		for failure in (None, SimpleNamespace(retcode=10016, comment="Invalid stops")):
			with self.subTest(failure=failure):
				self.order_send.reset_mock()
				self.log_event.reset_mock()
				first, second = self.position(), self.position(ticket=456, sl=0)
				self.positions_get.side_effect = [(first, second), (first,), (second,)]
				self.order_send.side_effect = [failure, SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE)]
				_remove_negative_swap_take_profits()
				self.assertEqual(self.order_send.call_count, 2)
				self.assertEqual(self.order_send.call_args.args[0]["sl"], 0)
				self.assertTrue(any(
					call.args[0] == "negative_swap_tp_removal_error"
					for call in self.log_event.call_args_list
				))
				self.positions_get.side_effect = [(first,), (first,)]
				self.order_send.side_effect = [SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE)]
				_remove_negative_swap_take_profits()
				self.assertEqual(self.order_send.call_count, 3)

	def test_removal_runs_before_profit_protection_and_cleanup(self) -> None:
		calls = []
		with patch("account_monitor._remove_negative_swap_take_profits", side_effect=lambda: calls.append("tp")), patch(
			"account_monitor.run_profit_protection_strategy_if_due", side_effect=lambda: calls.append("profit")
		), patch(
			"account_monitor.run_swap_rollover_cleanup_strategy_if_due", side_effect=lambda _account: calls.append("cleanup")
		):
			_run_management_tasks({"balance": 5000.0})
		self.assertEqual(calls, ["tp", "profit", "cleanup"])

	def test_removal_failure_does_not_disable_other_management_tasks(self) -> None:
		with patch("account_monitor._remove_negative_swap_take_profits", side_effect=RuntimeError("read failure")), patch(
			"account_monitor.run_profit_protection_strategy_if_due"
		) as profit, patch("account_monitor.run_swap_rollover_cleanup_strategy_if_due") as cleanup:
			_run_management_tasks({"balance": 5000.0})
		profit.assert_called_once_with()
		cleanup.assert_called_once_with({"balance": 5000.0})


if __name__ == "__main__":
	unittest.main()