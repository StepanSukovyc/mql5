"""Shared Gemini Vertex AI configuration helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


_DEFAULT_VERTEX_MODEL = "gemini-2.5-flash"
_MODEL_FALLBACK_CHAINS = {
	"gemini-2.0-flash-001": (
		"gemini-2.0-flash-001",
		"gemini-2.0-flash",
		"gemini-2.5-flash",
	),
	"gemini-2.0-flash": (
		"gemini-2.0-flash",
		"gemini-2.5-flash",
	),
	"gemini-2.5-flash": (
		"gemini-2.5-flash",
		"gemini-2.0-flash-001",
		"gemini-2.0-flash",
	),
	"gemini-2.0-flash-lite-001": (
		"gemini-2.0-flash-lite-001",
		"gemini-2.0-flash-lite",
		"gemini-2.5-flash-lite",
	),
	"gemini-2.0-flash-lite": (
		"gemini-2.0-flash-lite",
		"gemini-2.5-flash-lite",
	),
	"gemini-2.5-flash-lite": (
		"gemini-2.5-flash-lite",
		"gemini-2.0-flash-lite-001",
		"gemini-2.0-flash-lite",
	),
}


@dataclass(frozen=True)
class GeminiVertexConfig:
	"""Runtime configuration for Gemini on Vertex AI."""

	credentials_path: str
	project: str
	region: str
	model: str
	fallback_models: Tuple[str, ...]


def _build_model_fallbacks(model: str) -> Tuple[str, ...]:
	"""Return the configured model plus allowed fallbacks in execution order."""
	configured_model = (model or _DEFAULT_VERTEX_MODEL).strip() or _DEFAULT_VERTEX_MODEL
	return _MODEL_FALLBACK_CHAINS.get(configured_model, (configured_model,))


def _resolve_credentials_path(credentials_path: str) -> Path:
	"""Resolve credentials path against cwd first, then known .env directories."""
	configured_path = Path(credentials_path).expanduser()
	if configured_path.is_absolute() and configured_path.exists():
		return configured_path

	resolution_roots = [
		Path.cwd(),
		Path(__file__).resolve().parent,
		Path(__file__).resolve().parent.parent,
	]

	for root in resolution_roots:
		candidate = (root / configured_path).resolve()
		if candidate.exists():
			return candidate

	raise ValueError(
		"GOOGLE_APPLICATION_CREDENTIALS points to a missing file: "
		f"{credentials_path}"
	)


def load_gemini_api_config() -> GeminiVertexConfig:
	"""Load Vertex AI Gemini configuration from environment."""
	credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
	project = os.getenv("VERTEX_AI_PROJECT_ID")
	region = os.getenv("VERTEX_AI_REGION")
	model = os.getenv("VERTEX_AI_MODEL", _DEFAULT_VERTEX_MODEL)

	if not credentials_path:
		raise ValueError("GOOGLE_APPLICATION_CREDENTIALS not found in environment")
	resolved_credentials_path = _resolve_credentials_path(credentials_path)
	if not project:
		raise ValueError("VERTEX_AI_PROJECT_ID not found in environment")
	if not region:
		raise ValueError("VERTEX_AI_REGION not found in environment")

	fallback_models = _build_model_fallbacks(model)
	return GeminiVertexConfig(
		credentials_path=str(resolved_credentials_path),
		project=project,
		region=region,
		model=fallback_models[0],
		fallback_models=fallback_models,
	)