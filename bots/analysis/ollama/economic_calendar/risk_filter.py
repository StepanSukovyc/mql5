from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import EconomicCalendarConfig
from .models import CalendarEvent, EventRiskResult
from .symbol_mapping import get_symbol_exposure


def evaluate_event_risk(
    symbol: str,
    now_utc: datetime,
    events: Iterable[CalendarEvent],
    config: EconomicCalendarConfig,
    calendar_status: str = "AVAILABLE",
) -> EventRiskResult:
    now_utc = now_utc.astimezone(timezone.utc)
    exposure = get_symbol_exposure(symbol, config.symbol_exposure_map)
    if not config.enabled:
        return EventRiskResult(symbol, "NONE", True, list(exposure.currencies), calendar_status="AVAILABLE")
    if calendar_status in {"UNAVAILABLE", "INVALID", "STALE"}:
        allow = config.fail_mode == "OPEN"
        return EventRiskResult(symbol, "UNKNOWN", allow, list(exposure.currencies), [ ], [ ], [f"ECONOMIC_CALENDAR_{calendar_status}"], calendar_status, not allow)
    if exposure.status == "UNMAPPED":
        return EventRiskResult(symbol, "UNKNOWN", True, [], reason_codes=["ECONOMIC_SYMBOL_UNMAPPED"], calendar_status=calendar_status)

    blocking: list[CalendarEvent] = []
    warnings: list[CalendarEvent] = []
    for event in events:
        if event.currency not in exposure.currencies or not event.time_is_trusted or event.event_time_utc is None:
            continue
        importance = event.importance.upper()
        if importance == "HIGH":
            before, after, blocks = config.high_before_minutes, config.high_after_minutes, True
        elif importance == "MODERATE":
            before, after, blocks = config.moderate_before_minutes, config.moderate_after_minutes, config.moderate_block_enabled
        else:
            before, after, blocks = 0, 0, False
        within_window = event.event_time_utc - timedelta(minutes=before) <= now_utc <= event.event_time_utc + timedelta(minutes=after)
        if within_window and blocks:
            blocking.append(event)
        elif within_window or importance == "MODERATE":
            warnings.append(event)
    risk = "HIGH" if blocking else ("MEDIUM" if warnings else "NONE")
    would_block = bool(blocking)
    allow = not would_block or config.dry_run
    reasons = ["ECONOMIC_EVENT_BLOCKED" if would_block else "ECONOMIC_EVENT_WARNING"] if blocking or warnings else []
    return EventRiskResult(symbol, risk, allow, list(exposure.currencies), blocking, warnings, reasons, calendar_status, would_block)