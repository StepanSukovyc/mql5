"""Unit tests for broker-derived swap rollover detection."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import patch

from swap_rollover import detect_swap_rollover_time_from_deals, get_swap_block_window


def _deal(*, hour: int, minute: int, reason: int = 0, comment: str = "", swap: float = 0.0, deal_type: int = 0, entry: int = 0):
	deal_time = datetime(2026, 4, 8, hour, minute, 0, tzinfo=timezone.utc)
	return SimpleNamespace(
		time=int(deal_time.timestamp()),
		time_msc=0,
		reason=reason,
		comment=comment,
		swap=swap,
		type=deal_type,
		entry=entry,
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

	def test_detects_rollover_from_closing_deal_with_swap(self) -> None:
		deals = [
			_deal(hour=22, minute=45, swap=-0.97, entry=1),
			_deal(hour=22, minute=45, swap=-0.93, entry=1),
			_deal(hour=21, minute=45, swap=-0.17, entry=1),
		]

		rollover_time = detect_swap_rollover_time_from_deals(deals)

		self.assertIsNotNone(rollover_time)
		assert rollover_time is not None
		self.assertEqual((rollover_time.hour, rollover_time.minute), (22, 45))
		self.assertEqual(rollover_time.source, "mt5_rollover_history")

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

	@patch.dict(
		os.environ,
		{
			"SWAP_BLOCK_START_HOUR": "22",
			"SWAP_BLOCK_START_MINUTE": "30",
			"SWAP_BLOCK_END_HOUR": "23",
			"SWAP_BLOCK_END_MINUTE": "30",
		},
		clear=False,
	)
	@patch("swap_rollover.detect_swap_rollover_time")
	def test_manual_env_window_is_used_when_broker_detection_falls_back(self, mock_detect_swap_rollover_time) -> None:
		from swap_rollover import SwapRolloverTime

		mock_detect_swap_rollover_time.return_value = SwapRolloverTime(
			hour=23,
			minute=0,
			source="default_23_00",
			sample_size=0,
			observed_at_utc=None,
		)

		now_utc = datetime(2026, 4, 8, 22, 45, tzinfo=timezone.utc)
		window = get_swap_block_window(now_utc=now_utc)

		self.assertEqual(window.start_utc, datetime(2026, 4, 8, 22, 30, tzinfo=timezone.utc))
		self.assertEqual(window.end_utc, datetime(2026, 4, 8, 23, 30, tzinfo=timezone.utc))
		self.assertEqual(window.rollover_time.source, "env_manual_window")
		self.assertTrue(window.contains(now_utc))

	@patch.dict(
		os.environ,
		{
			"SWAP_BLOCK_START_HOUR": "22",
			"SWAP_BLOCK_START_MINUTE": "30",
			"SWAP_BLOCK_END_HOUR": "23",
			"SWAP_BLOCK_END_MINUTE": "30",
		},
		clear=False,
	)
	@patch("swap_rollover.detect_swap_rollover_time")
	def test_manual_env_window_is_used_even_when_broker_detection_finds_rollover(self, mock_detect_swap_rollover_time) -> None:
		from swap_rollover import SwapRolloverTime

		mock_detect_swap_rollover_time.return_value = SwapRolloverTime(
			hour=0,
			minute=53,
			source="mt5_rollover_history",
			sample_size=3,
			observed_at_utc=datetime(2026, 4, 9, 0, 53, tzinfo=timezone.utc),
		)

		now_utc = datetime(2026, 4, 8, 22, 45, tzinfo=timezone.utc)
		window = get_swap_block_window(now_utc=now_utc)

		self.assertEqual(window.start_utc, datetime(2026, 4, 8, 22, 30, tzinfo=timezone.utc))
		self.assertEqual(window.end_utc, datetime(2026, 4, 8, 23, 30, tzinfo=timezone.utc))
		self.assertEqual(window.rollover_time.source, "env_manual_window")
		self.assertTrue(window.contains(now_utc))


if __name__ == "__main__":
	unittest.main()