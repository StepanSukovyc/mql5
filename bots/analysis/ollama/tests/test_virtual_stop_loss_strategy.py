"""Unit tests for the experimental Thursday/Friday virtual stop-loss strategy."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import virtual_stop_loss_strategy
from virtual_stop_loss_strategy import (
	calculate_virtual_stop_loss_price,
	is_virtual_stop_loss_registration_day,
	register_virtual_stop_loss,
	run_virtual_stop_loss_strategy_if_due,
)


def _write_state(folder: Path, entries: dict) -> Path:
	state_dir = folder / "trade_logs"
	state_dir.mkdir(parents=True, exist_ok=True)
	state_path = state_dir / "virtual_stop_loss_state.json"
	state_path.write_text(json.dumps(entries), encoding="utf-8")
	return state_path


def _state_entry(**overrides) -> dict:
	entry = {
		"ticket": 123,
		"symbol": "EURUSD_ecn",
		"action": "BUY",
		"strategy_id": "gemini_primary",
		"magic": 234000,
		"volume": 0.01,
		"entry_price": 1.2,
		"stop_loss_price": 1.19,
		"synthetic_stop_distance": 0.01,
		"registered_at_utc": "2026-11-12T10:00:00+00:00",
	}
	entry.update(overrides)
	return entry


def _position(**overrides) -> SimpleNamespace:
	values = {"ticket": 123, "symbol": "EURUSD_ecn", "type": 0, "volume": 0.01}
	values.update(overrides)
	return SimpleNamespace(**values)


class RegistrationDayTests(unittest.TestCase):
	def test_prague_thursday_and_friday_are_registration_days(self) -> None:
		# 2026-11-12 is Thursday, 2026-11-13 is Friday.
		self.assertTrue(is_virtual_stop_loss_registration_day(datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)))
		self.assertTrue(is_virtual_stop_loss_registration_day(datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)))
		self.assertFalse(is_virtual_stop_loss_registration_day(datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)))

	def test_day_is_evaluated_in_prague_not_utc(self) -> None:
		# Wednesday 23:30 UTC is already Thursday 00:30 in Prague (UTC+1 in November).
		self.assertTrue(is_virtual_stop_loss_registration_day(datetime(2026, 11, 11, 23, 30, tzinfo=timezone.utc)))
		# Friday 23:30 UTC is already Saturday in Prague.
		self.assertFalse(is_virtual_stop_loss_registration_day(datetime(2026, 11, 13, 23, 30, tzinfo=timezone.utc)))


class StopLossPriceTests(unittest.TestCase):
	@patch("virtual_stop_loss_strategy.get_symbol_info", return_value=SimpleNamespace(digits=5))
	def test_price_is_derived_from_actual_fill(self, _mock_info) -> None:
		self.assertEqual(calculate_virtual_stop_loss_price("EURUSD_ecn", "BUY", 1.2, 0.015), 1.185)
		self.assertEqual(calculate_virtual_stop_loss_price("EURUSD_ecn", "SELL", 1.2, 0.015), 1.215)

	def test_invalid_inputs_return_none(self) -> None:
		self.assertIsNone(calculate_virtual_stop_loss_price("EURUSD_ecn", "BUY", 0.0, 0.015))
		self.assertIsNone(calculate_virtual_stop_loss_price("EURUSD_ecn", "BUY", 1.2, 0.0))
		self.assertIsNone(calculate_virtual_stop_loss_price("EURUSD_ecn", "HOLD", 1.2, 0.015))


class AIStopLossContextTests(unittest.TestCase):
	@patch("virtual_stop_loss_strategy.request_stop_loss_decision")
	@patch("virtual_stop_loss_strategy.load_gemini_api_config")
	@patch("virtual_stop_loss_strategy.collect_symbol_payload")
	def test_ai_receives_market_context_and_actual_fill(self, collect, config, request) -> None:
		market = {
			"current_price": 1.2,
			"candles": {"1h": [{"close": 1.2}], "4h": [{"close": 1.19}]},
			"oscillators": {"1h": {"rsi": [55]}, "4h": {"rsi": [52]}},
		}
		collect.return_value = market
		request.return_value = {"stop_loss_price": 1.195, "reasoning": "Support"}
		result = virtual_stop_loss_strategy._request_ai_stop_loss("EURUSD", "BUY", 1.201, 1.19)
		self.assertEqual(result["stop_loss_price"], 1.195)
		context = json.loads(request.call_args.args[1].split("\n", 1)[1])
		self.assertEqual(context["market"], market)
		self.assertEqual(context["entry_price"], 1.201)
		self.assertEqual(context["emergency_stop_price"], 1.19)
		self.assertIs(request.call_args.args[0], config.return_value)

	@patch("virtual_stop_loss_strategy.request_stop_loss_decision")
	@patch("virtual_stop_loss_strategy.collect_symbol_payload")
	def test_missing_candles_do_not_reach_model(self, collect, request) -> None:
		collect.return_value = {
			"current_price": 1.2, "candles": {"1h": [], "4h": []},
			"oscillators": {"1h": {}, "4h": {}},
		}
		with self.assertRaisesRegex(ValueError, "Missing 1h"):
			virtual_stop_loss_strategy._request_ai_stop_loss("EURUSD", "BUY", 1.2, 1.19)
		request.assert_not_called()


class RegistrationTests(unittest.TestCase):
	def setUp(self) -> None:
		self.ai = self.enterContext(patch(
			"virtual_stop_loss_strategy._request_ai_stop_loss",
			return_value={"stop_loss_price": 1.195, "reasoning": "Support invalidation"},
		))

	def _register(self, service_folder: Path, now_utc: datetime):
		return register_virtual_stop_loss(
			service_folder=service_folder,
			position_ticket=123,
			symbol="EURUSD_ecn",
			action="BUY",
			filled_price=1.2,
			synthetic_stop_distance=0.01,
			volume=0.01,
			strategy_id="gemini_primary",
			magic=234000,
			now_utc=now_utc,
		)

	@patch("virtual_stop_loss_strategy.get_symbol_info", return_value=SimpleNamespace(digits=5))
	@patch("virtual_stop_loss_strategy.is_virtual_stop_loss_enabled", return_value=True)
	def test_thursday_trade_is_registered_in_dedicated_state_file(self, _mock_enabled, _mock_info) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			sl = self._register(Path(temp_dir), datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc))
			state = json.loads(
				(Path(temp_dir) / "trade_logs" / "virtual_stop_loss_state.json").read_text(encoding="utf-8")
			)

		self.assertEqual(sl, 1.195)
		self.assertEqual(state["123"]["stop_loss_price"], 1.195)
		self.assertEqual(state["123"]["stop_loss_source"], "gemini")
		self.assertEqual(state["123"]["ai_reasoning"], "Support invalidation")
		self.assertEqual(state["123"]["emergency_stop_loss_price"], 1.19)
		self.assertEqual(state["123"]["action"], "BUY")
		self.assertEqual(state["123"]["entry_price"], 1.2)

	@patch("virtual_stop_loss_strategy.get_symbol_info", return_value=SimpleNamespace(digits=5))
	@patch("virtual_stop_loss_strategy.is_virtual_stop_loss_enabled", return_value=True)
	def test_ai_failure_keeps_persisted_emergency_stop(self, _enabled, _info) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			folder = Path(temp_dir)
			def fail(*args):
				state = json.loads((folder / "trade_logs" / "virtual_stop_loss_state.json").read_text())
				self.assertEqual(state["123"]["stop_loss_price"], 1.19)
				raise RuntimeError("AI unavailable")
			self.ai.side_effect = fail
			sl = self._register(folder, datetime(2026, 11, 12, 12, tzinfo=timezone.utc))
			state = json.loads((folder / "trade_logs" / "virtual_stop_loss_state.json").read_text())
		self.assertEqual(sl, 1.19)
		self.assertEqual(state["123"]["stop_loss_source"], "emergency_atr")
		self.assertIn("AI unavailable", state["123"]["ai_error"])

	@patch("virtual_stop_loss_strategy.get_symbol_info", return_value=SimpleNamespace(digits=5))
	@patch("virtual_stop_loss_strategy.is_virtual_stop_loss_enabled", return_value=True)
	def test_rounding_to_entry_price_retains_emergency_stop(self, _enabled, _info) -> None:
		self.ai.return_value = {"stop_loss_price": 1.1999999, "reasoning": "Too close"}
		with tempfile.TemporaryDirectory() as temp_dir:
			sl = self._register(Path(temp_dir), datetime(2026, 11, 12, 12, tzinfo=timezone.utc))
		self.assertEqual(sl, 1.19)

	@patch("virtual_stop_loss_strategy.get_symbol_info", return_value=SimpleNamespace(digits=5))
	@patch("virtual_stop_loss_strategy.is_virtual_stop_loss_enabled", return_value=True)
	def test_monitor_removed_position_is_not_resurrected_after_ai(self, _enabled, _info) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			folder = Path(temp_dir)
			def closed_during_request(*args):
				_write_state(folder, {})
				return {"stop_loss_price": 1.195, "reasoning": "Support"}
			self.ai.side_effect = closed_during_request
			self._register(folder, datetime(2026, 11, 12, 12, tzinfo=timezone.utc))
			state = json.loads((folder / "trade_logs" / "virtual_stop_loss_state.json").read_text())
		self.assertEqual(state, {})

	@patch("virtual_stop_loss_strategy.is_virtual_stop_loss_enabled", return_value=True)
	def test_wednesday_trade_is_not_registered(self, _mock_enabled) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			sl = self._register(Path(temp_dir), datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc))
			state_path = Path(temp_dir) / "trade_logs" / "virtual_stop_loss_state.json"
			self.assertIsNone(sl)
			self.assertFalse(state_path.exists())
			self.ai.assert_not_called()

	@patch("virtual_stop_loss_strategy.is_virtual_stop_loss_enabled", return_value=False)
	def test_disabled_flag_skips_registration(self, _mock_enabled) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			sl = self._register(Path(temp_dir), datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc))
			self.assertIsNone(sl)


class MonitoringTests(unittest.TestCase):
	def setUp(self) -> None:
		virtual_stop_loss_strategy._LAST_EVALUATED_MINUTE_KEY = None

	def _run(self, temp_dir: str, *, positions, tick, dry_run=False, close_result=True):
		patches = [
			patch("virtual_stop_loss_strategy.is_virtual_stop_loss_enabled", return_value=True),
			patch("virtual_stop_loss_strategy._get_dry_run", return_value=dry_run),
			patch("virtual_stop_loss_strategy._get_service_folder", return_value=Path(temp_dir)),
			patch("virtual_stop_loss_strategy.get_swap_block_window"),
			patch("virtual_stop_loss_strategy.mt5.positions_get", return_value=positions),
			patch("virtual_stop_loss_strategy.get_symbol_tick", return_value=tick),
			patch("virtual_stop_loss_strategy.close_position_by_ticket", return_value=close_result),
		]
		started = [item.start() for item in patches]
		try:
			started[3].return_value.contains.return_value = False
			run_virtual_stop_loss_strategy_if_due()
			return started[6]
		finally:
			for item in patches:
				item.stop()

	def _read_state(self, temp_dir: str) -> dict:
		return json.loads(
			(Path(temp_dir) / "trade_logs" / "virtual_stop_loss_state.json").read_text(encoding="utf-8")
		)

	def test_buy_position_closes_when_bid_drops_below_stop_loss(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			_write_state(Path(temp_dir), {"123": _state_entry()})
			mock_close = self._run(
				temp_dir,
				positions=(_position(),),
				tick=SimpleNamespace(bid=1.189, ask=1.1895),
			)
			state = self._read_state(temp_dir)

		mock_close.assert_called_once_with(
			position_ticket=123,
			symbol="EURUSD_ecn",
			position_type=0,
			volume=0.01,
			comment="vsl:gemini_primary",
			magic=234000,
		)
		self.assertEqual(state, {})

	def test_sell_position_closes_when_ask_rises_above_stop_loss(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			_write_state(
				Path(temp_dir),
				{"123": _state_entry(action="SELL", stop_loss_price=1.21, type=1)},
			)
			mock_close = self._run(
				temp_dir,
				positions=(_position(type=1),),
				tick=SimpleNamespace(bid=1.2095, ask=1.211),
			)
			state = self._read_state(temp_dir)

		mock_close.assert_called_once()
		self.assertEqual(state, {})

	def test_position_above_stop_loss_is_kept_open(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			_write_state(Path(temp_dir), {"123": _state_entry()})
			mock_close = self._run(
				temp_dir,
				positions=(_position(),),
				tick=SimpleNamespace(bid=1.195, ask=1.1955),
			)
			state = self._read_state(temp_dir)

		mock_close.assert_not_called()
		self.assertIn("123", state)

	def test_entry_for_externally_closed_position_is_removed(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			_write_state(Path(temp_dir), {"123": _state_entry()})
			mock_close = self._run(
				temp_dir,
				positions=(),
				tick=SimpleNamespace(bid=1.189, ask=1.1895),
			)
			state = self._read_state(temp_dir)

		mock_close.assert_not_called()
		self.assertEqual(state, {})

	def test_dry_run_logs_without_closing_and_keeps_entry(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			_write_state(Path(temp_dir), {"123": _state_entry()})
			mock_close = self._run(
				temp_dir,
				positions=(_position(),),
				tick=SimpleNamespace(bid=1.189, ask=1.1895),
				dry_run=True,
			)
			state = self._read_state(temp_dir)
			log_text = (Path(temp_dir) / "trade_logs" / "virtual_stop_loss.csv").read_text(encoding="utf-8")

		mock_close.assert_not_called()
		self.assertIn("123", state)
		self.assertIn("DRY-RUN", log_text)

	def test_disabled_flag_stops_monitoring_of_open_positions(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			_write_state(Path(temp_dir), {"123": _state_entry()})
			with patch(
				"virtual_stop_loss_strategy.is_virtual_stop_loss_enabled", return_value=False
			), patch("virtual_stop_loss_strategy.mt5.positions_get") as mock_positions, patch(
				"virtual_stop_loss_strategy.close_position_by_ticket"
			) as mock_close:
				run_virtual_stop_loss_strategy_if_due()

		mock_positions.assert_not_called()
		mock_close.assert_not_called()

	def test_failed_close_keeps_entry_for_retry(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			_write_state(Path(temp_dir), {"123": _state_entry()})
			mock_close = self._run(
				temp_dir,
				positions=(_position(),),
				tick=SimpleNamespace(bid=1.189, ask=1.1895),
				close_result=False,
			)
			state = self._read_state(temp_dir)

		mock_close.assert_called_once()
		self.assertIn("123", state)


if __name__ == "__main__":
	unittest.main()
