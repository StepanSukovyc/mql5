from datetime import datetime, timedelta, timezone
import json

from economic_calendar.config import EconomicCalendarConfig
from economic_calendar.models import CalendarEvent
from economic_calendar.mt5_file_provider import Mt5CalendarFileProvider
from economic_calendar.risk_filter import evaluate_event_risk
from economic_calendar.storage import atomic_write_json
from economic_calendar.symbol_mapping import get_symbol_exposure


NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


def config(**overrides):
    defaults = dict(enabled=True, export_path=None, max_data_age_minutes=15, high_before_minutes=30, high_after_minutes=30, moderate_block_enabled=False, moderate_before_minutes=15, moderate_after_minutes=15, fail_mode="OPEN", dry_run=False, lookahead_hours=24, symbol_exposure_map={}, fundamental_analysis_enabled=True, ollama_model="test", ollama_timeout_seconds=1, ollama_max_retries=1, sentiment_max_age_hours=24, sentiment_decay_hours=12)
    defaults.update(overrides)
    return EconomicCalendarConfig(**defaults)


def event(currency="USD", importance="HIGH", minutes=30, actual=None, trusted=True):
    return CalendarEvent("event", "value", "1", currency, currency, "CPI", importance, "2026-01-05T12:30:00", NOW + timedelta(minutes=minutes), trusted, actual, 2.0, 1.0, None, "RELEASED" if actual is not None else "SCHEDULED", "MT5_ECONOMIC_CALENDAR", NOW)


def test_forex_suffix_and_asset_exposure_mapping():
    assert get_symbol_exposure("EURUSD_ecn").currencies == ("EUR", "USD")
    assert get_symbol_exposure("XAUUSD").currencies == ("USD",)
    assert get_symbol_exposure("BTCUSD").assets == ("CRYPTO",)
    assert get_symbol_exposure("US500").status == "UNMAPPED"


def test_high_event_blocks_at_window_boundary_for_quote_and_base():
    result = evaluate_event_risk("EURUSD_ecn", NOW, [event("USD", minutes=30)], config())
    assert result.would_block and not result.allow_new_trade and result.risk_level == "HIGH"
    assert evaluate_event_risk("EURUSD", NOW, [event("EUR", minutes=-30)], config()).would_block


def test_moderate_warns_and_untrusted_or_unmapped_never_blocks():
    moderate = evaluate_event_risk("GBPJPY", NOW, [event("JPY", "MODERATE", 5)], config())
    assert moderate.allow_new_trade and moderate.risk_level == "MEDIUM"
    assert not evaluate_event_risk("EURUSD", NOW, [event("USD", trusted=False)], config()).would_block
    assert evaluate_event_risk("US500", NOW, [event("USD", minutes=5)], config()).allow_new_trade


def test_fail_modes_and_dry_run():
    assert evaluate_event_risk("EURUSD", NOW, [], config(fail_mode="OPEN"), "UNAVAILABLE").allow_new_trade
    assert not evaluate_event_risk("EURUSD", NOW, [], config(fail_mode="CLOSED"), "STALE").allow_new_trade
    dry_run = evaluate_event_risk("EURUSD", NOW, [event(minutes=5)], config(dry_run=True))
    assert dry_run.allow_new_trade and dry_run.would_block


def test_bridge_file_parsing_keeps_zero_actual_and_rejects_stale(tmp_path):
    path = tmp_path / "bridge.json"
    payload = {"schema_version": 1, "generated_at_utc": NOW.isoformat(), "events": [{"event_id": "1", "value_id": "2", "country_code": "US", "currency": "USD", "event_name": "CPI", "importance": "HIGH", "event_time_server": "2026-01-05T12:00:00", "event_time_utc": NOW.isoformat(), "time_is_trusted": True, "actual": 0, "forecast": 1.0, "previous": 2.0}]}
    atomic_write_json(path, payload)
    events, status = Mt5CalendarFileProvider(config(export_path=path)).load(NOW)
    assert status == "AVAILABLE" and events[0].actual == 0.0
    _, stale = Mt5CalendarFileProvider(config(export_path=path)).load(NOW + timedelta(minutes=16))
    assert stale == "STALE"


def test_atomic_json_write_is_always_parseable(tmp_path):
    path = tmp_path / "result.json"
    atomic_write_json(path, {"value": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"value": 1}