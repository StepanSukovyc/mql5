from __future__ import annotations

import unittest

import numpy as np

from market_data import (
    average_directional_index,
    average_true_range,
    bollinger_bands,
    exponential_moving_average,
    indicator_rows,
    volume_weighted_average_price,
)


class MarketDataIndicatorTests(unittest.TestCase):
    def test_exponential_moving_average_returns_seeded_series(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0]

        ema = exponential_moving_average(values, period=3)

        self.assertEqual(ema[:2], [None, None])
        self.assertAlmostEqual(ema[2], 2.0)
        self.assertAlmostEqual(ema[3], 3.0)
        self.assertAlmostEqual(ema[4], 4.0)

    def test_average_true_range_returns_positive_values_after_seed(self) -> None:
        highs = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        lows = [9.0, 10.0, 11.0, 12.0, 13.0, 14.0]
        closes = [9.5, 10.5, 11.5, 12.5, 13.5, 14.5]

        atr = average_true_range(highs, lows, closes, period=3)

        self.assertEqual(atr[:3], [None, None, None])
        self.assertTrue(all(value is not None and value > 0 for value in atr[3:]))

    def test_average_directional_index_returns_values_for_trending_series(self) -> None:
        highs = [float(10 + idx) for idx in range(40)]
        lows = [float(9 + idx) for idx in range(40)]
        closes = [float(9.5 + idx) for idx in range(40)]

        adx = average_directional_index(highs, lows, closes, period=14)

        self.assertTrue(any(value is not None for value in adx))
        last_value = next(value for value in reversed(adx) if value is not None)
        self.assertGreaterEqual(last_value, 0.0)

    def test_bollinger_bands_and_vwap_return_values(self) -> None:
        closes = [float(100 + idx) for idx in range(30)]
        highs = [value + 1.0 for value in closes]
        lows = [value - 1.0 for value in closes]
        volumes = [100.0 + idx for idx in range(30)]

        middle, upper, lower = bollinger_bands(closes, period=20)
        vwap = volume_weighted_average_price(highs, lows, closes, volumes)

        self.assertTrue(any(value is not None for value in middle))
        self.assertTrue(any(value is not None for value in upper))
        self.assertTrue(any(value is not None for value in lower))
        self.assertIsNotNone(vwap[-1])

    def test_indicator_rows_include_extended_indicators(self) -> None:
        rows = []
        for idx in range(260):
            rows.append(
                {
                    "time": 1_700_000_000 + (idx * 3600),
                    "open": 100.0 + idx,
                    "high": 101.0 + idx,
                    "low": 99.0 + idx,
                    "close": 100.5 + idx,
                    "tick_volume": 100,
                    "spread": 10,
                    "real_volume": 100,
                }
            )

        indicators = indicator_rows(rows, ma_period=20, rsi_period=14)

        self.assertIn("ema20", indicators)
        self.assertIn("ema50", indicators)
        self.assertIn("ema200", indicators)
        self.assertIn("atr14", indicators)
        self.assertIn("adx14", indicators)
        self.assertIn("rsi2", indicators)
        self.assertIn("bb_upper20", indicators)
        self.assertIn("bb_lower20", indicators)
        self.assertIn("vwap", indicators)
        self.assertTrue(indicators["ema20"])
        self.assertTrue(indicators["atr14"])
        self.assertTrue(indicators["bb_upper20"])
        self.assertTrue(indicators["vwap"])

    def test_indicator_rows_accept_mt5_structured_rows_without_dict_get(self) -> None:
        dtype = [
            ("time", "i8"),
            ("open", "f8"),
            ("high", "f8"),
            ("low", "f8"),
            ("close", "f8"),
            ("tick_volume", "i8"),
            ("spread", "i4"),
            ("real_volume", "i8"),
        ]
        rows = np.array(
            [
                (1_700_000_000 + (idx * 3600), 100.0 + idx, 101.0 + idx, 99.0 + idx, 100.5 + idx, 100 + idx, 10, 100)
                for idx in range(260)
            ],
            dtype=dtype,
        )

        indicators = indicator_rows(rows, ma_period=20, rsi_period=14)

        self.assertTrue(indicators["vwap"])
        self.assertTrue(indicators["ema200"])


if __name__ == "__main__":
    unittest.main()