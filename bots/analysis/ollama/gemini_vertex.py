"""Shared Gemini Vertex AI request helpers with structured JSON validation."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, Optional

from gemini_config import GeminiVertexConfig


_DEFAULT_MAX_ATTEMPTS_PER_MODEL = 2
_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_BACKOFF_SECONDS = 2.0
_PREDICTION_MAX_OUTPUT_TOKENS = 512
_FINAL_DECISION_MAX_OUTPUT_TOKENS = 768

_PREDICTION_RESPONSE_SCHEMA = {
	"type": "OBJECT",
	"required": ["symbol", "BUY", "SELL", "HOLD", "reasoning"],
	"properties": {
		"symbol": {"type": "STRING"},
		"BUY": {"type": "NUMBER"},
		"SELL": {"type": "NUMBER"},
		"HOLD": {"type": "NUMBER"},
		"reasoning": {"type": "STRING"},
	},
}

_FINAL_DECISION_RESPONSE_SCHEMA = {
	"type": "OBJECT",
	"required": ["recommended_symbol", "action", "lot_size", "take_profit", "reasoning"],
	"properties": {
		"recommended_symbol": {"type": "STRING"},
		"action": {"type": "STRING", "enum": ["BUY", "SELL"]},
		"lot_size": {"type": "NUMBER"},
		"take_profit": {"type": "NUMBER"},
		"reasoning": {"type": "STRING"},
	},
}


class GeminiVertexError(RuntimeError):
	"""Base error raised by the shared Gemini Vertex AI helper."""


class GeminiVertexRequestError(GeminiVertexError):
	"""Request failure with optional HTTP-like status code."""

	def __init__(self, message: str, status_code: Optional[int] = None) -> None:
		super().__init__(message)
		self.status_code = status_code


def _import_google_genai():
	try:
		from google import genai
		from google.genai import types
		return genai, types
	except ImportError as exc:
		raise GeminiVertexError(
			"Missing dependency 'google-genai'. Install it with 'pip install -r requirements.txt'."
		) from exc


def _log_event(event: str, **payload: Any) -> None:
	message = {"event": event, **payload}
	print(f"  🧠 Gemini Vertex: {json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)}")


def _extract_response_text(response: Any) -> str:
	text = getattr(response, "text", None)
	if isinstance(text, str):
		return text.strip()
	return ""


def _extract_text_snippet(text: str, limit: int = 200) -> str:
	cleaned = " ".join(text.split())
	if len(cleaned) <= limit:
		return cleaned
	return cleaned[: limit - 3] + "..."


def _extract_finish_reason(response: Any) -> str:
	candidates = getattr(response, "candidates", None) or []
	if not candidates:
		return ""
	finish_reason = getattr(candidates[0], "finish_reason", "")
	return str(finish_reason or "")


def _extract_usage_value(usage_metadata: Any, name: str) -> Optional[int]:
	value = getattr(usage_metadata, name, None)
	if value is None:
		return None
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def _parse_structured_json_response(response: Any) -> Dict[str, Any]:
	parsed = getattr(response, "parsed", None)
	if isinstance(parsed, dict):
		return parsed

	response_text = _extract_response_text(response)
	if not response_text:
		raise GeminiVertexRequestError("Gemini returned an empty structured response")

	try:
		decoded = json.loads(response_text)
	except json.JSONDecodeError as exc:
		raise GeminiVertexRequestError(
			f"Gemini returned invalid JSON: {exc.msg}",
		) from exc

	if not isinstance(decoded, dict):
		raise GeminiVertexRequestError("Gemini returned JSON that is not an object")

	return decoded


def _ensure_float(value: Any, field_name: str) -> float:
	try:
		return float(value)
	except (TypeError, ValueError) as exc:
		raise GeminiVertexRequestError(f"Field '{field_name}' is missing or not numeric") from exc


def _validate_prediction_payload(expected_symbol: str, payload: Dict[str, Any]) -> Dict[str, Any]:
	symbol = payload.get("symbol")
	reasoning = payload.get("reasoning")
	if symbol != expected_symbol:
		raise GeminiVertexRequestError(
			f"Prediction symbol mismatch: expected {expected_symbol}, got {symbol}"
		)
	if not isinstance(reasoning, str) or not reasoning.strip():
		raise GeminiVertexRequestError("Prediction reasoning is missing or empty")

	buy = _ensure_float(payload.get("BUY"), "BUY")
	sell = _ensure_float(payload.get("SELL"), "SELL")
	hold = _ensure_float(payload.get("HOLD"), "HOLD")

	for field_name, value in (("BUY", buy), ("SELL", sell), ("HOLD", hold)):
		if value < 0 or value > 100:
			raise GeminiVertexRequestError(f"Prediction field '{field_name}' must be between 0 and 100")

	total = buy + sell + hold
	if abs(total - 100.0) > 1.0:
		raise GeminiVertexRequestError(
			f"Prediction probabilities must sum to 100 (+/- 1), got {total:.2f}"
		)

	return {
		"symbol": symbol,
		"BUY": buy,
		"SELL": sell,
		"HOLD": hold,
		"reasoning": reasoning.strip(),
	}


def _validate_final_decision_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
	recommended_symbol = payload.get("recommended_symbol")
	action = payload.get("action")
	reasoning = payload.get("reasoning")
	if not isinstance(recommended_symbol, str) or not recommended_symbol.strip():
		raise GeminiVertexRequestError("Field 'recommended_symbol' is missing or empty")
	if action not in {"BUY", "SELL"}:
		raise GeminiVertexRequestError("Field 'action' must be BUY or SELL")
	if not isinstance(reasoning, str) or not reasoning.strip():
		raise GeminiVertexRequestError("Field 'reasoning' is missing or empty")

	lot_size = _ensure_float(payload.get("lot_size"), "lot_size")
	take_profit = _ensure_float(payload.get("take_profit"), "take_profit")
	if lot_size <= 0:
		raise GeminiVertexRequestError("Field 'lot_size' must be greater than 0")

	return {
		"recommended_symbol": recommended_symbol.strip(),
		"action": action,
		"lot_size": lot_size,
		"take_profit": take_profit,
		"reasoning": reasoning.strip(),
	}


def _sleep_before_retry(attempt: int) -> None:
	backoff_seconds = _DEFAULT_BACKOFF_SECONDS * attempt
	print(f"  ⏳ Gemini retry backoff: {backoff_seconds:.1f}s")
	time.sleep(backoff_seconds)


def _request_structured_json(
	*,
	config: GeminiVertexConfig,
	prompt: str,
	response_schema: Dict[str, Any],
	validator: Callable[[Dict[str, Any]], Dict[str, Any]],
	task_name: str,
	max_output_tokens: int,
) -> Dict[str, Any]:
	genai, types = _import_google_genai()
	os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = config.credentials_path

	last_error: Optional[GeminiVertexRequestError] = None

	for model_index, model_name in enumerate(config.fallback_models, start=1):
		for attempt in range(1, _DEFAULT_MAX_ATTEMPTS_PER_MODEL + 1):
			started_at = time.perf_counter()
			response = None
			response_text = ""
			try:
				with genai.Client(
					vertexai=True,
					project=config.project,
					location=config.region,
					http_options=types.HttpOptions(
						api_version="v1",
						timeout=_DEFAULT_TIMEOUT_SECONDS,
					),
				) as client:
					response = client.models.generate_content(
						model=model_name,
						contents=prompt,
						config=types.GenerateContentConfig(
							temperature=0,
							candidate_count=1,
							max_output_tokens=max_output_tokens,
							response_mime_type="application/json",
							response_schema=response_schema,
							thinking_config=types.ThinkingConfig(
								include_thoughts=False,
								thinking_budget=0,
							),
						),
					)

				response_text = _extract_response_text(response)
				validated_payload = validator(_parse_structured_json_response(response))
				usage_metadata = getattr(response, "usage_metadata", None)
				finish_reason = _extract_finish_reason(response)
				duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
				_log_event(
					"success",
					task=task_name,
					project=config.project,
					region=config.region,
					model=model_name,
					fallback_model=(model_name != config.model),
					fallback_position=model_index,
					attempt=attempt,
					duration_ms=duration_ms,
					response_id=getattr(response, "response_id", None),
					finish_reason=finish_reason,
					max_output_tokens=max_output_tokens,
					prompt_token_count=_extract_usage_value(usage_metadata, "prompt_token_count"),
					response_token_count=_extract_usage_value(usage_metadata, "response_token_count")
					or _extract_usage_value(usage_metadata, "candidates_token_count"),
					total_token_count=_extract_usage_value(usage_metadata, "total_token_count"),
					thoughts_token_count=_extract_usage_value(usage_metadata, "thoughts_token_count"),
				)
				return validated_payload
			except Exception as exc:
				status_code = getattr(exc, "code", None)
				message = getattr(exc, "message", None) or str(exc)
				if not isinstance(status_code, int):
					status_code = None
				last_error = GeminiVertexRequestError(message, status_code=status_code)
				duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
				_log_event(
					"error",
					task=task_name,
					project=config.project,
					region=config.region,
					model=model_name,
					fallback_model=(model_name != config.model),
					fallback_position=model_index,
					attempt=attempt,
					duration_ms=duration_ms,
					status_code=status_code,
					response_snippet=_extract_text_snippet(response_text),
					error_snippet=_extract_text_snippet(message),
				)
				if attempt < _DEFAULT_MAX_ATTEMPTS_PER_MODEL:
					_sleep_before_retry(attempt)

	if last_error is not None:
		raise last_error
	raise GeminiVertexRequestError(f"Gemini request failed for task '{task_name}'")


def request_prediction_json(config: GeminiVertexConfig, symbol: str, prompt: str) -> str:
	"""Request a structured prediction payload and serialize it back to JSON."""
	payload = _request_structured_json(
		config=config,
		prompt=prompt,
		response_schema=_PREDICTION_RESPONSE_SCHEMA,
		validator=lambda raw: _validate_prediction_payload(symbol, raw),
		task_name=f"prediction:{symbol}",
		max_output_tokens=_PREDICTION_MAX_OUTPUT_TOKENS,
	)
	return json.dumps(payload, ensure_ascii=False, indent=2)


def request_final_decision_json(config: GeminiVertexConfig, prompt: str) -> str:
	"""Request a structured final decision payload and serialize it back to JSON."""
	payload = _request_structured_json(
		config=config,
		prompt=prompt,
		response_schema=_FINAL_DECISION_RESPONSE_SCHEMA,
		validator=_validate_final_decision_payload,
		task_name="final-decision",
		max_output_tokens=_FINAL_DECISION_MAX_OUTPUT_TOKENS,
	)
	return json.dumps(payload, ensure_ascii=False, indent=2)