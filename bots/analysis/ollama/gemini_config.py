"""Shared Gemini configuration helpers."""

from __future__ import annotations

import os
from typing import Tuple


def load_gemini_api_config() -> Tuple[str, str]:
	"""Load Gemini API key and URL from environment."""
	api_key = os.getenv("GEMINI_API_KEY")
	api_url = os.getenv("GEMINI_URL")
	if not api_key:
		raise ValueError("GEMINI_API_KEY not found in environment")
	if not api_url:
		raise ValueError("GEMINI_URL not found in environment")
	return api_key, api_url