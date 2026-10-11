from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_advisory_state import get_active_rejection, record_rejection


class CandidateRejectionSummaryTests(unittest.TestCase):
	def test_reports_unique_candidates_first_reason_and_repeat_skips_separately(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			service_folder = Path(temp_dir)
			record_rejection(
				service_folder,
				strategy_id="parallel_mean_reversion",
				symbol="EURUSD_ecn",
				action="BUY",
				reason="signal_rejected:spread_above_limit",
				cooldown_minutes=30,
			)

			first = get_active_rejection(
				service_folder,
				strategy_id="parallel_mean_reversion",
				symbol="EURUSD_ecn",
				action="BUY",
			)
			second = get_active_rejection(
				service_folder,
				strategy_id="parallel_mean_reversion",
				symbol="EURUSD_ecn",
				action="BUY",
			)
			summary_path = service_folder / "trade_logs" / "candidate_rejection_summary.json"
			summary = json.loads(summary_path.read_text(encoding="utf-8"))
			state = json.loads(
				(service_folder / "trade_logs" / "gemini_advisory_state.json").read_text(encoding="utf-8")
			)

		self.assertIsNotNone(first)
		self.assertIsNotNone(second)
		self.assertEqual(summary["unique_candidate_count"], 1)
		candidate = summary["candidates"][0]
		self.assertEqual(candidate["first_rejection_reason"], "signal_rejected:spread_above_limit")
		self.assertEqual(candidate["repeated_cooldown_skips"], 2)
		self.assertEqual(state["rejections"][0]["repeat_skip_count"], 2)


if __name__ == "__main__":
	unittest.main()
