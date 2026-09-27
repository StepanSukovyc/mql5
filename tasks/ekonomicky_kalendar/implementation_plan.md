# Economic Calendar Implementation Plan

## Verified starting point

- `MetaTrader5` Python package 5.0.5640 exposes no `calendar*` API, so a read-only MQL5 exporter is required.
- `logika.py` orchestrates prediction processing, `ollama_service.py` builds local-AI predictions, `gemini_decision.py` prepares Gemini context, and `trade_execution.py` owns new-entry `order_send` calls.
- The project already uses UTC-aware datetimes in its scheduler and brokers use symbols with the configurable `_ecn` suffix.

## Files to change

- `bots/analysis/ollama/.env.example`: calendar, bridge, filter, sentiment, and dry-run settings.
- `bots/analysis/ollama/ollama_service.py`: optional, bounded `fundamental_context` and backwards-compatible output fields.
- `bots/analysis/ollama/gemini_decision.py`: include normalized fundamental context with final candidates.
- `bots/analysis/ollama/trading_logic.py` and/or `logika.py`: filter blocked new-entry candidates before Gemini selection.
- `bots/analysis/ollama/trade_execution.py`: immediately revalidate a new entry before `order_send`; position-close functions are intentionally excluded.
- `bots/analysis/ollama/README.md`: deployment, operation, states, fallback and test documentation.

## New files

- `bots/analysis/ollama/economic_calendar/`: models, configuration, file provider, symbol mapping, deterministic filter, Ollama analysis, aggregation, storage/audit, and integration facade.
- `bots/analysis/ollama/tests/test_economic_calendar.py` plus fixture bridge JSON.
- `mql/experts/mt5_economic_calendar_exporter.mq5`: read-only MT5 Economic Calendar exporter.

## Data flow and safeguards

1. The MQL5 exporter reads calendar values and metadata, records server timestamps plus a measured server-to-UTC offset, and atomically writes JSON.
2. Python validates and normalizes that file into timezone-aware UTC events. An untrusted server time cannot block a trade.
3. Before candidates reach AI, the deterministic filter maps the symbol safely and evaluates only relevant event windows. It defaults to fail-open and dry-run.
4. Only released events with a usable `actual` are sent individually to Ollama. Strict JSON validation, deduplication and bounded retry ensure unavailable Ollama cannot affect event timing safety.
5. Currency and pair sentiment are optional context for Ollama and Gemini; missing data remains `NO_DATA`, never neutral sentiment.
6. The execution boundary rechecks the deterministic filter before sending a new market order. It cannot affect position closing, profit cleanup, swap rollover cleanup, or hybrid loss exit.

## Fallbacks and tests

- Calendar disabled preserves the previous path.
- Invalid, stale, or unavailable data uses the configured fail mode; the default is `OPEN`.
- Corrupt bridge JSON retains only a recent valid snapshot.
- Tests use JSON fixtures and mocked HTTP/MT5 APIs for mapping, timing boundaries, stale/fail modes, atomic storage, deduplication, Ollama validation, sentiment, dry-run, and execution-time revalidation.