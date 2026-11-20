import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import MetaTrader5 as mt5

import gemini_vertex
import position_rotation_shadow as shadow
from position_rotation_strategy import PositionSnapshot


class RotationShadowTests(unittest.TestCase):
	def setUp(self) -> None:
		self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
		self.now = datetime(2026, 11, 20, 7, 30, tzinfo=timezone.utc)

	def _candidate(self, ticket, symbol, side, net):
		return PositionSnapshot(ticket, symbol, side, 0.01, 0, 7200, net, 0, 0, net)

	def _events(self):
		return [json.loads(row) for row in (self.folder / "trade_logs" / shadow._LOG).read_text().splitlines()]

	def test_local_comparison_and_both_followups_use_the_same_snapshot(self) -> None:
		candidates = [
			self._candidate(101, "EURUSD_ecn", mt5.POSITION_TYPE_BUY, -2),
			self._candidate(102, "GBPUSD_ecn", mt5.POSITION_TYPE_BUY, -3),
			self._candidate(103, "USDJPY_ecn", mt5.POSITION_TYPE_SELL, -5),
		]
		scores = [
			{"symbol": "EURUSD", "score": -10, "reasoning": "Mild drop"},
			{"symbol": "USDJPY", "score": 60, "reasoning": "Uptrend"},
		]
		convert = lambda value: datetime.fromtimestamp(value, tz=timezone.utc)
		initial = SimpleNamespace(bid=100, ask=101, time=self.now.timestamp())
		with patch.object(shadow, "_signal", ((self.folder, shadow._window_key(self.now)), self.now, scores)), patch(
			"position_rotation_shadow.get_symbol_tick", return_value=initial
		), patch("position_rotation_shadow.mt5.order_calc_margin", return_value=20), patch(
			"position_rotation_shadow.mt5.order_send"
		) as send:
			shadow.record_fallback(self.folder, self.now, candidates, 2, False, convert)
			for hour in (1, 3):
				later = self.now + timedelta(hours=hour)
				with patch(
					"position_rotation_shadow.get_symbol_tick",
					return_value=SimpleNamespace(bid=103, ask=104, time=later.timestamp()),
				), patch("position_rotation_shadow.estimate_order_profit", side_effect=(
					lambda _symbol, side, _volume, start, end: (end - start) * (1 if side == "BUY" else -1)
				)):
					shadow.maintain(self.folder, later, convert)
		send.assert_not_called()
		comparison, first, third = self._events()
		self.assertEqual(comparison["baseline_one"], 101)
		self.assertEqual(comparison["baseline_rotation"], [101, 102])
		self.assertEqual(comparison["gemini_ticket"], 103)
		self.assertEqual([first["hour"], third["hour"]], [1, 3])
		self.assertEqual(third["positions"][-1]["estimated_net_if_held"], -8)
		self.assertEqual(shadow._load_pending(self.folder), [])

	def test_late_signal_is_not_used_and_missed_followup_is_labeled(self) -> None:
		candidate = self._candidate(101, "EURUSD_ecn", mt5.POSITION_TYPE_BUY, -2)
		scores = [{"symbol": "EURUSD", "score": -60, "reasoning": "Downtrend"}]
		with patch.object(
			shadow, "_signal", ((self.folder, shadow._window_key(self.now)), self.now + timedelta(seconds=1), scores),
		), patch("position_rotation_shadow.get_symbol_tick", return_value=None):
			shadow.record_fallback(self.folder, self.now, [candidate], 1, False, lambda _: self.now)
			shadow.maintain(self.folder, self.now + timedelta(hours=4), lambda _: self.now)
		comparison, first, third = self._events()
		self.assertEqual(comparison["status"], "no_market_scores")
		self.assertIsNone(comparison["gemini_ticket"])
		self.assertEqual([event["positions"][0]["status"] for event in (first, third)], ["missed", "missed"])

	def test_replacement_is_logged_and_tracked_without_sending_an_order(self) -> None:
		outgoing = self._candidate(101, "EURUSD_ecn", mt5.POSITION_TYPE_BUY, -2)
		scores = [
			{"symbol": "EURUSD", "score": -60, "reasoning": "Downtrend"},
			{"symbol": "USDJPY", "score": 40, "reasoning": "Uptrend"},
		]
		convert = lambda value: datetime.fromtimestamp(value, tz=timezone.utc)
		plan = SimpleNamespace(lot_size=0.01, synthetic_stop_price=95, take_profit_price=120, risk_usd=7)
		with patch.object(shadow, "_signal", ((self.folder, shadow._window_key(self.now)), self.now, scores)), patch(
			"position_rotation_shadow.get_swap_block_window",
			return_value=SimpleNamespace(contains=lambda _: False),
		), patch(
			"position_rotation_shadow.get_account_state",
			return_value={"margin_free": 100, "raw_margin_free": 100, "raw_balance": 1050},
		), patch("position_rotation_shadow._fresh_entry_data", return_value={"example": True}), patch(
			"position_rotation_shadow.validate_trend_following_signal", return_value=SimpleNamespace(allowed=True),
		), patch("position_rotation_shadow.is_symbol_blacklisted", return_value=False), patch(
			"position_rotation_shadow.get_symbol_info", return_value=SimpleNamespace(trade_mode=mt5.SYMBOL_TRADE_MODE_FULL),
		), patch("position_rotation_shadow.calculate_synthetic_risk_plan", return_value=plan), patch(
			"position_rotation_shadow.validate_lot_size", return_value=(0.01, ""),
		), patch("position_rotation_shadow.mt5.order_calc_margin", return_value=20), patch(
			"position_rotation_shadow.estimate_order_profit",
			side_effect=lambda _symbol, _side, _volume, start, end: end - start,
		), patch("position_rotation_shadow.mt5.order_send") as send:
			with patch(
				"position_rotation_shadow.get_symbol_tick",
				return_value=SimpleNamespace(bid=100, ask=101, time=self.now.timestamp()),
			):
				for minute_delta in (-1, 5):
					shadow.record_fallback(
						self.folder, self.now + timedelta(minutes=minute_delta), [outgoing], 1, False, convert,
					)
				self.assertFalse((self.folder / "trade_logs" / shadow._LOG).exists())
				shadow.record_fallback(
					self.folder, self.now, [outgoing], 1, False, convert,
					open_symbols=("EURUSD_ecn", "GBPUSD_ecn"), position_cap=2,
				)
				shadow.record_fallback(
					self.folder, self.now, [outgoing], 1, False, convert,
					open_symbols=("EURUSD_ecn", "GBPUSD_ecn"), position_cap=3,
				)
			later = self.now + timedelta(hours=1)
			with patch(
				"position_rotation_shadow.get_symbol_tick",
				return_value=SimpleNamespace(bid=103, ask=104, time=later.timestamp()),
			):
				shadow.maintain(self.folder, later, convert)

		send.assert_not_called()
		swap, keep, swap_followup, _ = self._events()
		self.assertEqual(swap["shadow_decision"], "swap")
		self.assertEqual(swap["replacement"]["close_ticket"], 101)
		self.assertEqual(swap["replacement"]["entry"]["symbol"], "USDJPY_ecn")
		self.assertEqual(keep["shadow_decision"], "keep")
		self.assertIn("entry_possible_without_rotation", [row["reason"] for row in keep["entry_rejections"]])
		self.assertEqual(swap_followup["positions"][-1]["estimated_net_if_held"], 0.7)

	def test_gemini_request_contains_only_public_market_features(self) -> None:
		features = {"EURUSD": {"H1": [0.1], "H4": [0.2]}}

		def request(*, prompt, validator, **_kwargs):
			self.assertEqual(json.loads(prompt.split("\n", 1)[1]), features)
			return validator({"scores": [{"symbol": "EURUSD", "score": 20, "reasoning": "Higher closes"}]})

		with patch.object(gemini_vertex, "_request_structured_json", side_effect=request):
			config = SimpleNamespace()
			result = gemini_vertex.request_rotation_market_scores(config, features)
		self.assertEqual(result[0]["score"], 20)

	def test_market_collection_uses_only_closed_bars_for_fixed_watchlist(self) -> None:
		with patch("position_rotation_shadow._fresh_tick", return_value=object()), patch(
			"position_rotation_shadow.mt5.copy_rates_from_pos"
		) as rates, patch("position_rotation_shadow.mt5.positions_get") as positions:
			bars = rates.return_value
			bars.__len__.return_value = 13
			bars.__getitem__.return_value = [100.0] * 12 + [105.0]
			features = shadow._public_features(self.now, lambda _: self.now)

		positions.assert_not_called()
		self.assertEqual(set(features), set(shadow._PUBLIC_SYMBOLS))
		self.assertEqual({call.args[2:] for call in rates.call_args_list}, {(1, 13)})
		self.assertEqual(features["EURUSD"]["H1"], [0.0] * 11 + [5.0])
