from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import EconomicCalendarConfig
from .models import CalendarEvent


class Mt5CalendarFileProvider:
    """Reads the atomically exported JSON produced by the read-only MQL5 bridge."""

    def __init__(self, config: EconomicCalendarConfig) -> None:
        self.config = config
        self._last_valid_events: list[CalendarEvent] = []
        self._last_valid_at: datetime | None = None

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
        except ValueError:
            return None

    @staticmethod
    def _number(value: object) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _normalise(self, raw: dict[str, Any], retrieved_at: datetime) -> CalendarEvent | None:
        event_id = str(raw.get("event_id", "")).strip()
        value_id = str(raw.get("value_id", "")).strip()
        if not event_id or not value_id:
            return None
        event_time_utc = self._parse_time(raw.get("event_time_utc"))
        trusted = bool(raw.get("time_is_trusted", False)) and event_time_utc is not None
        actual = self._number(raw.get("actual"))
        return CalendarEvent(
            event_id=event_id, value_id=value_id, country_id=str(raw["country_id"]) if raw.get("country_id") is not None else None,
            country_code=str(raw.get("country_code", "")).upper(), currency=str(raw.get("currency", "")).upper(),
            event_name=str(raw.get("event_name", "")), importance=str(raw.get("importance", "NONE")).upper(),
            event_time_server=str(raw.get("event_time_server", "")), event_time_utc=event_time_utc,
            time_is_trusted=trusted, actual=actual, forecast=self._number(raw.get("forecast")),
            previous=self._number(raw.get("previous")), revised=self._number(raw.get("revised")),
            status="RELEASED" if actual is not None else str(raw.get("status", "SCHEDULED")).upper(),
            source="MT5_ECONOMIC_CALENDAR", retrieved_at_utc=retrieved_at,
            time_untrusted_reason=None if trusted else str(raw.get("time_untrusted_reason", "missing_or_untrusted_utc_offset")),
        )

    def load(self, now_utc: datetime | None = None) -> tuple[list[CalendarEvent], str]:
        now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not self.config.export_path or not self.config.export_path.exists():
            return self._fallback(now_utc, "UNAVAILABLE")
        try:
            document = json.loads(self.config.export_path.read_text(encoding="utf-8"))
            generated_at = self._parse_time(document.get("generated_at_utc"))
            raw_events = document.get("events", [])
            if generated_at is None or not isinstance(raw_events, list):
                raise ValueError("invalid calendar document")
            events = [event for raw in raw_events if isinstance(raw, dict) and (event := self._normalise(raw, generated_at))]
            if now_utc - generated_at > timedelta(minutes=self.config.max_data_age_minutes):
                return self._fallback(now_utc, "STALE")
            self._last_valid_events, self._last_valid_at = events, generated_at
            return events, "AVAILABLE"
        except (OSError, json.JSONDecodeError, ValueError):
            return self._fallback(now_utc, "INVALID")

    def _fallback(self, now_utc: datetime, status: str) -> tuple[list[CalendarEvent], str]:
        if self._last_valid_at and now_utc - self._last_valid_at <= timedelta(minutes=self.config.max_data_age_minutes):
            return self._last_valid_events, "AVAILABLE"
        return [], status