from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trading_logic import _get_or_create_current_hour_run_folder


class TradingLogicTests(unittest.TestCase):
	def test_reuses_latest_current_hour_run_folder(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			source_folder = Path(temp_dir)
			(source_folder / "20260521_175303").mkdir()
			(source_folder / "20260521_175546").mkdir()
			(source_folder / "20260521_165000").mkdir()

			with patch("trading_logic.datetime") as mock_datetime:
				mock_now = __import__("datetime").datetime(2026, 5, 21, 17, 59, 0, tzinfo=__import__("datetime").timezone.utc)
				mock_datetime.now.return_value = mock_now
				mock_datetime.side_effect = lambda *args, **kwargs: __import__("datetime").datetime(*args, **kwargs)

				timestamp, run_folder = _get_or_create_current_hour_run_folder(source_folder)

			self.assertEqual(timestamp, "20260521_175546")
			self.assertEqual(run_folder, source_folder / "20260521_175546")

	def test_creates_new_run_folder_name_when_current_hour_has_no_run(self) -> None:
		with tempfile.TemporaryDirectory() as temp_dir:
			source_folder = Path(temp_dir)

			with patch("trading_logic.datetime") as mock_datetime:
				mock_now = __import__("datetime").datetime(2026, 5, 21, 18, 1, 2, tzinfo=__import__("datetime").timezone.utc)
				mock_datetime.now.return_value = mock_now
				mock_datetime.side_effect = lambda *args, **kwargs: __import__("datetime").datetime(*args, **kwargs)

				timestamp, run_folder = _get_or_create_current_hour_run_folder(source_folder)

			self.assertEqual(timestamp, "20260521_180102")
			self.assertEqual(run_folder, source_folder / "20260521_180102")


if __name__ == "__main__":
	unittest.main()
