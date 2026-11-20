"""Tests for hourly account-wide position rotation."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest.mock import patch

import position_rotation_strategy as rotation
from position_rotation_strategy import run_position_rotation_strategy_if_due
from swap_rollover import PRAGUE_TZ


class PositionRotationStrategyTests(unittest.TestCase):
	def setUp(self) -> None:
		self.enterContext(patch.dict(
			os.environ,
			{
				"POSITION_ROTATION_STRATEGY_ENABLED": "true",
				"POSITION_ROTATION_STRATEGY_DRY_RUN": "false",
				"POSITION_ROTATION_STRATEGY_START_HOUR": "7",
				"POSITION_ROTATION_STRATEGY_START_MINUTE": "25",
				"POSITION_ROTATION_STRATEGY_END_HOUR": "20",
				"POSITION_ROTATION_STRATEGY_END_MINUTE": "34",
				"POSITION_ROTATION_SERVER_OFFSET_FROM_PRAGUE_HOURS": "1",
			},
			clear=False,
		))

	def _prague(self, hour: int, minute: int) -> datetime:
		return PRAGUE_TZ.localize(datetime(2026, 11, 19, hour, minute)).astimezone(timezone.utc)

	def _position(
		self,
		ticket: int,
		*,
		age_hours: float = 2,
		profit: float = -1,
		swap: float = 0,
	) -> SimpleNamespace:
		opened_at = datetime(2026, 11, 19, 6, 25, tzinfo=timezone.utc) - timedelta(hours=age_hours)
		server_wall_time = (
			opened_at.astimezone(PRAGUE_TZ).replace(tzinfo=None) + timedelta(hours=1)
		)
		return SimpleNamespace(
			ticket=ticket,
			symbol=f"SYM{ticket}",
			type=0,
			volume=0.01,
			time=server_wall_time.replace(tzinfo=timezone.utc).timestamp(),
			profit=profit,
			swap=swap,
		)

	def test_position_server_time_is_converted_before_calculating_age(self) -> None:
		now = self._prague(7, 25)
		position = self._position(1, age_hours=2, profit=1)
		with patch(
			"position_rotation_strategy.mt5.positions_get", return_value=(position,)
		), patch("position_rotation_strategy.get_position_charged_costs", return_value=0):
			snapshots = rotation._snapshot_positions(now)

		self.assertAlmostEqual(snapshots[0].age_seconds, 2 * 60 * 60)

	def _run_at(self, folder: str, hour: int, minute: int) -> None:
		with patch("position_rotation_strategy._get_service_folder", return_value=Path(folder)):
			run_position_rotation_strategy_if_due(now_utc=self._prague(hour, minute))

	def _snapshot_side_effect(
		self, positions: list[SimpleNamespace]
	) -> Callable[[datetime], list[rotation.PositionSnapshot]]:
		def snapshots(now: datetime) -> list[rotation.PositionSnapshot]:
			return self._snapshots(positions, now)
		return snapshots

	def test_prague_schedule_includes_each_hourly_window_and_stops_at_2035(self) -> None:
		self.assertTrue(rotation.is_position_rotation_active(self._prague(7, 25)))
		self.assertTrue(rotation.is_position_rotation_active(self._prague(20, 34)))
		self.assertFalse(rotation.is_position_rotation_active(self._prague(7, 24)))
		self.assertFalse(rotation.is_position_rotation_active(self._prague(20, 35)))

	def test_profitable_position_must_be_strictly_older_than_one_hour(self) -> None:
		exactly_one_hour = rotation.PositionSnapshot(
			ticket=1, symbol="A", position_type=0, volume=0.01,
			opened_at=0, age_seconds=3600, profit=1, swap=0,
			charged_costs=0, net_profit=1,
		)
		older = rotation.PositionSnapshot(
			ticket=2, symbol="B", position_type=0, volume=0.01,
			opened_at=0, age_seconds=3601, profit=1, swap=0,
			charged_costs=0, net_profit=1,
		)

		self.assertEqual(rotation._eligible_old_profits([exactly_one_hour, older]), [older])

	def test_closes_new_profitable_candidate_later_in_same_window(self) -> None:
		first = self._position(1, profit=5)
		second = self._position(2, profit=-1)
		positions = [first, second] + [self._position(ticket) for ticket in range(3, 10)]

		def close_position(*, position_ticket: int, **_kwargs: object) -> bool:
			positions[:] = [position for position in positions if position.ticket != position_ticket]
			return True

		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch(
			"position_rotation_strategy.close_position_by_ticket", side_effect=close_position
		) as close, patch(
			"position_rotation_strategy.get_position_charged_costs", return_value=-0.1
		):
			self._run_at(folder, 7, 25)
			second.profit = 2
			self._run_at(folder, 7, 29)

		self.assertEqual(
			[call.kwargs["position_ticket"] for call in close.call_args_list],
			[1, 2],
		)
		self.assertEqual(len(positions), 7)

	def test_old_profitable_position_closes_even_if_a_young_position_is_profitable(self) -> None:
		old_winner = self._position(1, age_hours=2, profit=5)
		young_winner = self._position(2, age_hours=0.5, profit=2)
		positions = [old_winner, young_winner] + [
			self._position(ticket, age_hours=2, profit=-1) for ticket in range(3, 9)
		]

		def close_position(*, position_ticket: int, **_kwargs: object) -> bool:
			positions[:] = [position for position in positions if position.ticket != position_ticket]
			return True

		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch(
			"position_rotation_strategy.close_position_by_ticket", side_effect=close_position
		) as close:
			self._run_at(folder, 7, 27)

		close.assert_called_once()
		self.assertEqual(close.call_args.kwargs["position_ticket"], 1)
		self.assertEqual(len(positions), 7)

	def test_stale_mt5_snapshot_cannot_drop_account_below_seven_positions(self) -> None:
		positions = [self._position(ticket, profit=1) for ticket in range(1, 11)]
		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch(
			"position_rotation_strategy.close_position_by_ticket", return_value=True
		) as close:
			self._run_at(folder, 7, 25)

		self.assertEqual(close.call_count, 3)

	def test_fallback_closes_two_least_losing_positions_but_keeps_seven(self) -> None:
		positions = [
			self._position(1, profit=-1),
			self._position(2, profit=-2),
			self._position(3, profit=-10),
		] + [self._position(ticket, profit=-20 - ticket) for ticket in range(4, 10)]

		def close_position(*, position_ticket: int, **_kwargs: object) -> bool:
			positions[:] = [position for position in positions if position.ticket != position_ticket]
			return True

		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch(
			"position_rotation_strategy.close_position_by_ticket", side_effect=close_position
		) as close, patch(
			"position_rotation_strategy.get_position_charged_costs", return_value=0
		):
			for minute in range(25, 30):
				self._run_at(folder, 7, minute)
			self._run_at(folder, 7, 30)

		self.assertEqual([call.kwargs["position_ticket"] for call in close.call_args_list], [1, 2])
		for call in close.call_args_list:
			# The deployed MT5 client rejected the previous 31-byte close comment.
			self.assertLess(len(call.kwargs["comment"].encode("ascii")), 31)
		self.assertEqual(len(positions), 7)

	def test_eight_positions_allow_only_one_fallback_close(self) -> None:
		positions = [self._position(ticket, profit=-ticket) for ticket in range(1, 9)]

		def close_position(*, position_ticket: int, **_kwargs: object) -> bool:
			positions[:] = [position for position in positions if position.ticket != position_ticket]
			return True

		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch(
			"position_rotation_strategy.close_position_by_ticket", side_effect=close_position
		) as close, patch(
			"position_rotation_strategy.get_position_charged_costs", return_value=0
		):
			for minute in range(25, 30):
				self._run_at(folder, 7, minute)
			self._run_at(folder, 7, 30)

		close.assert_called_once()

	def test_failed_profitable_close_does_not_trigger_loss_fallback(self) -> None:
		positions = [self._position(1, profit=1)] + [
			self._position(ticket, profit=-ticket) for ticket in range(2, 9)
		]
		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch(
			"position_rotation_strategy.close_position_by_ticket", return_value=False
		) as close, patch(
			"position_rotation_strategy.get_position_charged_costs", return_value=0
		):
			for minute in range(25, 30):
				self._run_at(folder, 7, minute)
			self._run_at(folder, 7, 30)

		self.assertTrue(close.call_count > 0)
		self.assertTrue(all(call.kwargs["position_ticket"] == 1 for call in close.call_args_list))

	def test_failed_profit_close_logs_broker_retcode_and_comment(self) -> None:
		positions = [self._position(1, profit=1)] + [
			self._position(ticket, profit=-ticket) for ticket in range(2, 9)
		]
		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch(
			"trade_execution.get_symbol_tick", return_value=SimpleNamespace(bid=1.1, ask=1.2)
		), patch(
			"trade_execution.mt5.order_send",
			return_value=SimpleNamespace(retcode=10030, comment="Invalid fill, broker rejected"),
		):
			self._run_at(folder, 7, 25)
			with (Path(folder) / "trade_logs" / rotation.LOG_FILE_NAME).open(
				"r", newline="", encoding="utf-8"
			) as handle:
				logs = list(csv.DictReader(handle))

		failure = next(row for row in logs if row["phase"] == "profit")
		self.assertEqual(failure["result"], "close_failed")
		self.assertIn("10030 - Invalid fill, broker rejected", failure["message"])

	def test_failed_fallback_close_logs_missing_tick_error(self) -> None:
		positions = [self._position(ticket, profit=-ticket) for ticket in range(1, 9)]
		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch(
			"trade_execution.get_symbol_tick", return_value=None
		), patch(
			"trade_execution.mt5.last_error", return_value=(-1, "terminal unavailable")
		), patch("trade_execution.mt5.order_send") as order_send:
			self._run_at(folder, 7, 25)
			self._run_at(folder, 7, 30)
			with (Path(folder) / "trade_logs" / rotation.LOG_FILE_NAME).open(
				"r", newline="", encoding="utf-8"
			) as handle:
				logs = list(csv.DictReader(handle))

		failures = [row for row in logs if row["phase"] == "fallback"]
		self.assertEqual(len(failures), 2)
		self.assertTrue(all(row["result"] == "close_failed" for row in failures))
		self.assertIn("Failed to get tick for closing SYM1", failures[0]["message"])
		self.assertIn("terminal unavailable", failures[0]["message"])
		order_send.assert_not_called()

	def test_young_profitable_position_prevents_loss_fallback(self) -> None:
		positions = [self._position(ticket, profit=-1) for ticket in range(1, 9)]
		positions[-1] = self._position(8, age_hours=0.5, profit=1)
		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch("position_rotation_strategy.close_position_by_ticket") as close, patch(
			"position_rotation_strategy.get_position_charged_costs", return_value=0
		):
			for minute in range(25, 30):
				self._run_at(folder, 7, minute)
			self._run_at(folder, 7, 30)

		close.assert_not_called()

	def test_fallback_runs_when_monitor_skips_a_minute_inside_observed_window(self) -> None:
		positions = [self._position(ticket, profit=-1) for ticket in range(1, 9)]
		def close_position(*, position_ticket: int, **_kwargs: object) -> bool:
			positions[:] = [position for position in positions if position.ticket != position_ticket]
			return True

		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch(
			"position_rotation_strategy.close_position_by_ticket", side_effect=close_position
		) as close, patch(
			"position_rotation_strategy.get_position_charged_costs", return_value=0
		):
			for minute in (25, 26, 28, 29, 30):
				self._run_at(folder, 7, minute)
			self.assertEqual(close.call_count, 1)
			state = json.loads(
				(Path(folder) / "trade_logs" / rotation.STATE_FILE_NAME).read_text(encoding="utf-8")
			)
		self.assertTrue(state["fallback_evaluated"])

	def test_fallback_is_skipped_when_no_scan_ran_before_minute_30(self) -> None:
		positions = [self._position(ticket, profit=-1) for ticket in range(1, 9)]
		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch("position_rotation_strategy.close_position_by_ticket") as close, patch(
			"position_rotation_strategy.get_position_charged_costs", return_value=0
		):
			self._run_at(folder, 7, 30)
			close.assert_not_called()
			state = json.loads(
				(Path(folder) / "trade_logs" / rotation.STATE_FILE_NAME).read_text(encoding="utf-8")
			)
		self.assertTrue(state["fallback_evaluated"])

	def test_does_not_run_when_account_has_seven_or_fewer_positions(self) -> None:
		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy._snapshot_positions", return_value=[]
		) as positions, patch("position_rotation_strategy.close_position_by_ticket") as close:
			self._run_at(folder, 7, 25)

		positions.assert_called_once()
		close.assert_not_called()

	def test_dry_run_does_not_send_orders(self) -> None:
		positions = [self._position(ticket, profit=-1) for ticket in range(1, 10)]
		with tempfile.TemporaryDirectory() as folder, patch.dict(
			os.environ, {"POSITION_ROTATION_STRATEGY_DRY_RUN": "true"}
		), patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch("position_rotation_strategy.get_position_charged_costs", return_value=0), patch(
			"position_rotation_strategy.close_position_by_ticket"
		) as close:
			for minute in range(25, 30):
				self._run_at(folder, 7, minute)
			self._run_at(folder, 7, 30)

		close.assert_not_called()

	def test_dry_run_simulates_account_floor_across_minute_checks(self) -> None:
		positions = [self._position(ticket, profit=1) for ticket in range(1, 11)]
		with tempfile.TemporaryDirectory() as folder, patch.dict(
			os.environ, {"POSITION_ROTATION_STRATEGY_DRY_RUN": "true"}
		), patch(
			"position_rotation_strategy._snapshot_positions",
			side_effect=self._snapshot_side_effect(positions),
		), patch("position_rotation_strategy.close_position_by_ticket") as close:
			for minute in range(25, 30):
				self._run_at(folder, 7, minute)
			self._run_at(folder, 7, 30)
			with (Path(folder) / "trade_logs" / rotation.LOG_FILE_NAME).open(
				"r", newline="", encoding="utf-8"
			) as handle:
				logs = list(csv.DictReader(handle))

		close.assert_not_called()
		planned = [row for row in logs if row["phase"] == "profit"]
		self.assertEqual(len(planned), 3)
		self.assertEqual([row["open_position_count"] for row in planned], ["10", "9", "8"])

	def test_unavailable_cost_history_prevents_any_close(self) -> None:
		positions = [self._position(ticket, profit=4.25, swap=-0.75) for ticket in range(1, 9)]
		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy.mt5.positions_get", return_value=tuple(positions)
		), patch(
			"position_rotation_strategy.get_position_charged_costs",
			side_effect=RuntimeError("deal history unavailable"),
		), patch("position_rotation_strategy.close_position_by_ticket") as close:
			with self.assertRaisesRegex(RuntimeError, "deal history unavailable"):
				self._run_at(folder, 7, 25)

		close.assert_not_called()

	def test_profit_eligibility_uses_swap_and_charged_costs(self) -> None:
		positions = [self._position(1, profit=0.5, swap=-0.1)] + [
			self._position(ticket, profit=-1) for ticket in range(2, 9)
		]

		def costs(position: SimpleNamespace) -> float:
			return -0.5 if position.ticket == 1 else 0.0

		with tempfile.TemporaryDirectory() as folder, patch(
			"position_rotation_strategy.mt5.positions_get", return_value=tuple(positions)
		), patch(
			"position_rotation_strategy.get_position_charged_costs", side_effect=costs
		), patch("position_rotation_strategy.close_position_by_ticket") as close:
			self._run_at(folder, 7, 25)

		close.assert_not_called()

	def _snapshots(self, positions: list[SimpleNamespace], now: datetime) -> list[rotation.PositionSnapshot]:
		return [
			rotation.PositionSnapshot(
				ticket=position.ticket,
				symbol=position.symbol,
				position_type=position.type,
				volume=position.volume,
				opened_at=rotation._position_time_to_utc(position.time).timestamp(),
				age_seconds=(
					now.timestamp() - rotation._position_time_to_utc(position.time).timestamp()
				),
				profit=position.profit,
				swap=position.swap,
				charged_costs=0,
				net_profit=round(position.profit + position.swap, 2),
			)
			for position in positions
		]


if __name__ == "__main__":
	unittest.main()
