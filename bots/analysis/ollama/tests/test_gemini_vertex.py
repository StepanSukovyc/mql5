"""Unit tests for Gemini Vertex response parsing fallbacks."""

from __future__ import annotations

import unittest
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from gemini_config import GeminiVertexConfig
from gemini_vertex import (
	GeminiVertexRequestError,
	_get_legacy_prompt_with_schema,
	_normalize_legacy_gemini_api_url,
	_parse_structured_json_response,
	_should_skip_remaining_vertex_models,
	_should_try_rest_fallback,
	_get_vertex_rest_endpoint,
	_get_thinking_config,
	_get_model_output_token_limit,
	_request_structured_json_via_rest,
	request_prediction_json,
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
			trade_number=7,
		)

		self.assertIsNotNone(decision)
		self.assertEqual(mock_request_final_decision_json.call_count, 1)
		prompt = mock_request_final_decision_json.call_args.args[1]
		self.assertIn('"candidates": [', prompt)
		self.assertIn('"recommended_symbol": "SYMBOL_NAME"', prompt)
		self.assertIn("Pole reasoning musí být krátké", prompt)
		self.assertIn("Pořadí obchodu: #7", prompt)
		self.assertIn("lot_size a take_profit se ve finalni exekuci pocitaji lokalne.", prompt)
		self.assertNotIn("plně řízen Gemini", prompt)

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
		cases = {
			"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
			"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-001:generateContent": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
			"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent",
			"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite-001:generateContent": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent",
		}

		for legacy_url, expected_url in cases.items():
			with self.subTest(legacy_url=legacy_url):
				self.assertEqual(_normalize_legacy_gemini_api_url(legacy_url), expected_url)

	def test_legacy_api_url_keeps_supported_models_unchanged(self) -> None:
		api_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

		self.assertEqual(_normalize_legacy_gemini_api_url(api_url), api_url)


class GeminiArgonRequestTests(unittest.TestCase):
	def _config(self, model="gemini-4-argon", region="eu"):
		return GeminiVertexConfig(
			credentials_path="test-credentials.json",
			project="test-project",
			region=region,
			model=model,
			fallback_models=(model,),
		)

	def _prediction(self):
		return {"symbol": "EURUSD_ecn", "BUY": 60, "SELL": 20, "HOLD": 20, "reasoning": "test"}

	def test_vertex_endpoints_cover_multi_region_global_and_regional(self) -> None:
		for region, host in {
			"eu": "aiplatform.eu.rep.googleapis.com",
			"us": "aiplatform.us.rep.googleapis.com",
			"global": "aiplatform.googleapis.com",
			"europe-west1": "europe-west1-aiplatform.googleapis.com",
		}.items():
			with self.subTest(region=region):
				self.assertEqual(
					_get_vertex_rest_endpoint(self._config(region=region), "gemini-4-argon"),
					f"https://{host}/v1/projects/test-project/locations/{region}"
					"/publishers/google/models/gemini-4-argon:generateContent",
				)

	def test_thinking_and_output_limits_preserve_flash_behavior(self) -> None:
		self.assertEqual(
			_get_thinking_config("gemini-4-argon"),
			{"include_thoughts": False, "thinking_level": "MEDIUM"},
		)
		self.assertEqual(
			_get_thinking_config("gemini-2.5-flash"),
			{"include_thoughts": False, "thinking_budget": 0},
		)
		for limit in (512, 768):
			self.assertEqual(_get_model_output_token_limit("gemini-4-argon", limit), 8192)
			self.assertEqual(_get_model_output_token_limit("gemini-2.5-flash", limit), limit)
		self.assertEqual(_get_model_output_token_limit("gemini-4-argon", 16384), 16384)

	@patch("gemini_vertex._log_event")
	@patch("gemini_vertex._load_legacy_gemini_api_config", return_value=None)
	@patch("google.genai.Client")
	@patch.dict(os.environ, {}, clear=False)
	def test_sdk_argon_request_uses_medium_without_budget(self, mock_client, _legacy, mock_log) -> None:
		client = mock_client.return_value.__enter__.return_value
		client.models.generate_content.return_value = SimpleNamespace(
			parsed=self._prediction(), text=json.dumps(self._prediction()),
		)

		result = json.loads(request_prediction_json(self._config(), "EURUSD_ecn", "synthetic test"))

		self.assertEqual(result, self._prediction())
		self.assertEqual(mock_client.call_args.kwargs["location"], "eu")
		self.assertTrue(mock_client.call_args.kwargs["vertexai"])
		request = client.models.generate_content.call_args.kwargs
		self.assertEqual(request["model"], "gemini-4-argon")
		self.assertEqual(request["config"].max_output_tokens, 8192)
		self.assertEqual(request["config"].thinking_config.thinking_level, "MEDIUM")
		self.assertIsNone(request["config"].thinking_config.thinking_budget)
		self.assertEqual(request["config"].response_mime_type, "application/json")
		self.assertEqual(mock_log.call_args.kwargs["transport"], "google-genai-sdk")
		self.assertEqual(mock_log.call_args.kwargs["model"], "gemini-4-argon")

	@patch("gemini_vertex._log_event")
	@patch("gemini_vertex._load_legacy_gemini_api_config", return_value=None)
	@patch("google.genai.Client")
	@patch.dict(os.environ, {}, clear=False)
	def test_sdk_flash_request_retains_disabled_thinking_and_small_limit(self, mock_client, _legacy, _log) -> None:
		client = mock_client.return_value.__enter__.return_value
		client.models.generate_content.return_value = SimpleNamespace(
			parsed=self._prediction(), text=json.dumps(self._prediction()),
		)
		request_prediction_json(self._config(model="gemini-2.5-flash", region="europe-west1"), "EURUSD_ecn", "test")
		request_config = client.models.generate_content.call_args.kwargs["config"]
		self.assertEqual(request_config.max_output_tokens, 512)
		self.assertEqual(request_config.thinking_config.thinking_budget, 0)
		self.assertIsNone(request_config.thinking_config.thinking_level)

	@patch("gemini_vertex._get_service_account_access_token", return_value="test-token")
	@patch("gemini_vertex.urllib.request.urlopen")
	def test_rest_argon_request_uses_eu_endpoint_and_medium(self, mock_urlopen, _token) -> None:
		mock_urlopen.return_value.__enter__.return_value.read.return_value = b'{"candidates":[]}'
		result = _request_structured_json_via_rest(
			config=self._config(), model_name="gemini-4-argon", prompt="synthetic test",
			response_schema={"type": "OBJECT"}, max_output_tokens=512,
		)
		request = mock_urlopen.call_args.args[0]
		self.assertTrue(request.full_url.startswith("https://aiplatform.eu.rep.googleapis.com/v1/"))
		generation = json.loads(request.data)["generationConfig"]
		self.assertEqual(generation["thinkingConfig"], {"includeThoughts": False, "thinkingLevel": "MEDIUM"})
		self.assertEqual(generation["maxOutputTokens"], 8192)
		self.assertEqual(result, {"candidates": []})

	@patch("gemini_vertex._get_service_account_access_token", return_value="test-token")
	@patch("gemini_vertex.urllib.request.urlopen")
	def test_rest_flash_request_preserves_existing_payload(self, mock_urlopen, _token) -> None:
		mock_urlopen.return_value.__enter__.return_value.read.return_value = b'{"candidates":[]}'
		_request_structured_json_via_rest(
			config=self._config(model="gemini-2.5-flash", region="europe-west1"),
			model_name="gemini-2.5-flash", prompt="test",
			response_schema={"type": "OBJECT"}, max_output_tokens=512,
		)
		request = mock_urlopen.call_args.args[0]
		self.assertTrue(request.full_url.startswith("https://europe-west1-aiplatform.googleapis.com/"))
		generation = json.loads(request.data)["generationConfig"]
		self.assertEqual(generation["maxOutputTokens"], 512)
		self.assertNotIn("thinkingConfig", generation)

	@patch("gemini_vertex._log_event")
	@patch("gemini_vertex._request_structured_json_via_rest")
	@patch("gemini_vertex._load_legacy_gemini_api_config", return_value=None)
	@patch("google.genai.Client")
	@patch.dict(os.environ, {}, clear=False)
	def test_argon_sdk_parse_error_can_use_same_model_vertex_rest(
		self, mock_client, _legacy, mock_rest, mock_log,
	) -> None:
		client = mock_client.return_value.__enter__.return_value
		client.models.generate_content.side_effect = GeminiVertexRequestError("Gemini returned invalid JSON: test")
		mock_rest.return_value = {"candidates": [{"content": {"parts": [
			{"text": json.dumps(self._prediction())},
		]}}]}
		result = json.loads(request_prediction_json(self._config(), "EURUSD_ecn", "synthetic test"))
		self.assertEqual(result, self._prediction())
		self.assertEqual(mock_rest.call_args.kwargs["model_name"], "gemini-4-argon")
		self.assertEqual(mock_rest.call_args.kwargs["max_output_tokens"], 8192)
		self.assertEqual(mock_log.call_args.kwargs["transport"], "vertex-rest-fallback")

	@patch("gemini_vertex._log_event")
	@patch("gemini_vertex._request_structured_json_via_legacy_api")
	@patch("gemini_vertex._load_legacy_gemini_api_config", return_value={
		"api_key": "test-key",
		"api_url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
	})
	@patch("google.genai.Client")
	@patch.dict(os.environ, {}, clear=False)
	def test_argon_error_preserves_explicit_flash_api_key_fallback(
		self, mock_client, _config, mock_legacy, mock_log,
	) -> None:
		client = mock_client.return_value.__enter__.return_value
		client.models.generate_content.side_effect = GeminiVertexRequestError("test Vertex failure")
		mock_legacy.return_value = {"candidates": [{"content": {"parts": [
			{"text": json.dumps(self._prediction())},
		]}}]}
		result = json.loads(request_prediction_json(self._config(), "EURUSD_ecn", "synthetic test"))
		self.assertEqual(result, self._prediction())
		client.models.generate_content.assert_called_once()
		mock_legacy.assert_called_once()
		self.assertIn("gemini-2.5-flash:generateContent", mock_legacy.call_args.kwargs["api_url"])
		self.assertEqual(mock_log.call_args.kwargs["transport"], "legacy-gemini-api")


if __name__ == "__main__":
	unittest.main()