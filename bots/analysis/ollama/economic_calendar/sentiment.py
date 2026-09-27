from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import exp
from typing import Iterable

from .models import CurrencySentiment


_WEIGHTS = {"HIGH": 1.0, "MODERATE": 0.5, "LOW": 0.2, "NONE": 0.0}


def aggregate_currency_sentiment(currency: str, analyses: Iterable[dict], *, now_utc: datetime, max_age_hours: int, decay_hours: int) -> CurrencySentiment:
    """Weighted mean: importance * confidence/100 * exp(-age/decay); stale events expire."""
    weighted_score = 0.0
    total_weight = 0.0
    contributors: list[str] = []
    confidence_weighted = 0.0
    for analysis in analyses:
        if analysis.get("currency") != currency or analysis.get("sentiment_score") is None:
            continue
        try:
            analysed_at = datetime.fromisoformat(str(analysis["analysed_at_utc"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            age_hours = (now_utc.astimezone(timezone.utc) - analysed_at).total_seconds() / 3600
            confidence = int(analysis["confidence"])
            score = float(analysis["sentiment_score"])
        except (KeyError, TypeError, ValueError):
            continue
        if age_hours < 0 or age_hours > max_age_hours:
            continue
        weight = _WEIGHTS.get(str(analysis.get("importance", "NONE")).upper(), 0.0) * (confidence / 100) * exp(-age_hours / decay_hours)
        if weight <= 0:
            continue
        weighted_score += score * weight
        total_weight += weight
        confidence_weighted += confidence * weight
        contributors.append(f"{analysis.get('event_id', '')}/{analysis.get('value_id', '')}")
    if not total_weight:
        return CurrencySentiment(currency, "NO_DATA", None, 0, [], now_utc)
    return CurrencySentiment(currency, "AVAILABLE", max(-1.0, min(1.0, weighted_score / total_weight)), round(confidence_weighted / total_weight), contributors, now_utc)


def pair_sentiment(base: CurrencySentiment, quote: CurrencySentiment) -> dict:
    if base.status == "AVAILABLE" and quote.status == "AVAILABLE":
        return {"status": "AVAILABLE", "sentiment_score": max(-1.0, min(1.0, (base.sentiment_score or 0) - (quote.sentiment_score or 0))), "confidence": min(base.confidence, quote.confidence)}
    if base.status == "AVAILABLE" or quote.status == "AVAILABLE":
        available = base if base.status == "AVAILABLE" else quote
        return {"status": "PARTIAL", "sentiment_score": None, "confidence": available.confidence // 2}
    return {"status": "NO_DATA", "sentiment_score": None, "confidence": 0}