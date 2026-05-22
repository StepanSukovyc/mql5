"""Unit tests for Gemini Vertex response parsing fallbacks."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from gemini_vertex import (
	GeminiVertexRequestError,
	_get_legacy_prompt_with_schema,
	_parse_structured_json_response,
	_should_skip_remaining_vertex_models,
	_should_try_rest_fallback,
)


class GeminiVertexParsingTests(unittest.TestCase):
	def test_parses_json_wrapped_in_code_fence(self) -> None:
		response = SimpleNamespace(
			parsed=None,
			text="```json\n{\n  \"recommended_symbol\": \"EURUSD_ecn\",\n  \"action\": \"BUY\",\n  \"lot_size\": 0.01,\n  \"take_profit\": 1.1,\n  \"reasoning\": \"ok\"\n}\n```",
		)

		parsed = _parse_structured_json_response(response)

		self.assertEqual(parsed["recommended_symbol"], "EURUSD_ecn")
		self.assertEqual(parsed["action"], "BUY")

	def test_parses_json_from_candidate_parts_with_surrounding_text(self) -> None:
		response = SimpleNamespace(
			parsed=None,
			text=None,
			candidates=[
				SimpleNamespace(
					content=SimpleNamespace(
						parts=[
							SimpleNamespace(
								text=(
									"Tady je výsledek:\n"
									"{\"recommended_symbol\":\"EURUSD_ecn\",\"action\":\"BUY\","
									"\"lot_size\":0.01,\"take_profit\":1.1,\"reasoning\":\"ok\"}\n"
									"Děkuji."
								)
							)
						]
					)
				)
			],
		)

		parsed = _parse_structured_json_response(response)

		self.assertEqual(parsed["recommended_symbol"], "EURUSD_ecn")
		self.assertEqual(parsed["take_profit"], 1.1)

	def test_rest_fallback_is_enabled_for_invalid_json_sdk_response(self) -> None:
		exc = GeminiVertexRequestError(
			'Gemini returned invalid JSON: Unterminated string starting at at line 1 column 48. Raw response snippet: {"recommended_symbol": "USDNOK_ecn", "action": "'
		)

		self.assertTrue(_should_try_rest_fallback(exc, None))

	def test_rest_fallback_stays_disabled_for_http_status_errors(self) -> None:
		exc = GeminiVertexRequestError("Gemini API returned 400", status_code=400)

		self.assertFalse(_should_try_rest_fallback(exc, 400))

	def test_legacy_prompt_appends_required_schema_fields(self) -> None:
		prompt = _get_legacy_prompt_with_schema(
			"Vrat pouze JSON.",
			{
				"type": "OBJECT",
				"required": ["symbol", "BUY", "SELL", "HOLD", "reasoning"],
				"properties": {
					"symbol": {"type": "STRING"},
					"BUY": {"type": "NUMBER"},
					"SELL": {"type": "NUMBER"},
					"HOLD": {"type": "NUMBER"},
					"reasoning": {"type": "STRING"},
				},
			},
		)

		self.assertIn('"symbol": "text"', prompt)
		self.assertIn('"BUY": 0', prompt)
		self.assertIn('"reasoning": "text"', prompt)

	def test_model_not_found_404_skips_remaining_vertex_models(self) -> None:
		self.assertTrue(
			_should_skip_remaining_vertex_models(
				404,
				"Publisher Model `projects/test/locations/europe-west1/publishers/google/models/gemini-2.0-flash` was not found or your project does not have access to it.",
			)
		)


if __name__ == "__main__":
	unittest.main()