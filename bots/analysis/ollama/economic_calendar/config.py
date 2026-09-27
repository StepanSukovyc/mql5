from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value >= minimum else default
    except ValueError:
        return default


@dataclass(frozen=True)
class EconomicCalendarConfig:
    enabled: bool
    export_path: Path | None
    max_data_age_minutes: int
    high_before_minutes: int
    high_after_minutes: int
    moderate_block_enabled: bool
    moderate_before_minutes: int
    moderate_after_minutes: int
    fail_mode: str
    dry_run: bool
    lookahead_hours: int
    symbol_exposure_map: dict[str, list[str]]
    fundamental_analysis_enabled: bool
    ollama_model: str
    ollama_timeout_seconds: int
    ollama_max_retries: int
    sentiment_max_age_hours: int
    sentiment_decay_hours: int

    @classmethod
    def from_env(cls) -> "EconomicCalendarConfig":
        raw_map = os.getenv("ECONOMIC_SYMBOL_EXPOSURE_MAP", "").strip()
        try:
            exposure_map = json.loads(raw_map) if raw_map else {}
            if not isinstance(exposure_map, dict) or not all(isinstance(v, list) for v in exposure_map.values()):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            exposure_map = {}
        fail_mode = os.getenv("ECONOMIC_FAIL_MODE", "OPEN").strip().upper()
        if fail_mode not in {"OPEN", "CLOSED"}:
            fail_mode = "OPEN"
        export_raw = os.getenv("ECONOMIC_CALENDAR_EXPORT_PATH", "").strip()
        return cls(
            enabled=_bool("ECONOMIC_CALENDAR_ENABLED", True),
            export_path=Path(export_raw) if export_raw else None,
            max_data_age_minutes=_int("ECONOMIC_CALENDAR_MAX_DATA_AGE_MINUTES", 15, 1),
            high_before_minutes=_int("ECONOMIC_HIGH_BLOCK_BEFORE_MINUTES", 30),
            high_after_minutes=_int("ECONOMIC_HIGH_BLOCK_AFTER_MINUTES", 30),
            moderate_block_enabled=_bool("ECONOMIC_MODERATE_BLOCK_ENABLED", False),
            moderate_before_minutes=_int("ECONOMIC_MODERATE_BLOCK_BEFORE_MINUTES", 15),
            moderate_after_minutes=_int("ECONOMIC_MODERATE_BLOCK_AFTER_MINUTES", 15),
            fail_mode=fail_mode,
            dry_run=_bool("ECONOMIC_FILTER_DRY_RUN", True),
            lookahead_hours=_int("ECONOMIC_CALENDAR_LOOKAHEAD_HOURS", 24),
            symbol_exposure_map={str(k).upper(): [str(x).upper() for x in v] for k, v in exposure_map.items()},
            fundamental_analysis_enabled=_bool("ECONOMIC_FUNDAMENTAL_ANALYSIS_ENABLED", True),
            ollama_model=os.getenv("ECONOMIC_OLLAMA_MODEL", "").strip() or os.getenv("OLLAMA_MODEL", ""),
            ollama_timeout_seconds=_int("ECONOMIC_OLLAMA_TIMEOUT_SECONDS", 120, 1),
            ollama_max_retries=_int("ECONOMIC_OLLAMA_MAX_RETRIES", 1),
            sentiment_max_age_hours=_int("ECONOMIC_SENTIMENT_MAX_AGE_HOURS", 24, 1),
            sentiment_decay_hours=_int("ECONOMIC_SENTIMENT_DECAY_HOURS", 12, 1),
        )