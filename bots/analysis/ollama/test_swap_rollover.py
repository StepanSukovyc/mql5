"""Unit tests for broker-derived swap rollover detection."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from datetime import datetime, timezone

from swap_rollover import detect_swap_rollover_time_from_deals, get_swap_block_window


def _deal(*, hour: int, minute: int, reason: int = 0, comment: str = "", swap: float = 0.0):
	deal_time = datetime(2026, 4, 8, hour, minute, 0, tzinfo=timezone.utc)
	return SimpleNamespace(
		time=int(deal_time.timestamp()),
		time_msc=0,
		reason=reason,
		comment=comment,
		swap=swap,
		type=0,
	)


class SwapRolloverTests(unittest.TestCase):
	def test_detects_most_common_rollover_minute(self) -> None:
		deals = [
			_deal(hour=23, minute=0, comment="rollover"),
			_deal(hour=23, minute=0, comment="rollover"),
			_deal(hour=22, minute=59, comment="rollover"),
		]

		rollover_time = detect_swap_rollover_time_from_deals(deals)

		self.assertIsNotNone(rollover_time)
		assert rollover_time is not None
		self.assertEqual(rollover_time.hour, 23)
		self.assertEqual(rollover_time.minute, 0)
		self.assertEqual(rollover_time.sample_size, 2)

	def test_falls_back_to_comment_when_reason_is_missing(self) -> None:
		deals = [
			_deal(hour=21, minute=30, comment="daily swap posting"),
		]

		rollover_time = detect_swap_rollover_time_from_deals(deals)

		self.assertIsNotNone(rollover_time)
		assert rollover_time is not None
		self.assertEqual((rollover_time.hour, rollover_time.minute), (21, 30))

	def test_block_window_covers_thirty_minutes_before_and_after(self) -> None:
		now_utc = datetime(2026, 4, 8, 22, 45, tzinfo=timezone.utc)
		window = get_swap_block_window(
			now_utc=now_utc,
			rollover_time=detect_swap_rollover_time_from_deals([_deal(hour=23, minute=0, comment="rollover")]),
		)

		self.assertEqual(window.start_utc, datetime(2026, 4, 8, 22, 30, tzinfo=timezone.utc))
		self.assertEqual(window.end_utc, datetime(2026, 4, 8, 23, 30, tzinfo=timezone.utc))
		self.assertTrue(window.contains(now_utc))

	def test_block_window_handles_midnight_crossing(self) -> None:
		now_utc = datetime(2026, 4, 8, 23, 50, tzinfo=timezone.utc)
		window = get_swap_block_window(
			now_utc=now_utc,
			rollover_time=detect_swap_rollover_time_from_deals([_deal(hour=0, minute=10, comment="rollover")]),
		)

		self.assertEqual(window.start_utc, datetime(2026, 4, 8, 23, 40, tzinfo=timezone.utc))
		self.assertEqual(window.end_utc, datetime(2026, 4, 9, 0, 40, tzinfo=timezone.utc))
		self.assertTrue(window.contains(now_utc))


if __name__ == "__main__":
	unittest.main()