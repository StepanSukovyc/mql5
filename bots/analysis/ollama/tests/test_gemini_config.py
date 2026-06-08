"""Unit tests for Gemini Vertex model fallback chains."""

from __future__ import annotations

import unittest

from gemini_config import _build_model_fallbacks


class GeminiConfigFallbackTests(unittest.TestCase):
	def test_flash_model_stays_on_supported_series(self) -> None:
		self.assertEqual(
			_build_model_fallbacks("gemini-2.5-flash"),
			("gemini-2.5-flash",),
		)

	def test_flash_lite_model_stays_on_supported_series(self) -> None:
		self.assertEqual(
			_build_model_fallbacks("gemini-2.5-flash-lite"),
			("gemini-2.5-flash-lite",),
		)

	def test_20_series_flash_upgrades_to_supported_25_fallback(self) -> None:
		self.assertEqual(
			_build_model_fallbacks("gemini-2.0-flash"),
			("gemini-2.0-flash", "gemini-2.5-flash"),
		)

	def test_20_series_flash_lite_upgrades_to_supported_25_fallback(self) -> None:
		self.assertEqual(
			_build_model_fallbacks("gemini-2.0-flash-lite"),
			("gemini-2.0-flash-lite", "gemini-2.5-flash-lite"),
		)


if __name__ == "__main__":
	unittest.main()