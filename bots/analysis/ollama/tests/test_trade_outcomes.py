from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import MetaTrader5 as mt5

from trade_outcomes import process_closed_trade_outcomes, register_open_trade


class ClosedTradeOutcomeTests(unittest.TestCase):
	def test_records_net_result_duration_and_r_multiple_once(self) -> None:
		entry_time = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
		exit_time = entry_time + timedelta(hours=2, minutes=30)
		deals = (
			SimpleNamespace(
				position_id=501,
				entry=getattr(mt5, "DEAL_ENTRY_IN", 0),
				time=int(entry_time.timestamp()),
				time_msc=int(entry_time.timestamp() * 1000),
				volume=0.2,
				profit=0.0,
				commission=-0.5,
				swap=0.0,
				fee=-0.1,
				reason=0,
			),
			SimpleNamespace(
				position_id=501,
				entry=getattr(mt5, "DEAL_ENTRY_OUT", 1),
				time=int(exit_time.timestamp()),
				time_msc=int(exit_time.timestamp() * 1000),
				volume=0.2,
				profit=12.0,
				commission=-0.5,
				swap=1.0,
				fee=0.0,
				reason=4,
			),
		)
		with tempfile.TemporaryDirectory() as temp_dir:
			service_folder = Path(temp_dir)
			register_open_trade(
				service_folder,
				position_id=501,
				strategy_id="quant_math",
				symbol="EURUSD_ecn",
				action="BUY",
				entry_time_utc=entry_time,
				entry_volume=0.2,
				entry_price=1.1,
				initial_risk_usd=20.0,
			)
			with patch("trade_outcomes.mt5.positions_get", return_value=()), patch(
				"trade_outcomes.mt5.history_deals_get", return_value=deals
			):
				recorded = process_closed_trade_outcomes(service_folder, now_utc=exit_time + timedelta(minutes=1))
				repeated = process_closed_trade_outcomes(service_folder, now_utc=exit_time + timedelta(minutes=2))

			outcome_path = service_folder / "trade_logs" / "closed_trade_outcomes.csv"
			with open(outcome_path, "r", newline="", encoding="utf-8") as handle:
				rows = list(csv.DictReader(handle))

		self.assertEqual(recorded, 1)
		self.assertEqual(repeated, 0)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["strategy_id"], "quant_math")
		self.assertAlmostEqual(float(rows[0]["net_profit"]), 11.9)
		self.assertAlmostEqual(float(rows[0]["holding_seconds"]), 9000.0)
		self.assertAlmostEqual(float(rows[0]["r_multiple"]), 0.595)
		self.assertEqual(rows[0]["exit_deal_count"], "1")

	def test_seeds_existing_positions_without_inventing_risk_multiple(self) -> None:
		entry_time = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
		exit_time = entry_time + timedelta(minutes=15)
		position = SimpleNamespace(
			identifier=700,
			ticket=700,
			type=getattr(mt5, "POSITION_TYPE_BUY", 0),
			time=int(entry_time.timestamp()),
			volume=0.1,
			price_open=1.2,
			symbol="EURUSD_ecn",
			magic=234400,
			comment="ga:quant_math",
		)
		deals = (
			SimpleNamespace(position_id=700, entry=getattr(mt5, "DEAL_ENTRY_IN", 0), time=int(entry_time.timestamp())),
			SimpleNamespace(
				position_id=700,
				entry=getattr(mt5, "DEAL_ENTRY_OUT", 1),
				time=int(exit_time.timestamp()),
				time_msc=int(exit_time.timestamp() * 1000),
				volume=0.1,
				profit=-3.0,
				commission=-0.2,
				swap=-0.1,
				fee=0.0,
				reason=1,
			),
		)
		with tempfile.TemporaryDirectory() as temp_dir:
			service_folder = Path(temp_dir)
			with patch("trade_outcomes.mt5.positions_get", return_value=(position,)), patch(
				"trade_outcomes.mt5.history_deals_get", return_value=deals
			):
				self.assertEqual(
					process_closed_trade_outcomes(service_folder, now_utc=entry_time + timedelta(minutes=1)),
					0,
				)
			with patch("trade_outcomes.mt5.positions_get", return_value=()), patch(
				"trade_outcomes.mt5.history_deals_get", return_value=deals
			):
				self.assertEqual(process_closed_trade_outcomes(service_folder, now_utc=exit_time), 1)

			outcome_path = service_folder / "trade_logs" / "closed_trade_outcomes.csv"
			with open(outcome_path, "r", newline="", encoding="utf-8") as handle:
				row = next(csv.DictReader(handle))

		self.assertEqual(row["strategy_id"], "quant_math")
		self.assertAlmostEqual(float(row["net_profit"]), -3.3)
		self.assertEqual(row["initial_risk_usd"], "")
		self.assertEqual(row["r_multiple"], "")


if __name__ == "__main__":
	unittest.main()
