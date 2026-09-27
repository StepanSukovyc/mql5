from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

import httpx

from .models import CalendarEvent


def _validate_response(payload: object, event: CalendarEvent) -> dict | None:
    if not isinstance(payload, dict):
        return None
    required = {"currency", "event_id", "direction", "sentiment_score", "confidence", "surprise_direction", "reason", "data_quality"}
    if not required.issubset(payload):
        return None
    try:
        score = float(payload["sentiment_score"])
        confidence = int(payload["confidence"])
    except (TypeError, ValueError):
        return None
    if payload["currency"] != event.currency or str(payload["event_id"]) != event.event_id:
        return None
    if not -1 <= score <= 1 or not 0 <= confidence <= 100:
        return None
    if payload["direction"] not in {"BULLISH", "BEARISH", "NEUTRAL", "UNKNOWN"}:
        return None
    if payload["surprise_direction"] not in {"ABOVE", "BELOW", "INLINE", "UNKNOWN"}:
        return None
    if payload["data_quality"] not in {"COMPLETE", "PARTIAL", "INSUFFICIENT"}:
        return None
    payload["sentiment_score"] = score
    payload["confidence"] = confidence
    payload["reason"] = str(payload["reason"])[:300]
    return payload


def analyse_released_event(
    event: CalendarEvent,
    ollama_url: str,
    model: str,
    timeout_seconds: int,
    max_retries: int = 1,
    request: Callable[..., object] = httpx.post,
) -> dict:
    """Return strict advisory sentiment. It never returns a trade recommendation."""
    if event.actual is None or not model:
        return {"currency": event.currency, "event_id": event.event_id, "value_id": event.value_id, "direction": "UNKNOWN", "sentiment_score": None, "confidence": 0, "surprise_direction": "UNKNOWN", "reason": "published actual or model unavailable", "data_quality": "INSUFFICIENT", "analysed_at_utc": datetime.now(timezone.utc).isoformat()}
    prompt = (
        "You are evaluating one published macroeconomic event. Do not recommend a trade. "
        "Do not evaluate a currency pair. Do not invent missing values. Use only supplied data. "
        "Return only JSON with currency,event_id,direction(BULLISH|BEARISH|NEUTRAL|UNKNOWN),"
        "sentiment_score(-1..1),confidence(0..100),surprise_direction(ABOVE|BELOW|INLINE|UNKNOWN),"
        "reason,data_quality(COMPLETE|PARTIAL|INSUFFICIENT).\n"
        f"Currency: {event.currency}\nEvent: {event.event_name}\nImportance: {event.importance}\n"
        f"Actual: {event.actual}\nForecast: {event.forecast}\nPrevious: {event.previous}\nRevised: {event.revised}"
    )
    for _ in range(max_retries + 1):
        try:
            response = request(ollama_url.rstrip("/") + "/api/generate", json={"model": model, "prompt": prompt, "stream": False, "format": "json"}, timeout=timeout_seconds)
            response.raise_for_status()
            raw = response.json().get("response", "")
            validated = _validate_response(json.loads(raw), event)
            if validated:
                return {**validated, "value_id": event.value_id, "analysed_at_utc": datetime.now(timezone.utc).isoformat(), "model": model}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            continue
    return {"currency": event.currency, "event_id": event.event_id, "value_id": event.value_id, "direction": "UNKNOWN", "sentiment_score": None, "confidence": 0, "surprise_direction": "UNKNOWN", "reason": "invalid Ollama response", "data_quality": "INSUFFICIENT", "analysed_at_utc": datetime.now(timezone.utc).isoformat(), "model": model}