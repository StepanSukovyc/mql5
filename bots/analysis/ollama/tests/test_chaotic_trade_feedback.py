"""Tests for the chaotic strategy's append-only feedback journal."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from chaotic_trade_feedback import (
	build_chaotic_recent_feedback,
	reconcile_chaotic_trade_outcomes,
	record_chaotic_decision,
	record_chaotic_position_opened,
)


class ChaoticTradeFeedbackTests(unittest.TestCase):
	def test_feedback_contains_only_latest_closed_trades(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			service_folder = Path(temp_dir)
			first_id = record_chaotic_decision(
				service_folder,
				symbol="EURUSD_ecn",
				action="BUY",
				candidate_source="chaotic_ollama",
				decision_payload={"reasoning": "first"},
				prediction_snapshot={"buy": 44, "sell": 28},
				account_state={},
			)
			record_chaotic_position_opened(
				service_folder,
				decision_id=first_id,
				position_ticket=101,
				order_ticket=101,
				deal_ticket=201,
				filled_price=1.08,
				filled_volume=0.01,
			)
			second_id = record_chaotic_decision(
				service_folder,
				symbol="GBPUSD_ecn",
				action="SELL",
				candidate_source="chaotic_ollama",
				decision_payload={"reasoning": "second"},
				prediction_snapshot={"buy": 20, "sell": 48},
				account_state={},
			)
			record_chaotic_position_opened(
				service_folder,
				decision_id=second_id,
				position_ticket=102,
				order_ticket=102,
				deal_ticket=202,
				filled_price=1.27,
				filled_volume=0.01,
			)

			deals = [
				SimpleNamespace(position_id=101, entry=1, time=1_785_000_000, profit=-2.0, swap=-0.1, commission=-0.1, price=1.07, reason=0),
				SimpleNamespace(position_id=102, entry=1, time=1_785_000_100, profit=3.0, swap=0.0, commission=-0.1, price=1.26, reason=0),
			]
			created = reconcile_chaotic_trade_outcomes(
				service_folder,
				open_positions=[],
				history_deals_get=lambda _start, _end: deals,
				now_utc=datetime(2026, 8, 5, tzinfo=timezone.utc),
			)
			feedback = build_chaotic_recent_feedback(service_folder, limit=1)

		self.assertEqual(created, 2)
		self.assertEqual(feedback["window"]["closed_trades"], 1)
		self.assertEqual(feedback["window"]["wins"], 1)
		self.assertEqual(feedback["recent_trades"][0]["symbol"], "GBPUSD_ecn")
		self.assertEqual(feedback["recent_trades"][0]["net_profit"], 2.9)


if __name__ == "__main__":
	unittest.main()