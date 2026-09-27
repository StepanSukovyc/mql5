from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    value_id: str
    country_id: Optional[str]
    country_code: str
    currency: str
    event_name: str
    importance: str
    event_time_server: str
    event_time_utc: Optional[datetime]
    time_is_trusted: bool
    actual: Optional[float]
    forecast: Optional[float]
    previous: Optional[float]
    revised: Optional[float]
    status: str
    source: str
    retrieved_at_utc: datetime
    time_untrusted_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("event_time_utc", "retrieved_at_utc"):
            value = data[key]
            data[key] = value.isoformat() if value else None
        return data


@dataclass(frozen=True)
class SymbolExposure:
    symbol: str
    normalized_symbol: str
    currencies: tuple[str, ...]
    assets: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class EventRiskResult:
    symbol: str
    risk_level: str
    allow_new_trade: bool
    relevant_currencies: list[str]
    blocking_events: list[CalendarEvent] = field(default_factory=list)
    warning_events: list[CalendarEvent] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    calendar_status: str = "AVAILABLE"
    would_block: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "risk_level": self.risk_level,
            "allow_new_trade": self.allow_new_trade,
            "relevant_currencies": self.relevant_currencies,
            "blocking_events": [event.to_dict() for event in self.blocking_events],
            "warning_events": [event.to_dict() for event in self.warning_events],
            "reason_codes": self.reason_codes,
            "calendar_status": self.calendar_status,
            "would_block": self.would_block,
        }


@dataclass(frozen=True)
class CurrencySentiment:
    currency: str
    status: str
    sentiment_score: Optional[float]
    confidence: int
    contributing_events: list[str]
    generated_at_utc: datetime
