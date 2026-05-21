from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

from news_filter import reset_news_filter_cache, should_block_symbol_for_news


class NewsFilterTests(unittest.TestCase):
	def setUp(self) -> None:
		reset_news_filter_cache()

	@patch.dict(
		"os.environ",
		{
			"NEWS_FILTER_ENABLED": "true",
			"NEWS_FILTER_API_URL": "https://example.test/calendar?from={from_date}&to={to_date}",
			"NEWS_FILTER_LOOKBACK_MINUTES": "15",
			"NEWS_FILTER_LOOKAHEAD_MINUTES": "30",
			"NEWS_FILTER_IMPACTS": "high",
		},
		clear=False,
	)
	@patch("news_filter.httpx.get")
	def test_blocks_symbol_during_high_impact_news_window(self, mock_get) -> None:
		mock_response = Mock()
		mock_response.json.return_value = {
			"events": [
				{
					"timestamp": "2026-04-01T12:10:00Z",
					"currency": "USD",
					"impact": "high",
					"title": "CPI",
				}
			]
		}
		mock_response.raise_for_status.return_value = None
		mock_get.return_value = mock_response

		decision = should_block_symbol_for_news(
			"EURUSD_ecn",
			now_utc=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
		)

		self.assertTrue(decision.blocked)
		self.assertEqual(decision.reason, "high_impact_news_window")
		self.assertEqual(len(decision.relevant_events), 1)

	@patch.dict(
		"os.environ",
		{
			"NEWS_FILTER_ENABLED": "true",
			"NEWS_FILTER_API_URL": "https://example.test/calendar?from={from_date}&to={to_date}",
			"NEWS_FILTER_IMPACTS": "high",
		},
		clear=False,
	)
	@patch("news_filter.httpx.get")
	def test_does_not_block_when_currency_is_unrelated(self, mock_get) -> None:
		mock_response = Mock()
		mock_response.json.return_value = {
			"events": [
				{
					"timestamp": "2026-04-01T12:10:00Z",
					"currency": "JPY",
					"impact": "high",
					"title": "BOJ",
				}
			]
		}
		mock_response.raise_for_status.return_value = None
		mock_get.return_value = mock_response

		decision = should_block_symbol_for_news(
			"EURUSD_ecn",
			now_utc=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
		)

		self.assertFalse(decision.blocked)
		self.assertEqual(decision.reason, "clear")

	def test_returns_disabled_when_feature_is_off(self) -> None:
		with patch.dict("os.environ", {"NEWS_FILTER_ENABLED": "false"}, clear=False):
			decision = should_block_symbol_for_news(
				"EURUSD_ecn",
				now_utc=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
			)

		self.assertFalse(decision.blocked)
		self.assertEqual(decision.reason, "disabled")

	@patch.dict(
		"os.environ",
		{
			"NEWS_FILTER_ENABLED": "true",
			"NEWS_FILTER_API_URL": "https://example.test/calendar?from={from_date}&to={to_date}&apikey={token}",
			"NEWS_FILTER_API_TOKEN": "secret-token",
			"NEWS_FILTER_IMPACTS": "high",
		},
		clear=False,
	)
	@patch("news_filter.httpx.get")
	def test_injects_token_into_url_placeholder_without_header_auth(self, mock_get) -> None:
		mock_response = Mock()
		mock_response.json.return_value = []
		mock_response.raise_for_status.return_value = None
		mock_get.return_value = mock_response

		should_block_symbol_for_news(
			"EURUSD_ecn",
			now_utc=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
		)

		called_url = mock_get.call_args.args[0]
		called_headers = mock_get.call_args.kwargs.get("headers", {})
		self.assertIn("apikey=secret-token", called_url)
		self.assertEqual(called_headers, {})


if __name__ == "__main__":
	unittest.main()
