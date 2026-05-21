"""Shared Gemini Vertex AI request helpers with structured JSON validation."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

import httpx

from gemini_config import GeminiVertexConfig


_DEFAULT_MAX_ATTEMPTS_PER_MODEL = 2
_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_BACKOFF_SECONDS = 2.0
_GOOGLE_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_LEGACY_GEMINI_TIMEOUT_SECONDS = 60.0
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


def _import_google_auth():
	try:
		from google.auth.transport.requests import Request
		from google.oauth2 import service_account
		return Request, service_account
	except ImportError as exc:
		raise GeminiVertexError(
			"Missing dependency 'google-auth'. Install it with 'pip install -r requirements.txt'."
		) from exc


def _log_event(event: str, **payload: Any) -> None:
	message = {"event": event, **payload}
	print(f"  🧠 Gemini Vertex: {json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)}")


def _extract_response_text(response: Any) -> str:
	if isinstance(response, dict):
		candidates = response.get("candidates") or []
		if candidates:
			content = candidates[0].get("content") or {}
			parts = content.get("parts") or []
			fragments = [str(part.get("text", "")) for part in parts if isinstance(part, dict) and part.get("text")]
			return "".join(fragments).strip()
		return ""

	text = getattr(response, "text", None)
	if isinstance(text, str):
		return text.strip()

	candidates = getattr(response, "candidates", None) or []
	if candidates:
		content = getattr(candidates[0], "content", None)
		parts = getattr(content, "parts", None) or []
		fragments = []
		for part in parts:
			part_text = getattr(part, "text", None)
			if isinstance(part_text, str) and part_text:
				fragments.append(part_text)
		if fragments:
			return "".join(fragments).strip()
	return ""


def _extract_text_snippet(text: str, limit: int = 200) -> str:
	cleaned = " ".join(text.split())
	if len(cleaned) <= limit:
		return cleaned
	return cleaned[: limit - 3] + "..."


def _extract_finish_reason(response: Any) -> str:
	if isinstance(response, dict):
		candidates = response.get("candidates") or []
		if not candidates:
			return ""
		return str(candidates[0].get("finishReason") or "")

	candidates = getattr(response, "candidates", None) or []
	if not candidates:
		return ""
	finish_reason = getattr(candidates[0], "finish_reason", "")
	return str(finish_reason or "")


def _extract_usage_value(usage_metadata: Any, name: str) -> Optional[int]:
	if isinstance(usage_metadata, dict):
		value = usage_metadata.get(name)
	else:
		value = getattr(usage_metadata, name, None)
	if value is None:
		return None
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def _strip_markdown_code_fences(text: str) -> str:
	cleaned = text.strip()
	if cleaned.startswith("```json"):
		cleaned = cleaned[7:]
	elif cleaned.startswith("```"):
		cleaned = cleaned[3:]

	if cleaned.endswith("```"):
		cleaned = cleaned[:-3]

	return cleaned.strip()


def _load_json_object(text: str) -> Dict[str, Any]:
	decoded = json.loads(text)
	if not isinstance(decoded, dict):
		raise GeminiVertexRequestError("Gemini returned JSON that is not an object")
	return decoded


def _extract_balanced_json_object(text: str) -> Optional[str]:
	start = text.find("{")
	if start < 0:
		return None

	depth = 0
	in_string = False
	escaped = False
	for index in range(start, len(text)):
		char = text[index]
		if in_string:
			if escaped:
				escaped = False
			elif char == "\\":
				escaped = True
			elif char == '"':
				in_string = False
			continue

		if char == '"':
			in_string = True
		elif char == "{":
			depth += 1
		elif char == "}":
			depth -= 1
			if depth == 0:
				return text[start:index + 1]

	return None


def _parse_json_object_from_text(response_text: str) -> Dict[str, Any]:
	cleaned_text = _strip_markdown_code_fences(response_text)
	try:
		return _load_json_object(cleaned_text)
	except json.JSONDecodeError:
		candidate = _extract_balanced_json_object(cleaned_text)
		if candidate:
			return _load_json_object(candidate)
		raise


def _parse_structured_json_response(response: Any) -> Dict[str, Any]:
	parsed = getattr(response, "parsed", None)
	if isinstance(parsed, dict):
		return parsed

	response_text = _extract_response_text(response)
	if not response_text:
		raise GeminiVertexRequestError("Gemini returned an empty structured response")

	try:
		decoded = _parse_json_object_from_text(response_text)
	except json.JSONDecodeError as exc:
		raise GeminiVertexRequestError(
			f"Gemini returned invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}. "
			f"Raw response snippet: {_extract_text_snippet(response_text, limit=320)}",
		) from exc

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


def _should_try_rest_fallback(exc: Exception, status_code: Optional[int]) -> bool:
	if isinstance(status_code, int):
		return False

	message = (getattr(exc, "message", None) or str(exc) or "").lower()
	module_name = type(exc).__module__.lower()
	class_name = type(exc).__name__.lower()
	return (
		"returned invalid json" in message
		or "empty structured response" in message
		or "timed out" in message
		or "timeout" in message
		or module_name.startswith("httpx")
		or module_name.startswith("httpcore")
		or class_name.endswith("timeout")
	)


def _should_skip_remaining_vertex_models(status_code: Optional[int], message: str) -> bool:
	if status_code != 404:
		return False

	normalized_message = (message or "").lower()
	return (
		"publisher model" in normalized_message
		and ("was not found" in normalized_message or "does not have access" in normalized_message)
	)


def _get_vertex_rest_endpoint(config: GeminiVertexConfig, model_name: str) -> str:
	host = "aiplatform.googleapis.com" if config.region == "global" else f"{config.region}-aiplatform.googleapis.com"
	return (
		f"https://{host}/v1/projects/{config.project}/locations/{config.region}"
		f"/publishers/google/models/{model_name}:generateContent"
	)


def _get_service_account_access_token(credentials_path: str) -> str:
	Request, service_account = _import_google_auth()
	credentials = service_account.Credentials.from_service_account_file(
		credentials_path,
		scopes=[_GOOGLE_CLOUD_PLATFORM_SCOPE],
	)
	credentials.refresh(Request())
	token = credentials.token
	if not token:
		raise GeminiVertexRequestError("Failed to acquire access token for Vertex AI REST fallback")
	return str(token)


def _load_legacy_gemini_api_config() -> Optional[Dict[str, str]]:
	api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
	api_url = (os.getenv("GEMINI_URL") or "").strip()
	if not api_key or not api_url:
		return None
	return {
		"api_key": api_key,
		"api_url": api_url,
	}


def _get_legacy_prompt_with_schema(prompt: str, response_schema: Dict[str, Any]) -> str:
	properties = response_schema.get("properties") or {}
	required_fields = response_schema.get("required") or []
	if not properties or not required_fields:
		return prompt

	placeholder_values = {
		"STRING": '"text"',
		"NUMBER": "0",
		"INTEGER": "0",
		"BOOLEAN": "false",
	}
	example_lines = []
	for field_name in required_fields:
		spec = properties.get(field_name) or {}
		if spec.get("enum"):
			placeholder = json.dumps(spec["enum"][0], ensure_ascii=False)
		else:
			placeholder = placeholder_values.get(str(spec.get("type") or "").upper(), '"value"')
		example_lines.append(f'  "{field_name}": {placeholder}')

	example_json = "{\n" + ",\n".join(example_lines) + "\n}"
	return (
		f"{prompt}\n\n"
		"POVINNY FORMAT ODPOVEDI:\n"
		f"{example_json}\n"
		"Vrat pouze jeden JSON objekt presne s uvedenymi klici. "
		"Nevracet markdown, code fence, komentar ani zadne dalsi texty pred nebo za JSON."
	)


def _request_structured_json_via_legacy_api(
	*,
	prompt: str,
	api_key: str,
	api_url: str,
) -> Dict[str, Any]:
	request_data = {
		"contents": [
			{
				"parts": [
					{"text": prompt},
				]
			}
		]
	}

	try:
		with httpx.Client(timeout=_LEGACY_GEMINI_TIMEOUT_SECONDS) as client:
			response = client.post(
				api_url,
				json=request_data,
				headers={
					"Content-Type": "application/json",
					"X-goog-api-key": api_key,
				},
			)
	except httpx.HTTPError as exc:
		raise GeminiVertexRequestError(str(exc)) from exc

	if response.status_code == 429:
		raise GeminiVertexRequestError("Legacy Gemini API quota exceeded", status_code=429)

	if response.status_code >= 400:
		raise GeminiVertexRequestError(response.text or response.reason_phrase, status_code=response.status_code)

	try:
		decoded = response.json()
	except json.JSONDecodeError as exc:
		raise GeminiVertexRequestError(f"Legacy Gemini API returned invalid JSON: {exc.msg}") from exc

	if not isinstance(decoded, dict):
		raise GeminiVertexRequestError("Legacy Gemini API returned JSON that is not an object")

	return decoded


def _request_structured_json_via_rest(
	*,
	config: GeminiVertexConfig,
	model_name: str,
	prompt: str,
	response_schema: Dict[str, Any],
	max_output_tokens: int,
) -> Dict[str, Any]:
	access_token = _get_service_account_access_token(config.credentials_path)
	payload = {
		"contents": [
			{
				"role": "user",
				"parts": [{"text": prompt}],
			}
		],
		"generationConfig": {
			"temperature": 0,
			"candidateCount": 1,
			"maxOutputTokens": max_output_tokens,
			"responseMimeType": "application/json",
			"responseSchema": response_schema,
		},
	}
	request = urllib.request.Request(
		_get_vertex_rest_endpoint(config, model_name),
		data=json.dumps(payload).encode("utf-8"),
		headers={
			"Authorization": f"Bearer {access_token}",
			"Content-Type": "application/json; charset=utf-8",
		},
		method="POST",
	)

	try:
		with urllib.request.urlopen(request, timeout=_DEFAULT_TIMEOUT_SECONDS) as response:
			response_text = response.read().decode("utf-8")
	except urllib.error.HTTPError as exc:
		response_text = exc.read().decode("utf-8", errors="replace")
		raise GeminiVertexRequestError(response_text or str(exc), status_code=exc.code) from exc
	except urllib.error.URLError as exc:
		raise GeminiVertexRequestError(str(exc.reason) or str(exc)) from exc

	try:
		decoded = json.loads(response_text)
	except json.JSONDecodeError as exc:
		raise GeminiVertexRequestError(f"Vertex REST fallback returned invalid JSON: {exc.msg}") from exc

	if not isinstance(decoded, dict):
		raise GeminiVertexRequestError("Vertex REST fallback returned JSON that is not an object")

	return decoded


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

	legacy_api_config = _load_legacy_gemini_api_config()
	vertex_models = config.fallback_models if legacy_api_config is None else (config.model,)
	max_attempts_per_model = _DEFAULT_MAX_ATTEMPTS_PER_MODEL if legacy_api_config is None else 1
	last_error: Optional[GeminiVertexRequestError] = None

	for model_index, model_name in enumerate(vertex_models, start=1):
		skip_remaining_vertex_models = False
		for attempt in range(1, max_attempts_per_model + 1):
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

				if legacy_api_config is None and _should_try_rest_fallback(exc, status_code):
					_log_event(
						"fallback_attempt",
						task=task_name,
						project=config.project,
						region=config.region,
						model=model_name,
						fallback_model=(model_name != config.model),
						fallback_position=model_index,
						attempt=attempt,
						transport="vertex-rest-fallback",
						previous_error=_extract_text_snippet(message),
					)
					try:
						rest_response = _request_structured_json_via_rest(
							config=config,
							model_name=model_name,
							prompt=prompt,
							response_schema=response_schema,
							max_output_tokens=max_output_tokens,
						)
						validated_payload = validator(_parse_structured_json_response(rest_response))
						usage_metadata = rest_response.get("usageMetadata")
						finish_reason = _extract_finish_reason(rest_response)
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
							transport="vertex-rest-fallback",
							previous_error=_extract_text_snippet(message),
							response_id=rest_response.get("responseId"),
							finish_reason=finish_reason,
							max_output_tokens=max_output_tokens,
							prompt_token_count=_extract_usage_value(usage_metadata, "promptTokenCount"),
							response_token_count=_extract_usage_value(usage_metadata, "responseTokenCount")
							or _extract_usage_value(usage_metadata, "candidatesTokenCount"),
							total_token_count=_extract_usage_value(usage_metadata, "totalTokenCount"),
							thoughts_token_count=_extract_usage_value(usage_metadata, "thoughtsTokenCount"),
						)
						return validated_payload
					except GeminiVertexRequestError as rest_exc:
						_log_event(
							"error",
							task=task_name,
							project=config.project,
							region=config.region,
							model=model_name,
							fallback_model=(model_name != config.model),
							fallback_position=model_index,
							attempt=attempt,
							duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
							transport="vertex-rest-fallback",
							status_code=rest_exc.status_code,
							error_snippet=_extract_text_snippet(str(rest_exc), limit=320),
						)
						message = str(rest_exc)
						status_code = rest_exc.status_code

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
					transport="google-genai-sdk",
					status_code=status_code,
					response_snippet=_extract_text_snippet(response_text),
					error_snippet=_extract_text_snippet(message),
				)
				skip_remaining_vertex_models = _should_skip_remaining_vertex_models(status_code, message)
				if attempt < max_attempts_per_model and not skip_remaining_vertex_models:
					_sleep_before_retry(attempt)
				if skip_remaining_vertex_models or legacy_api_config is not None:
					break

		if skip_remaining_vertex_models or legacy_api_config is not None:
			break

	if last_error is not None and legacy_api_config is not None:
		legacy_started_at = time.perf_counter()
		print(f"  ↪️  Vertex request failed, switching to legacy Gemini API (timeout {_LEGACY_GEMINI_TIMEOUT_SECONDS:.0f}s)...")
		_log_event(
			"fallback_attempt",
			task=task_name,
			project=config.project,
			region=config.region,
			model="gemini-api-key",
			fallback_model=True,
			fallback_position=len(vertex_models) + 1,
			attempt=1,
			transport="legacy-gemini-api",
			previous_error=_extract_text_snippet(str(last_error), limit=320),
		)
		try:
			legacy_response = _request_structured_json_via_legacy_api(
				prompt=_get_legacy_prompt_with_schema(prompt, response_schema),
				api_key=legacy_api_config["api_key"],
				api_url=legacy_api_config["api_url"],
			)
			validated_payload = validator(_parse_structured_json_response(legacy_response))
			duration_ms = round((time.perf_counter() - legacy_started_at) * 1000, 2)
			_log_event(
				"success",
				task=task_name,
				project=config.project,
				region=config.region,
				model="gemini-api-key",
				fallback_model=True,
				fallback_position=len(vertex_models) + 1,
				attempt=1,
				duration_ms=duration_ms,
				transport="legacy-gemini-api",
				previous_error=_extract_text_snippet(str(last_error), limit=320),
			)
			return validated_payload
		except GeminiVertexRequestError as legacy_exc:
			duration_ms = round((time.perf_counter() - legacy_started_at) * 1000, 2)
			_log_event(
				"error",
				task=task_name,
				project=config.project,
				region=config.region,
				model="gemini-api-key",
				fallback_model=True,
				fallback_position=len(vertex_models) + 1,
				attempt=1,
				duration_ms=duration_ms,
				transport="legacy-gemini-api",
				status_code=legacy_exc.status_code,
				error_snippet=_extract_text_snippet(str(legacy_exc), limit=320),
			)
			last_error = legacy_exc

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