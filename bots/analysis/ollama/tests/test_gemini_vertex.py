"""Unit tests for Gemini Vertex response parsing fallbacks."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gemini_vertex import (
	GeminiVertexRequestError,
	_get_legacy_prompt_with_schema,
	_normalize_legacy_gemini_api_url,
	_parse_structured_json_response,
	_should_skip_remaining_vertex_models,
	_should_try_rest_fallback,
)


class GeminiVertexParsingTests(unittest.TestCase):
	@patch("gemini_decision.request_final_decision_json")
	def test_final_decision_prompt_construction_accepts_ranked_candidates_example(self, mock_request_final_decision_json) -> None:
		from gemini_decision import ask_gemini_final_decision

		mock_request_final_decision_json.return_value = (
			'{"recommended_symbol":"EURUSD_ecn","action":"BUY","reasoning":"ok"}'
		)

		decision = ask_gemini_final_decision(
			predictions=[{"symbol": "EURUSD_ecn", "BUY": 60, "SELL": 20}],
			open_positions=[],
			account_state={"balance": 5000},
			gemini_config=SimpleNamespace(),
		)

		self.assertIsNotNone(decision)
		self.assertEqual(mock_request_final_decision_json.call_count, 1)
		prompt = mock_request_final_decision_json.call_args.args[1]
		self.assertIn('"candidates": [', prompt)
		self.assertIn('"recommended_symbol": "SYMBOL_NAME"', prompt)

	def test_parses_json_wrapped_in_code_fence(self) -> None:
		response = SimpleNamespace(
			parsed=None,
			text="```json\n{\n  \"recommended_symbol\": \"EURUSD_ecn\",\n  \"action\": \"BUY\",\n  \"reasoning\": \"ok\"\n}\n```",
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
									"\"reasoning\":\"ok\"}\n"
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
		self.assertEqual(parsed["reasoning"], "ok")

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

	def test_final_decision_validation_accepts_advisory_only_fields(self) -> None:
		from gemini_vertex import _validate_final_decision_payload

		payload = _validate_final_decision_payload(
			{
				"recommended_symbol": "EURUSD_ecn",
				"action": "BUY",
				"reasoning": "strong setup",
			}
		)

		self.assertEqual(payload["recommended_symbol"], "EURUSD_ecn")
		self.assertEqual(payload["action"], "BUY")
		self.assertEqual(payload["reasoning"], "strong setup")

	def test_final_decision_validation_normalizes_ranked_candidates(self) -> None:
		from gemini_vertex import _validate_final_decision_payload

		payload = _validate_final_decision_payload(
			{
				"recommended_symbol": "EURUSD_ecn",
				"action": "BUY",
				"reasoning": "strong setup",
				"candidates": [
					{"symbol": "EURUSD_ecn", "action": "BUY", "reasoning": "top"},
					{"symbol": "USDJPY_ecn", "action": "SELL", "reasoning": "alt"},
					{"symbol": "USDJPY_ecn", "action": "SELL", "reasoning": "duplicate"},
				],
			}
		)

		self.assertEqual(payload["recommended_symbol"], "EURUSD_ecn")
		self.assertEqual(len(payload["candidates"]), 2)
		self.assertEqual(payload["candidates"][1]["symbol"], "USDJPY_ecn")

	def test_model_not_found_404_skips_remaining_vertex_models(self) -> None:
		self.assertTrue(
			_should_skip_remaining_vertex_models(
				404,
				"Publisher Model `projects/test/locations/europe-west1/publishers/google/models/gemini-2.0-flash` was not found or your project does not have access to it.",
			)
		)

	def test_legacy_api_url_rewrites_removed_flash_model_to_supported_endpoint(self) -> None:
		self.assertEqual(
			_normalize_legacy_gemini_api_url(
				"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
			),
			"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
		)


if __name__ == "__main__":
	unittest.main()