"""External economic-news filter used to block new entries around high-impact events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from typing import Any, Iterable, List, Optional

import httpx

from instrument_utils import get_symbol_news_currencies


@dataclass(frozen=True)
class EconomicNewsEvent:
	timestamp_utc: datetime
	currencies: tuple[str, ...]
	impact: str
	title: str

	def to_dict(self) -> dict[str, object]:
		return {
			"timestamp_utc": self.timestamp_utc.isoformat(),
			"currencies": list(self.currencies),
			"impact": self.impact,
			"title": self.title,
		}


@dataclass(frozen=True)
class NewsFilterDecision:
	blocked: bool
	reason: str
	symbol: str
	currencies: tuple[str, ...]
	relevant_events: tuple[EconomicNewsEvent, ...]

	def to_dict(self) -> dict[str, object]:
		return {
			"blocked": self.blocked,
			"reason": self.reason,
			"symbol": self.symbol,
			"currencies": list(self.currencies),
			"relevant_events": [event.to_dict() for event in self.relevant_events],
		}


_CACHE_KEY: Optional[str] = None
_CACHE_EVENTS: tuple[EconomicNewsEvent, ...] = ()


def reset_news_filter_cache() -> None:
	"""Clear cached news events. Useful for isolated tests."""
	global _CACHE_KEY, _CACHE_EVENTS
	_CACHE_KEY = None
	_CACHE_EVENTS = ()


def _to_bool(name: str, default: bool) -> bool:
	raw = os.getenv(name)
	if raw is None:
		return default
	return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _to_int(name: str, default: int, *, minimum: int) -> int:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		value = int(raw)
		if value < minimum:
			raise ValueError
		return value
	except (TypeError, ValueError):
		return default


def _to_float(name: str, default: float, *, minimum: float) -> float:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		value = float(raw)
		if value < minimum:
			raise ValueError
		return value
	except (TypeError, ValueError):
		return default


def is_news_filter_enabled() -> bool:
	"""Return whether the external economic-news filter is active."""
	return _to_bool("NEWS_FILTER_ENABLED", False)


def _get_news_api_url() -> str:
	return os.getenv("NEWS_FILTER_API_URL", "").strip()


def _get_news_api_token() -> str:
	return os.getenv("NEWS_FILTER_API_TOKEN", "").strip()


def _get_news_api_token_header() -> str:
	return os.getenv("NEWS_FILTER_API_TOKEN_HEADER", "Authorization").strip() or "Authorization"


def _get_news_api_token_prefix() -> str:
	return os.getenv("NEWS_FILTER_API_TOKEN_PREFIX", "Bearer ")


def _get_timeout_seconds() -> float:
	return _to_float("NEWS_FILTER_TIMEOUT_SECONDS", 10.0, minimum=1.0)


def _get_lookahead_minutes() -> int:
	return _to_int("NEWS_FILTER_LOOKAHEAD_MINUTES", 30, minimum=0)


def _get_lookback_minutes() -> int:
	return _to_int("NEWS_FILTER_LOOKBACK_MINUTES", 15, minimum=0)


def _get_impacts() -> set[str]:
	raw = os.getenv("NEWS_FILTER_IMPACTS", "high")
	return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _resolve_api_url(*, from_utc: datetime, to_utc: datetime) -> str:
	url = _get_news_api_url()
	token = _get_news_api_token()
	return (
		url.replace("{from_iso}", from_utc.isoformat())
		.replace("{to_iso}", to_utc.isoformat())
		.replace("{from_date}", from_utc.strftime("%Y-%m-%d"))
		.replace("{to_date}", to_utc.strftime("%Y-%m-%d"))
		.replace("{token}", token)
	)


def _parse_datetime(value: Any) -> Optional[datetime]:
	if value is None:
		return None
	if isinstance(value, (int, float)):
		try:
			return datetime.fromtimestamp(float(value), tz=timezone.utc)
		except (OverflowError, OSError, ValueError):
			return None
	if isinstance(value, str):
		text = value.strip()
		if not text:
			return None
		if text.endswith("Z"):
			text = text[:-1] + "+00:00"
		try:
			parsed = datetime.fromisoformat(text)
			if parsed.tzinfo is None:
				return parsed.replace(tzinfo=timezone.utc)
			return parsed.astimezone(timezone.utc)
		except ValueError:
			return None
	return None


def _normalize_impact(value: Any) -> Optional[str]:
	if value is None:
		return None
	if isinstance(value, (int, float)):
		if float(value) >= 3:
			return "high"
		if float(value) >= 2:
			return "medium"
		return "low"
	text = str(value).strip().lower()
	if not text:
		return None
	if any(token in text for token in ("high", "3", "red")):
		return "high"
	if any(token in text for token in ("medium", "med", "2", "orange")):
		return "medium"
	if any(token in text for token in ("low", "1", "yellow")):
		return "low"
	return text


def _extract_event_items(payload: Any) -> Iterable[dict[str, Any]]:
	if isinstance(payload, list):
		return [item for item in payload if isinstance(item, dict)]
	if isinstance(payload, dict):
		for key in ("events", "data", "news", "calendar", "items", "results"):
			value = payload.get(key)
			if isinstance(value, list):
				return [item for item in value if isinstance(item, dict)]
		return [payload]
	return []


def _extract_currencies(item: dict[str, Any]) -> tuple[str, ...]:
	for key in ("currencies", "currency", "countryCode", "country"):
		value = item.get(key)
		if value is None:
			continue
		if isinstance(value, list):
			currencies = [str(entry).strip().upper() for entry in value if str(entry).strip()]
			if currencies:
				return tuple(currencies)
		text = str(value).replace("|", ",")
		currencies = [part.strip().upper() for part in text.split(",") if part.strip()]
		if currencies:
			return tuple(currencies)
	return ()


def _parse_event(item: dict[str, Any]) -> Optional[EconomicNewsEvent]:
	timestamp = None
	for key in ("timestamp", "datetime", "date", "time", "published_at", "event_time"):
		timestamp = _parse_datetime(item.get(key))
		if timestamp is not None:
			break
	if timestamp is None:
		return None

	impact = None
	for key in ("impact", "importance", "volatility"):
		impact = _normalize_impact(item.get(key))
		if impact is not None:
			break
	if impact is None:
		impact = "unknown"

	title = ""
	for key in ("title", "event", "name", "headline"):
		value = item.get(key)
		if value:
			title = str(value).strip()
			break

	return EconomicNewsEvent(
		timestamp_utc=timestamp,
		currencies=_extract_currencies(item),
		impact=impact,
		title=title,
	)


def _fetch_news_events(*, now_utc: datetime) -> tuple[EconomicNewsEvent, ...]:
	global _CACHE_KEY, _CACHE_EVENTS
	lookback_minutes = _get_lookback_minutes()
	lookahead_minutes = _get_lookahead_minutes()
	from_utc = now_utc - timedelta(minutes=lookback_minutes)
	to_utc = now_utc + timedelta(minutes=lookahead_minutes)
	url = _resolve_api_url(from_utc=from_utc, to_utc=to_utc)
	impacts = sorted(_get_impacts())
	cache_key = f"{url}|{from_utc.isoformat()}|{to_utc.isoformat()}|{','.join(impacts)}"
	if _CACHE_KEY == cache_key:
		return _CACHE_EVENTS

	headers: dict[str, str] = {}
	token = _get_news_api_token()
	if token and "{token}" not in _get_news_api_url():
		headers[_get_news_api_token_header()] = f"{_get_news_api_token_prefix()}{token}"

	response = httpx.get(url, headers=headers, timeout=_get_timeout_seconds())
	response.raise_for_status()
	payload = response.json()
	events: List[EconomicNewsEvent] = []
	allowed_impacts = _get_impacts()
	for item in _extract_event_items(payload):
		event = _parse_event(item)
		if event is None:
			continue
		if allowed_impacts and event.impact.lower() not in allowed_impacts:
			continue
		events.append(event)

	_CACHE_KEY = cache_key
	_CACHE_EVENTS = tuple(events)
	return _CACHE_EVENTS


def should_block_symbol_for_news(symbol: str, *, now_utc: datetime | None = None) -> NewsFilterDecision:
	"""Return whether a new entry for the symbol should be blocked by economic news."""
	now = now_utc or datetime.now(tz=timezone.utc)
	if not is_news_filter_enabled():
		return NewsFilterDecision(False, "disabled", symbol, (), ())
	if not _get_news_api_url():
		return NewsFilterDecision(False, "missing_api_url", symbol, (), ())

	currencies = tuple(get_symbol_news_currencies(symbol))
	if not currencies:
		return NewsFilterDecision(False, "no_currency_mapping", symbol, (), ())

	try:
		events = _fetch_news_events(now_utc=now)
	except Exception as exc:  # pylint: disable=broad-except
		return NewsFilterDecision(False, f"fetch_failed:{exc}", symbol, currencies, ())

	lookback_cutoff = now - timedelta(minutes=_get_lookback_minutes())
	lookahead_cutoff = now + timedelta(minutes=_get_lookahead_minutes())
	relevant_events = tuple(
		event
		for event in events
		if event.timestamp_utc >= lookback_cutoff
		and event.timestamp_utc <= lookahead_cutoff
		and set(event.currencies).intersection(currencies)
	)
	if relevant_events:
		return NewsFilterDecision(True, "high_impact_news_window", symbol, currencies, relevant_events)

	return NewsFilterDecision(False, "clear", symbol, currencies, ())