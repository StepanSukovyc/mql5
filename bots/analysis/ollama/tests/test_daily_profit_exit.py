from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import MetaTrader5 as mt5

import daily_profit_exit_strategy as strategy
from gemini_vertex import GeminiVertexRequestError, _validate_exit_decisions, request_exit_decisions


class DailyProfitExitTests(unittest.TestCase):
	def setUp(self):
		self.temp = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.stack.enter_context(patch.dict(os.environ, {
			"SERVICE_DEST_FOLDER": self.temp.name,
			"DAILY_PROFIT_EXIT_ENABLED": "true", "DAILY_PROFIT_EXIT_DRY_RUN": "false",
			"DAILY_PROFIT_EXIT_MIN_RETAINED_USD": "1", "DAILY_PROFIT_EXIT_SLIPPAGE_BUFFER_USD": "1",
		}))
		self.now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
		self.clock = self.mock("_now", return_value=self.now)
		self.mock("get_swap_block_window", return_value=SimpleNamespace(contains=lambda _: False))
		self.original_market_context = strategy._market_context
		self.mock("_market_context", return_value={"price": 100})
		self.mock("load_gemini_api_config", return_value=SimpleNamespace(model="gemini-4-argon"))
		self.pnl = self.mock("realized_today", return_value=20)
		self.costs = self.mock("get_position_charged_costs", return_value=-0.2)
		self.mock("get_symbol_tick", return_value=SimpleNamespace(time=self.now.timestamp(), bid=100, ask=101))
		self.mock("estimate_order_profit", return_value=-5)
		self.position = SimpleNamespace(
			ticket=1, identifier=101, symbol="TEST", type=mt5.POSITION_TYPE_BUY,
			volume=0.01, time=1000, magic=0, comment="manual",
			profit=-5, swap=-1, tp=110, price_open=105,
		)
		self.positions = self.mock("_positions", return_value=[self.position])
		self.account = self.stack.enter_context(patch.object(strategy.mt5, "account_info", return_value=SimpleNamespace(
			balance=10000, equity=9900, margin_free=1000, currency="USD", login=123, server="test",
		)))
		self.ai = self.mock("request_exit_decisions", return_value={"decisions": [
			{"ticket": 1, "action": "CLOSE", "reasoning": "market changed"},
		]})
		self.close = self.mock("close_position_by_ticket", return_value=True)

	def mock(self, name, **kwargs):
		return self.stack.enter_context(patch.object(strategy, name, **kwargs))

	def events(self):
		path = Path(self.temp.name) / "trade_logs" / "daily_profit_exit.jsonl"
		return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

	def test_hold_all_never_closes_and_does_not_repeat_hourly_call(self):
		self.ai.return_value["decisions"][0]["action"] = "HOLD"
		strategy.evaluate_daily_profit_exit()
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()
		self.ai.assert_called_once()

	def test_live_close_includes_manual_positions_and_preserves_magic(self):
		self.mock("_reconcile", return_value=True)
		strategy.evaluate_daily_profit_exit()
		self.close.assert_called_once_with(
			1, "TEST", mt5.POSITION_TYPE_BUY, 0.01,
			comment="Daily profit Gemini exit", magic=0,
		)
		prompt = self.ai.call_args.args[1]
		self.assertIn('"magic": 0', prompt)
		self.assertIn('"tp": 110', prompt)
		self.assertIn("original entry thesis is unknown", prompt)
		self.assertIn("pending_close", strategy._load())
		events = self.events()
		request = next(event for event in events if event["event"] == "ai_request")
		review = next(event for event in events if event["event"] == "ai_review")
		self.assertEqual(request["prompt"], prompt)
		self.assertEqual(request["model"], "gemini-4-argon")
		self.assertEqual(review["model"], "gemini-4-argon")
		self.assertEqual(review["decisions"][0]["reasoning"], "market changed")

	def test_prompt_is_logged_even_when_gemini_request_fails(self):
		self.ai.side_effect = RuntimeError("Gemini unavailable")
		with self.assertRaises(RuntimeError):
			strategy.evaluate_daily_profit_exit()
		events = self.events()
		request = next(event for event in events if event["event"] == "ai_request")
		self.assertIn("realized_today_usd", request["prompt"])
		self.assertEqual(request["snapshot"], json.loads(request["prompt"].split("\n", 1)[1]))
		self.assertFalse(any(event["event"] == "ai_review" for event in events))

	def test_hold_review_logs_exact_input_snapshot_before_ai_call(self):
		market = {
			"generated_at": self.now.isoformat(), "price": 100,
			"spread": {"bid": 100, "ask": 101},
			"candles": {tf: [{"close": 100}] for tf in ("1h", "4h", "day")},
			"indicators": {tf: {
				name: {"value": 20}
				for name in ("rsi", "ema20", "ema50", "ema200", "atr14", "adx14")
			} for tf in ("1h", "4h", "day")},
		}
		self.mock("_market_context", return_value=market)
		def hold(_config, prompt, _tickets):
			request = self.events()[-1]
			self.assertEqual(request["event"], "ai_request")
			snapshot = request["snapshot"]
			self.assertEqual(snapshot, json.loads(prompt.split("\n", 1)[1]))
			self.assertEqual(snapshot["markets"], {"TEST": market})
			self.assertEqual(snapshot["as_of_utc"], self.now.isoformat())
			self.assertEqual(snapshot["realized_today_usd"], 20)
			self.assertEqual(snapshot["min_retained_usd"], 1)
			self.assertEqual(snapshot["slippage_buffer_usd"], 1)
			self.assertEqual(snapshot["balance"], 10000)
			self.assertEqual(snapshot["equity"], 9900)
			self.assertEqual(snapshot["margin_free"], 1000)
			position = snapshot["positions"][0]
			self.assertEqual(position["ticket"], 1)
			self.assertEqual(position["profit"], -5)
			self.assertEqual(position["swap"], -1)
			self.assertEqual(position["net_profit_usd"], -6.2)
			self.assertEqual(position["incremental_close_pnl_usd"], -6)
			return {"decisions": [{"ticket": 1, "action": "HOLD", "reasoning": "trend intact"}]}
		self.ai.side_effect = hold
		strategy.evaluate_daily_profit_exit()
		self.ai.assert_called_once()
		self.close.assert_not_called()
		self.assertEqual(self.events()[-1]["event"], "ai_review")

	def test_scalping_positions_with_tp_are_not_excluded(self):
		self.position.magic = 234600
		self.mock("_reconcile", return_value=True)
		strategy.evaluate_daily_profit_exit()
		self.assertEqual(self.close.call_args.kwargs["magic"], 234600)

	def test_profit_exhaustion_during_ai_blocks_close(self):
		self.pnl.side_effect = [20, 1.5]
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()
		self.assertEqual(self.events()[-1]["reason"], "insufficient_realized_budget")

	def test_net_costs_and_swap_are_in_loss_budget(self):
		self.position.profit = -18
		self.position.swap = -1
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()

	def test_budget_boundary_allows_retained_profit_plus_slippage(self):
		self.pnl.return_value = 8.2
		self.mock("_reconcile", return_value=True)
		strategy.evaluate_daily_profit_exit()
		self.close.assert_called_once()

	def test_dry_run_only_audits(self):
		os.environ["DAILY_PROFIT_EXIT_DRY_RUN"] = "true"
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()
		self.assertEqual(self.events()[-1]["event"], "would_close")
		self.assertNotIn("pending_close", strategy._load())

	def test_zero_budget_does_not_call_ai(self):
		self.pnl.return_value = 0
		strategy.evaluate_daily_profit_exit()
		self.ai.assert_not_called()

	def test_non_usd_account_fails_before_ai(self):
		self.account.return_value.currency = "EUR"
		with self.assertRaises(RuntimeError):
			strategy.evaluate_daily_profit_exit()
		self.ai.assert_not_called()
		self.close.assert_not_called()

	def test_cutoff_is_prague_2115_and_rechecked_after_ai(self):
		late = datetime(2026, 11, 1, 20, 15, tzinfo=timezone.utc)
		self.clock.return_value = late
		strategy.evaluate_daily_profit_exit()
		self.ai.assert_not_called()
		self.clock.return_value = self.now
		self.ai.side_effect = lambda *_: (setattr(self.clock, "return_value", late) or {
			"decisions": [{"ticket": 1, "action": "CLOSE", "reasoning": "test"}],
		})
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()

	def test_shutdown_or_stale_review_prevents_execution(self):
		stop = threading.Event()
		self.ai.side_effect = lambda *_: (stop.set() or {
			"decisions": [{"ticket": 1, "action": "CLOSE", "reasoning": "test"}],
		})
		strategy.evaluate_daily_profit_exit(stop)
		self.close.assert_not_called()

	def test_stale_ai_response_prevents_execution(self):
		from datetime import timedelta
		self.ai.side_effect = lambda *_: (setattr(self.clock, "return_value", self.now + timedelta(minutes=11)) or {
			"decisions": [{"ticket": 1, "action": "CLOSE", "reasoning": "test"}],
		})
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()
		self.assertEqual(self.events()[-1]["reason"], "stale_review")

	def test_stale_quote_prevents_close(self):
		self.mock("get_symbol_tick", return_value=SimpleNamespace(time=self.now.timestamp() - 121))
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()
		self.assertEqual(self.events()[-1]["reason"], "missing_or_stale_tick")

	def prepare_multiple_closes(self):
		second = SimpleNamespace(**vars(self.position))
		second.ticket = 2
		second.identifier = 102
		self.positions.return_value = [self.position, second]
		self.ai.return_value["decisions"].append({"ticket": 2, "action": "CLOSE", "reasoning": "test"})
		exits = {}
		self.stack.enter_context(patch.object(strategy.mt5, "history_deals_get", side_effect=lambda **kwargs: exits[kwargs["position"]]))
		def close(ticket, *_args, **_kwargs):
			position = next(p for p in self.positions.return_value if p.ticket == ticket)
			self.positions.return_value = [p for p in self.positions.return_value if p.ticket != ticket]
			self.pnl.return_value += position.profit + position.swap
			exits[position.identifier] = [SimpleNamespace(
				entry=mt5.DEAL_ENTRY_OUT, time=self.now.timestamp(),
				time_msc=int(self.now.timestamp() * 1000), ticket=100 + ticket,
			)]
			return True
		self.close.side_effect = close

	def test_multiple_confirmed_closes_use_one_ai_review_and_fresh_budget(self):
		self.prepare_multiple_closes()
		strategy.evaluate_daily_profit_exit()
		self.assertEqual([call.args[0] for call in self.close.call_args_list], [1, 2])
		self.ai.assert_called_once()
		self.assertEqual(self.pnl.return_value, 8)
		self.assertNotIn("pending_close", strategy._load())
		self.assertEqual(len([event for event in self.events() if event["event"] == "close_confirmed"]), 2)
		self.assertIn("may close multiple positions sequentially", self.ai.call_args.args[1])

	def test_next_close_uses_actual_realized_result_after_previous_close(self):
		self.prepare_multiple_closes()
		close = self.close.side_effect
		def worse_fill(*args, **kwargs):
			result = close(*args, **kwargs)
			self.pnl.return_value = 7
			return result
		self.close.side_effect = worse_fill
		strategy.evaluate_daily_profit_exit()
		self.close.assert_called_once()
		self.assertEqual(self.events()[-1]["reason"], "insufficient_realized_budget")
		self.assertNotIn("pending_close", strategy._load())

	def test_unconfirmed_first_close_blocks_remaining_recommendations(self):
		self.prepare_multiple_closes()
		self.close.side_effect = None
		self.stack.enter_context(patch.object(strategy.mt5, "history_deals_get", return_value=[
			SimpleNamespace(entry=mt5.DEAL_ENTRY_IN, time=1000, time_msc=1000000),
		]))
		strategy.evaluate_daily_profit_exit()
		self.close.assert_called_once()
		self.assertIn("pending_close", strategy._load())
		self.assertEqual(self.events()[-1]["event"], "close_confirmation_pending")

	def test_cutoff_after_first_confirmed_close_blocks_next(self):
		self.prepare_multiple_closes()
		close = self.close.side_effect
		def reach_cutoff(*args, **kwargs):
			result = close(*args, **kwargs)
			self.clock.return_value = datetime(2026, 11, 1, 20, 15, tzinfo=timezone.utc)
			return result
		self.close.side_effect = reach_cutoff
		strategy.evaluate_daily_profit_exit()
		self.close.assert_called_once()
		self.assertEqual(self.events()[-1]["reason"], "disabled_or_time_blocked")

	def test_multiple_dry_run_closes_respect_cumulative_loss_budget(self):
		self.prepare_multiple_closes()
		os.environ["DAILY_PROFIT_EXIT_DRY_RUN"] = "true"
		self.pnl.return_value = 14
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()
		self.assertEqual(len([event for event in self.events() if event["event"] == "would_close"]), 1)
		self.assertEqual(self.events()[-1]["reason"], "insufficient_realized_budget")

	def test_multiple_dry_run_closes_with_sufficient_budget_are_all_logged(self):
		self.prepare_multiple_closes()
		os.environ["DAILY_PROFIT_EXIT_DRY_RUN"] = "true"
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()
		self.assertEqual([event["ticket"] for event in self.events() if event["event"] == "would_close"], [1, 2])
		self.assertNotIn("pending_close", strategy._load())

	def test_failed_close_keeps_pending_marker_and_blocks_repetition(self):
		self.close.return_value = False
		self.stack.enter_context(patch.object(strategy.mt5, "history_deals_get", return_value=[
			SimpleNamespace(entry=mt5.DEAL_ENTRY_IN, time=1000, time_msc=1000000),
		]))
		strategy.evaluate_daily_profit_exit()
		self.assertIn("pending_close", strategy._load())
		strategy.evaluate_daily_profit_exit()
		self.close.assert_called_once()
		self.ai.assert_called_once()

	def test_nonfinite_costs_fail_closed(self):
		self.costs.return_value = float("nan")
		with self.assertRaises(ValueError):
			strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()

	def test_account_change_during_review_blocks_close(self):
		self.ai.side_effect = lambda *_: (setattr(self.account.return_value, "login", 456) or {
			"decisions": [{"ticket": 1, "action": "CLOSE", "reasoning": "test"}],
		})
		with self.assertRaises(RuntimeError):
			strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()

	def test_slow_final_history_read_blocks_stale_review(self):
		def profit(*_):
			if self.pnl.call_count == 2:
				self.clock.return_value = datetime(2026, 11, 1, 12, 11, tzinfo=timezone.utc)
			return 20
		self.pnl.side_effect = profit
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()
		self.assertEqual(self.events()[-1]["reason"], "stale_data_before_order")

	def test_disabled_during_final_history_read_blocks_close(self):
		def profit(*_):
			if self.pnl.call_count == 2:
				os.environ["DAILY_PROFIT_EXIT_ENABLED"] = "false"
			return 20
		self.pnl.side_effect = profit
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()

	def test_worker_is_nonblocking_and_prevents_duplicate_worker(self):
		entered = threading.Event()
		release = threading.Event()
		completed = threading.Event()
		def review(_stop):
			entered.set()
			self.assertTrue(release.wait(5))
		def audit(*_, **__):
			completed.set()
		with patch.object(strategy, "evaluate_daily_profit_exit", side_effect=review) as evaluate:
			with patch.object(strategy, "_audit", side_effect=audit):
				try:
					strategy.run_daily_profit_exit_strategy_if_due()
					self.assertTrue(entered.wait(5))
					self.assertTrue(strategy._WORKER_LOCK.locked())
					strategy.run_daily_profit_exit_strategy_if_due()
					evaluate.assert_called_once()
				finally:
					release.set()
				# Acquire waits until the worker's finally releases its process lock.
				self.assertTrue(strategy._WORKER_LOCK.acquire(timeout=5))
				strategy._WORKER_LOCK.release()
		self.assertFalse(completed.is_set())

	def test_market_context_requires_complete_finite_indicators(self):
		payload = {
			"generated_at": self.now.isoformat(), "current_price": 100, "spread_snapshot": {},
			"candles": {tf: [{"close": 100}] for tf in ("1h", "4h", "day")},
			"oscillators": {tf: {
				name: [{"value": 20}] for name in ("rsi", "ema20", "ema50", "ema200", "atr14", "adx14")
			} for tf in ("1h", "4h", "day")},
		}
		with patch.object(strategy, "collect_symbol_payload", return_value=payload):
			self.assertEqual(self.original_market_context("TEST")["indicators"]["1h"]["rsi"]["value"], 20)
			payload["oscillators"]["4h"]["ema200"] = []
			with self.assertRaises(RuntimeError):
				self.original_market_context("TEST")

	def test_existing_file_lock_blocks_worker(self):
		path, _ = strategy._paths()
		finished = threading.Event()
		def audit(event, **_):
			if event == "worker_lock_unavailable":
				finished.set()
		with path.with_suffix(".lock").open("a+b") as handle:
			handle.write(b"0")
			handle.flush()
			handle.seek(0)
			if os.name == "nt":
				strategy.msvcrt.locking(handle.fileno(), strategy.msvcrt.LK_NBLCK, 1)
			else:
				strategy.fcntl.flock(handle.fileno(), strategy.fcntl.LOCK_EX | strategy.fcntl.LOCK_NB)
			try:
				with patch.object(strategy, "_audit", side_effect=audit):
					with patch.object(strategy, "evaluate_daily_profit_exit") as evaluate:
						strategy.run_daily_profit_exit_strategy_if_due()
						self.assertTrue(finished.wait(5))
						self.assertTrue(strategy._WORKER_LOCK.acquire(timeout=5))
						strategy._WORKER_LOCK.release()
						evaluate.assert_not_called()
			finally:
				handle.seek(0)
				if os.name == "nt":
					strategy.msvcrt.locking(handle.fileno(), strategy.msvcrt.LK_UNLCK, 1)
				else:
					strategy.fcntl.flock(handle.fileno(), strategy.fcntl.LOCK_UN)

	def test_volume_change_or_disappeared_position_prevents_close(self):
		changed = SimpleNamespace(**vars(self.position))
		changed.volume = 0.02
		self.positions.side_effect = [[self.position], [changed]]
		strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()

	def test_missing_costs_fail_without_close(self):
		self.costs.side_effect = RuntimeError("history unavailable")
		with self.assertRaises(RuntimeError):
			strategy.evaluate_daily_profit_exit()
		self.close.assert_not_called()

	def test_failed_ai_attempt_is_persisted_before_request(self):
		self.ai.side_effect = GeminiVertexRequestError("bad response")
		with self.assertRaises(GeminiVertexRequestError):
			strategy.evaluate_daily_profit_exit()
		self.ai.reset_mock()
		strategy.evaluate_daily_profit_exit()
		self.ai.assert_not_called()
		self.close.assert_not_called()

	def test_pending_close_blocks_new_ai_until_history_confirms(self):
		state = {"pending_close": {"ticket": 1, "identifier": 101, "requested_at_msc": 1000}}
		strategy._save(state)
		deal = SimpleNamespace(entry=mt5.DEAL_ENTRY_OUT, time=2, time_msc=2000, ticket=999)
		with patch.object(strategy.mt5, "history_deals_get", return_value=[deal]):
			strategy.evaluate_daily_profit_exit()
			self.ai.assert_not_called()
			self.positions.return_value = []
			self.assertTrue(strategy._reconcile(state))
		self.assertNotIn("pending_close", strategy._load())
		self.assertEqual(self.events()[-1]["event"], "close_confirmed")

class DailyProfitExitAccountingTests(unittest.TestCase):
	def test_time_gate_calls_real_swap_window_with_keyword_argument(self):
		with patch.dict(os.environ, {
			"SWAP_BLOCK_START_HOUR": "22", "SWAP_BLOCK_START_MINUTE": "30",
			"SWAP_BLOCK_END_HOUR": "23", "SWAP_BLOCK_END_MINUTE": "30",
		}):
			self.assertTrue(strategy._allowed(datetime(2026, 11, 1, 12, tzinfo=timezone.utc)))
			self.assertFalse(strategy._allowed(datetime(2026, 11, 1, 20, 15, tzinfo=timezone.utc)))

	def test_realized_today_uses_prague_midnight_and_trade_deals_only(self):
		def deal(kind, profit=0, swap=0, commission=0, fee=0):
			return SimpleNamespace(type=kind, profit=profit, swap=swap, commission=commission, fee=fee)
		now = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
		with patch.object(strategy.mt5, "history_deals_get", return_value=[
			deal(mt5.DEAL_TYPE_BALANCE, profit=1000),
			deal(mt5.DEAL_TYPE_CREDIT, profit=100),
			deal(mt5.DEAL_TYPE_BUY, commission=-1),
			deal(mt5.DEAL_TYPE_SELL, profit=20, swap=-2, commission=-1, fee=-0.5),
		]) as history:
			self.assertEqual(strategy.realized_today(now), 15.5)
			self.assertEqual(history.call_args.args[0], datetime(2026, 6, 30, 22, tzinfo=timezone.utc))
		with patch.object(strategy.mt5, "history_deals_get", return_value=None):
			with self.assertRaises(RuntimeError):
				strategy.realized_today(now)

	def test_exit_schema_rejects_missing_unknown_duplicate_or_invalid_tickets(self):
		valid = {"ticket": 1, "action": "HOLD", "reasoning": "wait"}
		self.assertEqual(_validate_exit_decisions({"decisions": [valid]}, {1})["decisions"], [valid])
		for decisions in (
			[], [valid, valid], [dict(valid, ticket=2)], [dict(valid, ticket=True)],
			[dict(valid, action="BUY")], [dict(valid, action=["CLOSE"])], [dict(valid, reasoning="")],
		):
			with self.subTest(decisions=decisions), self.assertRaises(GeminiVertexRequestError):
				_validate_exit_decisions({"decisions": decisions}, {1})

	def test_separate_cost_postings_reduce_budget_and_corrections_block_it(self):
		def deal(kind, profit=0):
			return SimpleNamespace(type=kind, profit=profit, swap=0, commission=0, fee=0)
		deals = [
			deal(mt5.DEAL_TYPE_SELL, 20), deal(mt5.DEAL_TYPE_COMMISSION_DAILY, -2),
			deal(mt5.DEAL_TYPE_CHARGE, -1), deal(mt5.DEAL_TYPE_INTEREST, 10),
			deal(mt5.DEAL_TYPE_BALANCE, -1000),
		]
		with patch.object(strategy.mt5, "history_deals_get", return_value=deals):
			self.assertEqual(strategy.realized_today(datetime.now(timezone.utc)), 17)
			deals.append(deal(mt5.DEAL_TYPE_CORRECTION, -1))
			with self.assertRaises(RuntimeError):
				strategy.realized_today(datetime.now(timezone.utc))

	def test_exit_request_uses_dedicated_schema_and_validator(self):
		response = {"decisions": [{"ticket": 1, "action": "HOLD", "reasoning": "wait"}]}
		config = SimpleNamespace(model="gemini-4-argon")
		with patch("gemini_vertex._request_structured_json", side_effect=lambda **kwargs: kwargs["validator"](response)) as request:
			self.assertEqual(request_exit_decisions(config, "prompt", {1}), response)
			self.assertEqual(request.call_args.kwargs["task_name"], "daily_profit_exit")
			self.assertEqual(request.call_args.kwargs["max_output_tokens"], 8192)
			self.assertEqual(request.call_args.kwargs["response_schema"]["required"], ["decisions"])


if __name__ == "__main__":
	unittest.main()
