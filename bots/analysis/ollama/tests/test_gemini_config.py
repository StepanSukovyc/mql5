"""Unit tests for Gemini Vertex model fallback chains."""

from __future__ import annotations

import unittest

from gemini_config import _build_model_fallbacks


class GeminiConfigFallbackTests(unittest.TestCase):
	def test_flash_model_includes_20_series_fallbacks(self) -> None:
		self.assertEqual(
			_build_model_fallbacks("gemini-2.5-flash"),
			("gemini-2.5-flash", "gemini-2.0-flash-001", "gemini-2.0-flash"),
		)

	def test_flash_lite_model_includes_20_series_fallbacks(self) -> None:
		self.assertEqual(
			_build_model_fallbacks("gemini-2.5-flash-lite"),
			("gemini-2.5-flash-lite", "gemini-2.0-flash-lite-001", "gemini-2.0-flash-lite"),
		)


if __name__ == "__main__":
	unittest.main()