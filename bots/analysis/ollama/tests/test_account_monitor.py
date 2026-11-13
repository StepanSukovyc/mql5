"""Unit tests for account monitor trading trigger logic."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import MetaTrader5 as mt5

import account_monitor
from account_monitor import _remove_negative_swap_take_profits, _run_management_tasks, check_stop_condition, run_old_loss_cleanup_if_due, run_position_management_monitor


class AccountMonitorTests(unittest.TestCase):
	def setUp(self) -> None:
		self.daily_profit_exit = self.enterContext(patch("account_monitor.run_daily_profit_exit_strategy_if_due"))
		self.old_loss_cleanup = self.enterContext(patch("account_monitor.run_old_loss_cleanup_if_due"))

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
				with patch("account_monitor.process_closed_trade_outcomes") as mock_outcomes, patch(
					"account_monitor._remove_negative_swap_take_profits"
				) as mock_remove_tp, patch(
					"account_monitor.run_profit_cleanup_strategy_if_due"
				) as mock_profit_cleanup, patch(
					"account_monitor.time.sleep", side_effect=lambda _seconds: stop_event.set()
				):
					run_position_management_monitor(check_interval_seconds=1, stop_event=stop_event)
					mock_remove_tp.assert_called_once()
					mock_outcomes.assert_called_once_with(Path(temp_dir))
					mock_profit_cleanup.assert_called_once_with(mock_get_account_state_snapshot.return_value)
					self.daily_profit_exit.assert_called_once_with(stop_event)
					self.old_loss_cleanup.assert_called_once_with()

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
		with tempfile.TemporaryDirectory() as temp_dir:
			with patch.dict(os.environ, {"SERVICE_DEST_FOLDER": temp_dir}, clear=False), patch(
				"account_monitor.process_closed_trade_outcomes"
			) as mock_outcomes, patch("account_monitor._remove_negative_swap_take_profits"):
				_run_management_tasks({"balance": 5000.0})
				mock_outcomes.assert_called_once_with(Path(temp_dir))

		mock_swap_cleanup.assert_called_once()
		mock_profit_protection.assert_called_once()
		self.daily_profit_exit.assert_called_once_with(None)


class NegativeSwapTakeProfitTests(unittest.TestCase):
	def setUp(self) -> None:
		self.positions_get = self.enterContext(patch("account_monitor.mt5.positions_get"))
		self.order_send = self.enterContext(patch("account_monitor.mt5.order_send"))
		self.log_event = self.enterContext(patch("account_monitor._log_position_management_event"))
		self.enterContext(patch("account_monitor.mt5.last_error", return_value=(1, "terminal unavailable")))
		self.enterContext(patch("account_monitor.run_old_loss_cleanup_if_due"))

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
			"account_monitor.run_profit_cleanup_strategy_if_due",
			side_effect=lambda _account: calls.append("profit_cleanup"),
		), patch(
			"account_monitor.run_profit_protection_strategy_if_due", side_effect=lambda: calls.append("profit")
		), patch(
			"account_monitor.run_swap_rollover_cleanup_strategy_if_due", side_effect=lambda _account: calls.append("cleanup")
		):
			_run_management_tasks({"balance": 5000.0})
		self.assertEqual(calls, ["tp", "profit_cleanup", "profit", "cleanup"])

	def test_removal_failure_does_not_disable_other_management_tasks(self) -> None:
		with patch("account_monitor._remove_negative_swap_take_profits", side_effect=RuntimeError("read failure")), patch(
			"account_monitor.run_profit_cleanup_strategy_if_due"
		) as profit_cleanup, patch(
			"account_monitor.run_profit_protection_strategy_if_due"
		) as profit, patch("account_monitor.run_swap_rollover_cleanup_strategy_if_due") as cleanup:
			_run_management_tasks({"balance": 5000.0})
		profit_cleanup.assert_called_once_with({"balance": 5000.0})
		profit.assert_called_once_with()
		cleanup.assert_called_once_with({"balance": 5000.0})


class OldLossCleanupTests(unittest.TestCase):
	def setUp(self) -> None:
		account_monitor._LAST_OLD_LOSS_CLEANUP_BUCKET = None
		self.now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
		self.positions_get = self.enterContext(patch("account_monitor.mt5.positions_get"))
		self.costs = self.enterContext(patch("account_monitor.get_position_charged_costs", return_value=-0.5))
		self.close = self.enterContext(patch("account_monitor.close_position_by_ticket", return_value=True))
		self.log = self.enterContext(patch("account_monitor._log_position_management_event"))
		self.enterContext(patch.dict(os.environ, {}, clear=True))

	def position(self, ticket: int, age: timedelta, profit: float, swap: float = 0) -> SimpleNamespace:
		return SimpleNamespace(
			ticket=ticket, symbol="EURUSD", type=0, volume=0.1,
			time=(self.now - age).timestamp(), profit=profit, swap=swap,
		)

	def test_only_positions_over_default_age_and_in_net_loss_are_closed(self) -> None:
		positions = (
			self.position(1, timedelta(days=77, seconds=1), 0.6, swap=-0.2),
			self.position(2, timedelta(days=77), -10),
			self.position(3, timedelta(days=78), 1),
			self.position(4, timedelta(days=78), 0.5),
		)
		self.positions_get.side_effect = lambda **kwargs: tuple(
			p for p in positions if not kwargs or p.ticket == kwargs["ticket"]
		)
		run_old_loss_cleanup_if_due(now_utc=self.now)

		self.close.assert_called_once_with(
			position_ticket=1, symbol="EURUSD", position_type=0,
			volume=0.1, comment="Old loss cleanup",
		)
		self.assertEqual(self.log.call_args.args[0], "old_loss_cleanup_closed")
		self.assertEqual(self.log.call_args.kwargs["close_reason"], "age_limit")

	def test_loss_over_333_closes_young_position_regardless_of_age_setting(self) -> None:
		position = self.position(1, timedelta(days=1), -333.01)
		self.positions_get.return_value = (position,)
		with patch.dict(os.environ, {"OLD_LOSS_MAX_AGE_DAYS": "invalid"}):
			run_old_loss_cleanup_if_due(now_utc=self.now)

		self.close.assert_called_once_with(
			position_ticket=1, symbol="EURUSD", position_type=0,
			volume=0.1, comment="Position loss limit cleanup",
		)
		self.assertEqual(self.log.call_args.kwargs["close_reason"], "loss_limit")

	def test_configured_loss_limit_overrides_default(self) -> None:
		position = self.position(1, timedelta(days=1), -250.01)
		self.positions_get.return_value = (position,)
		with patch.dict(os.environ, {"OLD_LOSS_MAX_AMOUNT": "250"}):
			run_old_loss_cleanup_if_due(now_utc=self.now)

		self.close.assert_called_once()
		self.assertEqual(self.log.call_args.kwargs["max_position_loss"], 250.0)

	def test_loss_exactly_333_does_not_trigger_absolute_loss_limit(self) -> None:
		position = self.position(1, timedelta(days=1), -332.5)
		self.positions_get.return_value = (position,)
		self.costs.return_value = -0.5
		run_old_loss_cleanup_if_due(now_utc=self.now)
		self.close.assert_not_called()

	def test_absolute_loss_uses_net_result_including_swap_and_costs(self) -> None:
		position = self.position(1, timedelta(days=1), -330, swap=-2)
		self.positions_get.return_value = (position,)
		self.costs.return_value = -1.01
		run_old_loss_cleanup_if_due(now_utc=self.now)
		self.close.assert_called_once()
		self.assertEqual(self.log.call_args.kwargs["close_reason"], "loss_limit")

	def test_refresh_rechecks_current_net_result(self) -> None:
		old = self.position(1, timedelta(days=80), -2)
		refreshed = self.position(1, timedelta(days=80), 1)
		self.positions_get.side_effect = [(old,), (refreshed,)]
		run_old_loss_cleanup_if_due(now_utc=self.now)
		self.close.assert_not_called()

	def test_break_even_is_not_closed_due_to_floating_point_rounding(self) -> None:
		position = self.position(1, timedelta(days=80), 0.3, swap=-0.2)
		self.positions_get.return_value = (position,)
		self.costs.return_value = -0.1
		run_old_loss_cleanup_if_due(now_utc=self.now)
		self.close.assert_not_called()

	def test_five_minute_buckets_and_custom_age_retry_failed_closes(self) -> None:
		position = self.position(1, timedelta(days=31), -1)
		self.positions_get.return_value = (position,)
		self.close.return_value = False
		with patch.dict(os.environ, {"OLD_LOSS_MAX_AGE_DAYS": "30"}):
			run_old_loss_cleanup_if_due(now_utc=self.now)
			run_old_loss_cleanup_if_due(now_utc=self.now + timedelta(minutes=4))
			run_old_loss_cleanup_if_due(now_utc=self.now + timedelta(minutes=5))
		self.assertEqual(self.close.call_count, 2)
		self.assertEqual(self.log.call_args.args[0], "old_loss_cleanup_close_failed")

	def test_missing_cost_history_skips_one_position_but_not_others(self) -> None:
		first = self.position(1, timedelta(days=80), -1)
		second = self.position(2, timedelta(days=80), -1)
		self.positions_get.side_effect = [(first, second), (first,), (second,)]
		self.costs.side_effect = [RuntimeError("history unavailable"), -0.5]
		run_old_loss_cleanup_if_due(now_utc=self.now)
		self.close.assert_called_once()
		self.assertEqual(self.close.call_args.kwargs["position_ticket"], 2)
		self.assertTrue(any(call.args[0] == "old_loss_cleanup_position_error" for call in self.log.call_args_list))

	def test_invalid_age_and_unavailable_positions_are_explicit_errors(self) -> None:
		with patch.dict(os.environ, {"OLD_LOSS_MAX_AGE_DAYS": "0"}):
			run_old_loss_cleanup_if_due(now_utc=self.now)
		self.assertTrue(any(
			call.args[0] == "old_loss_cleanup_configuration_error"
			for call in self.log.call_args_list
		))
		account_monitor._LAST_OLD_LOSS_CLEANUP_BUCKET = None
		self.positions_get.return_value = None
		with patch("account_monitor.mt5.last_error", return_value=(-1, "unavailable")):
			with self.assertRaisesRegex(RuntimeError, "unavailable"):
				run_old_loss_cleanup_if_due(now_utc=self.now)


if __name__ == "__main__":
	unittest.main()