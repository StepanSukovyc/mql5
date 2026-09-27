from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .config import EconomicCalendarConfig
from .mt5_file_provider import Mt5CalendarFileProvider
from .risk_filter import evaluate_event_risk
from .storage import append_audit_jsonl, append_filter_audit, atomic_write_json
from .symbol_mapping import get_symbol_exposure


class EconomicCalendarService:
    """Facade used only by new-entry code paths; it never manages open positions."""

    def __init__(self, service_folder: Path | None = None, config: EconomicCalendarConfig | None = None) -> None:
        self.config = config or EconomicCalendarConfig.from_env()
        self.service_folder = service_folder
        self.provider = Mt5CalendarFileProvider(self.config)

    def evaluate(self, symbol: str):
        events, status = self.provider.load()
        result = evaluate_event_risk(symbol, datetime.now(timezone.utc), events, self.config, status)
        if self.service_folder:
            snapshot = {"schema_version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(), "source": "MT5_ECONOMIC_CALENDAR", "source_status": status, "data_age_seconds": 0, "events": [event.to_dict() for event in events]}
            calendar_folder = self.service_folder / "economic_calendar"
            atomic_write_json(calendar_folder / "upcoming_events.json", {**snapshot, "events": [event.to_dict() for event in events if event.status == "SCHEDULED"]})
            atomic_write_json(calendar_folder / "released_events.json", {**snapshot, "events": [event.to_dict() for event in events if event.status != "SCHEDULED"]})
            atomic_write_json(calendar_folder / "currency_sentiment.json", {"schema_version": 1, "generated_at_utc": snapshot["generated_at_utc"], "source": "MT5_ECONOMIC_CALENDAR", "source_status": status, "data_age_seconds": 0, "currencies": {}})
            atomic_write_json(calendar_folder / "pair_sentiment" / f"{symbol}.json", {"schema_version": 1, "generated_at_utc": snapshot["generated_at_utc"], "source": "MT5_ECONOMIC_CALENDAR", "source_status": status, "data_age_seconds": 0, "symbol": symbol, "status": "NO_DATA", "sentiment_score": None})
            exposure = get_symbol_exposure(symbol, self.config.symbol_exposure_map)
            event = (result.blocking_events or result.warning_events or [None])[0]
            append_filter_audit(self.service_folder, {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(), "symbol": symbol, "mapping_status": exposure.status,
                "calendar_status": result.calendar_status, "risk_level": result.risk_level, "allow_new_trade": result.allow_new_trade,
                "would_block": result.would_block, "event_id": event.event_id if event else "", "value_id": event.value_id if event else "",
                "event_name": event.event_name if event else "", "currency": event.currency if event else "",
                "importance": event.importance if event else "", "event_time_utc": event.event_time_utc.isoformat() if event and event.event_time_utc else "",
                "decision": "ALLOW" if result.allow_new_trade else "BLOCK", "reason_code": ";".join(result.reason_codes),
            })
            append_audit_jsonl(self.service_folder, "risk_evaluated", result.to_dict())
        return result

    def context_for_symbol(self, symbol: str) -> dict:
        result = self.evaluate(symbol)
        exposure = get_symbol_exposure(symbol, self.config.symbol_exposure_map)
        return {
            "calendar_status": result.calendar_status, "symbol_mapping_status": exposure.status,
            "risk_level": result.risk_level, "new_trade_blocked": result.would_block,
            "upcoming_relevant_events": [event.to_dict() for event in result.blocking_events + result.warning_events][:3],
            "recent_published_events": [], "base_currency_sentiment": {}, "quote_currency_sentiment": {},
            "pair_sentiment": {"status": "NO_DATA"}, "data_generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }