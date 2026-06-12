# Changelog

## 2026-06-12 (Secondary Strategies Use Full Non-Crypto Universe When Whitelist Is Empty)

- **Changed Empty Secondary Whitelists To Mean Full Non-Crypto Coverage**
  - `parallel_strategy_mean_reversion.py` and `reversal_pattern_strategy.py` no longer interpret an empty whitelist as index-only coverage
  - When `PARALLEL_SYMBOL_WHITELIST` or `REVERSAL_SYMBOL_WHITELIST` is empty, the strategy may now evaluate the full non-crypto universe: Forex, indices, and other allowed CFD instruments
  - When a whitelist is explicitly configured, the previous restrictive behavior remains unchanged and only whitelist matches are eligible

- **Centralized Secondary Symbol Eligibility Logic**
  - `instrument_utils.py` now exposes a shared helper that decides whether a symbol belongs to the allowed secondary-strategy universe
  - The helper keeps crypto excluded from parallel and reversal fallback when no explicit whitelist is provided

- **Updated Tests And Operator Documentation**
  - Added focused tests for Forex, index, CFD, and crypto behavior of the empty-whitelist secondary universe
  - `TRADING_LOGIC.md` now documents the exact meaning of empty vs explicit secondary whitelists

## 2026-06-05 (Third Reversal-Pattern Fallback Strategy)

- **Added A Third Entry Strategy Based On Reversal Price Formations**
  - New module `reversal_pattern_strategy.py` validates bullish and bearish reversal setups from live candle structure instead of relying only on oscillators
  - The strategy looks for engulfing or pin-bar style rejection at Bollinger extremes, adds VWAP location checks, and filters the setup with ADX, spread, RSI, and ATR-size constraints
  - The strategy is opt-in through `REVERSAL_STRATEGY_ENABLED` and keeps its own `strategy_id`, `magic`, session window, margin gate, position cap, cooldown, and risk profile

- **Extended Final Decision Orchestration To Run A Third Fallback Layer**
  - `final_decision.py` now executes strategies in the order `primary -> parallel -> reversal` using the same local risk, cooldown, audit, and execution pipeline
  - Secondary strategy status logging is now generic, so the runtime writes both `parallel_strategy_status.csv` and `reversal_strategy_status.csv`
  - `strategy_context.py` and `account_monitor.py` now include the reversal profile when resolving known strategy ownership and the default wake-up margin threshold

- **Documented And Enabled Runtime Configuration For Reversal Strategy**
  - `.env.example` now contains the full `REVERSAL_*` configuration namespace and a short operator description of the setup
  - Local `.env` now enables the strategy with a conservative FX-major whitelist and explicit thresholds for ADX, ATR range, confirmation close, and pin-bar shape
  - `TRADING_LOGIC.md` now describes when the reversal profile runs, which filters it uses, and where to look for its audit/status output

## 2026-05-22 (Rolling 30-Day Loss Cleanup Advisory Strategy)

- **Added `monthly_loss_cleanup_strategy.py` — advisory-only, never closes positions**
  - Computes realized profit over a rolling 30-calendar-day window from MT5 deal history
  - Active trading days = unique UTC calendar dates with at least one closing deal in the window; a configurable floor (`MONTHLY_LOSS_CLEANUP_MIN_ACTIVE_DAYS`, default `15`) prevents the target from being too low during bot downtime
  - `target = effective_active_days × MONTHLY_LOSS_CLEANUP_DAILY_TARGET_USD` (default `50 USD/day`)
  - `surplus = realized_profit_30d − target`; if surplus ≤ 0 no recommendations are generated
  - When surplus > 0 the strategy finds all open positions older than `MONTHLY_LOSS_CLEANUP_MIN_POSITION_AGE_DAYS` (default `30`) that are currently losing and performs a greedy selection from largest to smallest loss until the surplus is consumed
  - All candidates and the recommended subset are written to `trade_logs/monthly_loss_cleanup_recommendations.json`; the file is overwritten on every daily run
  - Runs once per Prague day after a configurable trigger time (`MONTHLY_LOSS_CLEANUP_HOUR` / `MONTHLY_LOSS_CLEANUP_MINUTE`, default `13:00`); last-run day key is persisted in `trade_logs/monthly_loss_cleanup_state.json`
  - Skipped during the swap rollover block window
  - Wired into `account_monitor.py` → `run_position_management_monitor` alongside the existing daily loss-cleanup strategy

## 2026-05-22 (Gemini Candidate Cap + Explicit Local Fallback Audit)

- **Added Configurable Gemini Advisory Candidate Cap**
  - `final_decision.py` now limits advisory candidates with `GEMINI_ADVISORY_MAX_CANDIDATES`
  - The default cap is `3`, so the runtime tries at most three Gemini symbol/action candidates before continuing with the local deterministic ranking queue
  - The active sample and local runtime configuration now document `GEMINI_ADVISORY_MAX_CANDIDATES=3`

- **Made Gemini-To-Local Fallback Visible In Audit Logs**
  - `trade_decision_audit.csv` now records an explicit `queue_transition` row with reason `gemini_candidates_exhausted_local_fallback`
  - The transition row includes how many Gemini candidates were exhausted before the local queue took over
  - Existing audit and snapshot rows still keep `candidate_queue` and `candidate_rank`, so the exact order remains readable alongside the transition marker

## 2026-05-22 (Deterministic Strategy Layers + Session-Gated Parallel Fallback)

- **Replaced Gemini-Centric Execution With Local Strategy Layers**
  - `final_decision.py` now uses Gemini only as an advisory ranking input for symbol and direction selection
  - Final `lot_size` and `take_profit` are now resolved locally through `risk_engine.py` using an internal synthetic stop and configurable `R` multiple
  - `signal_rules.py` became the deterministic primary trend-following gate, so a trade is opened only when local market structure confirms the candidate

- **Added Persistent AI Advisory Cache And Local Retry Control**
  - New module `ai_advisory_state.py` stores a decision signature, a 15-minute Gemini decision cache, and a 30-minute rejection cooldown in `trade_logs/gemini_advisory_state.json`
  - Repeated retries no longer re-query Gemini for the same materially unchanged state; the runtime now retries locally against the remaining candidate queue

- **Activated Parallel Mean-Reversion Runtime Fallback**
  - `parallel_strategy_mean_reversion.py` now validates a real parallel setup using Bollinger Bands, RSI2, VWAP distance, ADX regime filter, spread limits, and symbol whitelist
  - `final_decision.py` now falls back to the parallel strategy only after the primary strategy has no executable candidate and only when the parallel profile is allowed to activate

- **Added Explicit Session Guards For Strategy Profiles**
  - `strategy_context.py` now carries per-strategy UTC session start, session end, and Friday cutoff configuration
  - Primary strategy uses `PRIMARY_SESSION_*` and `PRIMARY_FRIDAY_CUTOFF_HOUR_UTC`
  - Parallel strategy uses `PARALLEL_SESSION_*` and `PARALLEL_FRIDAY_CUTOFF_HOUR_UTC`
  - `final_decision.py` skips strategy execution outside the configured window instead of letting the profile trade all day

- **Expanded Market Data For The Parallel Strategy**
  - `market_data.py` now exposes `rsi2`, Bollinger Bands, VWAP, EMA20/50/200, ATR14, and ADX14 in the normalized payload
  - Longer historical seed loading remains in place so long-window indicators such as EMA200 are stable

- **Moved Tests Into A Dedicated Tests Folder And Updated Coverage**
  - All Python tests for the Ollama/Gemini trading stack now live under `bots/analysis/ollama/tests/`
  - Coverage now includes the strategy layers, local synthetic risk resolution, advisory cache reuse, local candidate fallback, and parallel session windows

## 2026-05-21 (Restore Original Trading Flow + Keep Profit Protection)

- **Reverted The Same-Day Strategy Refactor Back To The Original Single-Flow Trading Logic**
  - `logika.py`, `trading_logic.py`, `final_decision.py`, `trade_execution.py` and related helper modules were restored to the pre-refactor behavior
  - The temporary parallel strategy/profile/news-filter architecture was removed from the active trading path
  - Current runtime is again the original single Gemini trading flow with account-monitor-driven management layers

- **Kept Profit Protection As The Active Profit-Exit Layer**
  - `account_monitor.py` now runs `profit_protection_strategy.py` instead of the older minute `profit_cleanup_strategy.py`
  - Profit protection tracks `max_net_profit` per open position in `trade_logs/profit_protection_state.json` using keys like `gemini_primary:<ticket>`
  - A profitable position is closed only after retracement from its tracked peak or after configured stale/maximum-hold limits, so the old direct profit cleanup no longer drives active profit exits

- **Ignored Generated Python Analysis Output In Git**
  - Root `.gitignore` now ignores the full `analysis/python/` tree
  - Generated runtime files such as `analysis/python/trade_logs/profit_protection_state.json` no longer appear as repository changes

## 2026-05-07 (Economy Mode For MT5 Data Refresh Without Ollama Precomputation)

- **Added Default Economy Mode For The Main Trading Loop**
  - `logika.py` now supports `ECONOMY_MODE_ENABLED` and `ECONOMY_MODE_INTERVAL_SECONDS`
  - When economy mode is enabled, the main process keeps refreshing MT5 market data in the background every configured interval, default 300 seconds
  - The parallel `Ollama Service` thread is no longer started in this mode, reducing local inference load while preserving fresh source data for trading

- **Forced Trade-Time Gemini Analysis In Economy Mode**
  - `trading_logic.py` now skips reuse of prepared Ollama predictions when `ECONOMY_MODE_ENABLED=true`
  - At trade time, the bot goes directly to Gemini even if `OLLAMA_FALLBACK_TO_GEMINI` would otherwise be disabled
  - This keeps all downstream trading behavior unchanged while replacing only the precomputation path

## 2026-05-06 (Immediate Legacy Gemini Fallback After First Vertex Failure)

- **Stopped Repeating Vertex Attempts When Legacy Gemini API Is Configured**
  - `gemini_vertex.py` now treats Vertex as a single first-choice attempt when both `GEMINI_API_KEY` and `GEMINI_URL` are configured
  - If that first Vertex request fails, the helper now skips additional Vertex retries, skips extra Vertex fallback models, and skips the direct Vertex REST fallback
  - The request switches immediately to the older Generative Language API flow and logs that handoff as `transport="legacy-gemini-api"`

- **Kept Original Vertex Retry Path Only For Vertex-Only Environments**
  - When no legacy Gemini API configuration is available, the previous behavior remains in place: SDK attempt, optional Vertex REST fallback for transport-level failures, and configured Vertex model retries/fallbacks
  - This keeps the Vertex-only runtime unchanged while reducing noise and latency in environments where the API-key fallback is intentionally enabled

## 2026-05-06 (Vertex REST Fallback For Gemini Timeouts)

- **Added Direct Vertex REST Fallback When Python SDK Transport Fails**
  - `gemini_vertex.py` now first tries the existing `google-genai` SDK path and automatically falls back to a direct Vertex `generateContent` REST call when the SDK fails with transport-level timeout errors without an HTTP status
  - The REST fallback authenticates with the configured service-account JSON and validates the same structured JSON schema as the SDK path, so final decision and prediction parsing stay unchanged
  - Gemini logs now distinguish successful REST fallback responses with `transport="vertex-rest-fallback"`, making it visible when the SDK path timed out but the underlying Vertex API call still succeeded

- **Restored Legacy Gemini API-Key Fallback After Vertex Exhaustion**
  - When all configured Vertex model attempts fail, `gemini_vertex.py` now tries the older Generative Language API flow using `GEMINI_API_KEY` and `GEMINI_URL`
  - The legacy fallback reuses the same prompt and the same structured JSON validation helpers, so predictions and final decisions remain compatible with the current pipeline
  - Runtime logs now expose the final fallback transport as `transport="legacy-gemini-api"`

- **Made Google Auth An Explicit Runtime Dependency**
  - Added `google-auth` to `requirements.txt` so the REST fallback does not rely on a transitive dependency from another package
  - This documents that the trading bot now supports two Vertex transports in the same runtime: Python SDK first, direct REST fallback second

## 2026-04-21 (Ollama Context And Latency Stabilization)

- **Made Ollama Context Window Configurable Per Request**
  - `ollama_service.py` now sends `options.num_ctx` with each Ollama API request instead of relying on the server default context size
  - Added `OLLAMA_NUM_CTX` to `.env` and `.env.example` so larger prompts can be enabled without editing code
  - Prompt-size fallback now compares the full prompt against a budget derived from the configured context window before switching to compact mode

- **Added Configurable Ollama Request Timeout**
  - Added `OLLAMA_TIMEOUT_SECONDS` to `.env` and `.env.example`
  - `ollama_service.py` no longer uses a hard-coded 180 second HTTP timeout and can wait longer for larger CPU-bound requests

- **Adjusted Default Runtime Settings For Better CPU Stability**
  - Active Ollama configuration now uses `OLLAMA_NUM_CTX=16384`, `OLLAMA_TIMEOUT_SECONDS=600`, and `OLLAMA_COMPACT_PROMPT=true`
  - This reduces KV-cache pressure and shortens inference time while still allowing larger prompts than the previous 4096-token default
  - Recent Ollama server logs now show successful `/api/generate` responses after these runtime adjustments

## 2026-04-15 (ECN Suffix Override For CFD Detection)

- **Made `_ecn` Symbols Always Valid Even With CFD Blacklist Enabled**
  - `instrument_utils.py` now treats symbols ending with `_ecn` as always valid before any CFD classification is applied
  - This prevents forex pairs such as `EURUSD_ecn` from being filtered out by the `__CFD__` blacklist token even when MT5 metadata resembles CFD instruments
  - Added `MT5_ALWAYS_VALID_SYMBOL_SUFFIXES` with default `_ecn` for future overrides
  - Added unit tests to verify that `_ecn` symbols stay allowed while non-`_ecn` CFD symbols can still be blocked

## 2026-04-15 (CFD Blacklist Token)

- **Added A Dedicated Blacklist Token For All CFD Instruments**
  - `market_data.py` now recognizes `__CFD__` inside `MT5_SYMBOL_BLACKLIST`
  - When present, all symbols classified as CFD are filtered out before prediction generation and final trade selection
  - Updated `.env`, `.env.example`, and `README.md` to document the new blacklist option
  - Local `.env` now enables `MT5_SYMBOL_BLACKLIST=__CFD__`, so CFD instruments are currently excluded from the trading universe

## 2026-04-14 (Fee-Aware CFD Take-Profit Safeguards)

- **Protected CFD Trades From Uneconomic Gemini Take-Profit Targets**
  - `final_decision.py` now detects CFD instruments separately from crypto and standard symbols
  - In full Gemini TP mode, CFD `take_profit` is no longer accepted blindly; the target is checked with MT5 profit estimation against modeled per-trade fees
  - If Gemini `take_profit` is too close to cover fees and the minimum net-profit target, the system now moves `TP` to the nearest fee-safe level instead of opening a likely uneconomic trade
  - If no fee-safe `TP` can be found within the configured distance, the trade is executed without `take_profit` instead of keeping a loss-making target

- **Added CFD Runtime Configuration**
  - Added `MT5_CFD_SYMBOL_PATTERNS` for optional explicit CFD symbol classification
  - Added `MT5_CFD_TP_MAX_DISTANCE_PERCENT` with default `3.0`
  - Added `MT5_CFD_MIN_NET_PROFIT_USD` with default `0.10`
  - Added `MT5_CFD_ALLOW_FULL_TP_MODE` with default `true`

- **Added MT5 Profit-Based CFD TP Validation Tests**
  - `test_final_decision.py` now verifies that a CFD `take_profit` is either raised to a fee-safe level or disabled when no safe target exists
  - Added `estimate_order_profit()` helper to `mt5_symbols.py` for MT5-native TP profitability checks

## 2026-04-13 (Configurable Ollama-Only Prediction Mode)

- **Added Env Switch For Gemini Fallback After Stale Ollama Predictions**
  - Added `OLLAMA_FALLBACK_TO_GEMINI` to `.env` and `.env.example` with default `false`
  - When `OLLAMA_FALLBACK_TO_GEMINI=false`, instruments without a fresh Ollama prediction are ignored instead of falling back to `ask_gemini_prediction`
  - If no instrument has a fresh Ollama prediction, the trading cycle now completes without creating predictions and without opening a trade
  - Updated README and TRADING_LOGIC documentation to describe the new behavior

- **Limited Gemini Fallback To A Configurable Number Of Instruments Per Cycle**
  - Added `OLLAMA_GEMINI_FALLBACK_MAX_INSTRUMENTS` to `.env` and `.env.example` with default `60`
  - When `OLLAMA_FALLBACK_TO_GEMINI=true`, the main trading flow now uses Gemini fallback only for the first configured number of instruments without a fresh Ollama prediction
  - Remaining instruments without a usable Ollama prediction are skipped for the current cycle instead of being sent to Gemini

- **Parallelized Gemini Fallback With A Configurable Concurrency Limit**
  - Added `GEMINI_FALLBACK_MAX_PARALLEL_REQUESTS` to `.env` and `.env.example` with default `3`
  - Gemini fallback requests can now run concurrently instead of strictly one-by-one
  - The parallelism is bounded so the cycle speeds up without removing control over API pressure

- **Parallelized Ollama Prediction Requests With Configurable Throttling**
  - Added `OLLAMA_MAX_PARALLEL_REQUESTS` and `OLLAMA_REQUEST_DELAY_SECONDS` to `.env` and `.env.example`
  - The Ollama service can now submit multiple symbol prediction requests concurrently instead of processing every instrument strictly one-by-one
  - Concurrency stays bounded and the optional delay allows smoothing request bursts when needed

- **Added Configurable Compact Ollama Prompt Mode**
  - Added `OLLAMA_COMPACT_PROMPT` to `.env` and `.env.example` with default `false`
  - When enabled, Ollama receives a reduced summary payload instead of the full raw market-data JSON
  - When disabled, the existing full-prompt behavior remains unchanged

## 2026-04-13 (Final Decision Retry After Failed Trade)

- **Retried Final Decision With Symbol Exclusion After Trade Failure**
  - `final_decision.py` now excludes the current symbol and retries Gemini final selection when symbol validation fails, trade parameters are invalid, or MT5 trade execution fails
  - This prevents the whole decision phase from stopping on a single bad symbol such as an invalid Gemini `take_profit`
  - Added `test_final_decision.py` to verify that a failed first trade leads to a retry with a different prediction

## 2026-04-10 (Fixed Swap Window + Rollover Audit Visibility)

- **Interpreted Fixed Swap Window In Prague Time Instead Of UTC**
  - The manual swap block interval from `.env` is now evaluated in `Europe/Prague`, so `22:30-23:30` matches Prague local time instead of UTC
  - This aligns the trading lock and rollover cleanup with the intended market blackout window and prevents trades from slipping through two hours late during DST

- **Forced Swap Block Window To Always Use The Configured Manual Interval**
  - `swap_rollover.py` now always builds the active trading lock and rollover cleanup window from `.env` via `SWAP_BLOCK_START_*` and `SWAP_BLOCK_END_*`
  - Broker-derived rollover timestamps are no longer used for production window selection, preventing the cleanup from shifting away from the expected `22:30-23:30` Prague-time interval

- **Added Audit Rows For Skip And No-Candidate Rollover Passes**
  - `swap_rollover_cleanup_strategy.py` now writes an audit row even when the strategy is outside the fixed window or when no eligible profitable position is found inside the window
  - This makes it possible to distinguish between “strategy did not run in the active window” and “strategy ran but found nothing to close” directly from `trade_logs/swap_rollover_cleanup.csv`

## 2026-04-09 (Broker Rollover Detection Fallback)

- **Added Manual Block-Window Fallback When Broker Rollover Detection Is Unavailable**
  - `swap_rollover.py` now first tries to infer rollover from MT5 broker history and also recognizes closing deals carrying non-zero `swap`
  - If broker history still does not expose a usable rollover timestamp, the trading lock and rollover cleanup now fall back to a manual `.env` interval defined by `SWAP_BLOCK_START_*` and `SWAP_BLOCK_END_*`
  - Current fallback configuration is `22:30-23:30` and is used consistently by both the main trading lock and the rollover cleanup strategy

## 2026-04-08 (Configurable Loss Cleanup Buffer)

- **Made Loss Cleanup Safety Buffer Configurable And Set It To 2%**
  - Added `LOSS_CLEANUP_BALANCE_BUFFER_PERCENT` to `.env` and `.env.example`
  - Loss cleanup no longer uses a hardcoded 1% reserve; it now reads the configured percentage from environment
  - Current configuration is set to `2`, so cleanup leaves a 2% raw-balance safety buffer before closing a losing position

## 2026-04-08 (Broker Swap Window Blocking)

- **Moved Trading Block Window To Broker-Derived Swap Rollover Time**
  - Added `swap_rollover.py` to detect rollover time from MT5 history deals and build the trading block window from broker timestamps when available
  - Trading lock now prefers broker-derived rollover timing and otherwise uses the configured manual fallback interval from `.env`
  - Loss cleanup skips the same broker-derived rollover window for consistent behavior across strategies

- **Added Separate Rollover Cleanup Alongside Original Minute Profit Cleanup**
  - Restored the original minute profit cleanup logic and limited it to run only outside the broker-derived swap block window
  - Added `swap_rollover_cleanup_strategy.py` as a separate strategy that runs only inside the broker-derived swap block window
  - The new rollover cleanup closes profitable positions with net profit at least `0.10 USD` to avoid carrying positive trades through swap posting

## 2026-04-08 (Daily Loss Cleanup Scheduling)

- **Changed Loss Cleanup To Run Once Daily Using Previous Prague-Day Profit**
  - Loss cleanup now evaluates only once per Prague day after `LOSS_CLEANUP_STRATEGY_HOUR:LOSS_CLEANUP_STRATEGY_MINUTE` instead of every hour
  - Profit budget now uses the previous completed Prague-day realized result while keeping the existing candidate selection and open P/L safety rules
  - Added persistent state file `trade_logs/loss_cleanup_state.json` so the strategy cannot execute more than once per Prague day after a process restart

## 2026-04-08 (Loss Cleanup Diagnostics Clarification)

- **Separated Actual MT5 Fee From Modeled Fee In Daily Deal Snapshot**
  - `loss_cleanup_daily_deals.csv` now stores `actual_fee` from MT5 separately from the modeled per-volume fee used for candidate scoring
  - Added per-deal `realized_component` so the reported `daily_realized_profit` can be traced directly from the snapshot rows
  - Console and docs now state explicitly that `daily_realized_profit` uses `profit + swap + commission + actual deal.fee`

## 2026-04-08 (Loss Cleanup Floating P/L Guard)

- **Blocked Loss Cleanup When Open P/L Is Already Negative**
  - Loss cleanup now subtracts the current negative open P/L (`equity - raw_balance`) from the daily realized profit budget before computing `Z`
  - Strategy no longer closes an old losing position purely because today's realized deals are positive while the account is already negative on open positions
  - Console output and audit log now show `current_open_profit` and the resulting effective profit budget for transparency

## 2026-04-08 (Loss Cleanup Safety Guard)

- **Hardened Hourly Loss Cleanup Safety Check**
  - Loss cleanup now computes `Z` from daily realized profit including `profit`, `swap`, `commission`, and `fee`
  - Strategy keeps logging `daily_clean_profit` separately for diagnostics against MT5 history views
  - Added explicit guard that rejects any candidate which would push daily realized profit below `0.00` after close
  - Audit log `trade_logs/loss_cleanup.csv` now also stores `daily_realized_profit`

## 2026-04-01 (Minute Profit Cleanup Strategy)

- **Added Minute Profit Cleanup Strategy**
  - Added `profit_cleanup_strategy.py` for minute-by-minute review of open profitable positions
  - Strategy computes `VOLUME = ((int)(B / 500) + 1) * 0.01` from the current account balance `B`
  - For each open position it computes `ZISK = profit + swap - fee`, where fee is `0.10 USD` per `0.01` lot
  - Target profit threshold is `PCZ = (0.01 * L / VOLUME) * B` with a hard minimum of `0.005`
  - All currently eligible positions are closed in a single run when `ZISK > PCZ`

- **Added Runtime Controls And Observability**
  - Added `PROFIT_CLEANUP_STRATEGY_ENABLED` and `PROFIT_CLEANUP_STRATEGY_DRY_RUN` to `.env` and `.env.example`
  - Default dry-run is `true` to allow safe validation before enabling live closes
  - Added audit log `trade_logs/profit_cleanup.csv`

- **Added Validation Script**
  - Added `verify_profit_cleanup_strategy.py` for quick local verification of `VOLUME`, `ZISK`, and `PCZ`
  - Validation script reuses the same calculation helper as the live strategy

- **Added Calculation Unit Tests**
  - Added `test_profit_cleanup_strategy.py` using stdlib `unittest`
  - Tests cover the user example, an eligible scenario, `PCZ` minimum floor, and swap/fee impact

- **Integrated With Account Monitor**
  - Account monitor now evaluates the minute profit cleanup before the hourly loss cleanup

## 2026-03-30 (Hourly Loss Cleanup Strategy + Dry Run)

- **Added Hourly Loss Cleanup Strategy**
  - Added `loss_cleanup_strategy.py` for an hourly review of stale losing positions
  - Strategy runs once per hour at `LOSS_CLEANUP_STRATEGY_MINUTE` when `LOSS_CLEANUP_STRATEGY_ENABLED=true`
  - Computes daily clean profit from closed MT5 deals without swap and fee deductions
  - Subtracts 1% of current account balance to derive cleanup budget `Z`
  - Scans open positions older than 7 days and selects the largest losing candidate whose effective loss stays below `Z`
  - Effective loss includes current position profit, swap, and synthetic fee `0.10 USD` per `0.01` lot

- **Added Safe Position Close Helper**
  - `trade_execution.py` now exposes `close_position_by_ticket()` for closing existing MT5 positions via opposite market order

- **Added Runtime Safety Controls**
  - Added `LOSS_CLEANUP_STRATEGY_DRY_RUN` to `.env` and `.env.example`
  - Default is `true`, so the strategy only logs which position would be closed and does not send an MT5 close order
  - Cleanup strategy is also blocked during restricted trading window `23:00-23:30 CET/CEST`

- **Added Observability**
  - Cleanup actions are logged to `trade_logs/loss_cleanup.csv`
  - Audit log includes timestamp, dry-run mode, daily clean profit, balance buffer, `Z`, candidate details, and result message
  - Added `trade_logs/loss_cleanup_daily_deals.csv` with raw MT5 deal snapshots returned by `history_deals_get()` for discrepancy diagnostics

- **Adjusted Daily Profit Source**
  - Loss cleanup now derives daily clean profit from today's closed positions using `history_orders_get()` plus `position_id` matching
  - This is intended to better align the strategy with the position-style history shown in the MT5 mobile app

## 2026-03-27 (Prediction Lot Sizing + Trade Log Source)

- **Removed Remaining Balance-Based Lot Sizing**
  - Final trade execution now always uses `lot_size` from the final Gemini prediction
  - Removed the no-longer-used `trade_risk.py` helper and current-state references to local balance-based sizing
  - Updated docs to reflect that execution mode now changes only `take_profit`, not the lot source

- **Added Explicit `lot_source` Trade Logging**
  - `trade_logs/trades.csv` now includes `lot_source`
  - Current executions write `gemini_prediction` so the origin of volume is visible in audit logs
  - Legacy CSV files are migrated automatically on next write by inserting `legacy_unknown` for older rows

## 2026-03-27 (Configurable Balance Cap + Validation Script)

- **Made Strategy Balance Cap Configurable via `.env`**
  - Added `TRADING_ACCOUNT_BALANCE_CAP` environment setting (default `5000`)
  - Strategy balance is now capped dynamically from environment instead of hardcoded constant
  - Effective free margin now subtracts the full reserve above the configured cap
  - Preserves excess account funds outside strategy sizing and margin decisions

- **Updated Execution Observability**
  - Account state now exposes both effective and raw balance/free margin values
  - Final decision and monitor logs now show reserve information when cap is active
  - `trading_logic.py` now logs the active configured strategy balance cap at startup

- **Added Validation Script**
  - Added `verify_account_balance_cap.py` for quick local verification of capped balance and free margin scenarios

- **Documentation Updated**
  - Updated `TRADING_LOGIC.md` with `.env` configuration and reserve examples for capped balance behavior

## 2026-03-27 (Modular Refactor + Startup Fix)

- **Refactored Trading Stack into Shared Helper Modules**
  - Extracted MT5 account helpers to `account_state.py`
  - Extracted MT5 connection lifecycle to `mt5_connection.py`
  - Extracted symbol/tick helpers to `mt5_symbols.py`
  - Extracted open position serialization to `mt5_positions.py`
  - Extracted trading validation to `trading_validation.py`
  - Extracted trade execution and CSV logging to `trade_execution.py`
  - Extracted trade history reading to `trade_history.py`
  - Extracted Gemini config loading to `gemini_config.py`
  - Extracted Gemini final-decision helpers to `gemini_decision.py`

- **Simplified Orchestration Modules**
  - `final_decision.py` is now primarily orchestration of the final decision workflow
  - Added smaller internal helpers for parsing Gemini decision JSON, resolving trade parameters, saving final decision files, and handling symbol retry/exclusion flow
  - `trading_logic.py` now reuses shared Gemini response cleaning and shared Gemini config loading instead of maintaining duplicate local helpers

- **Documentation Updated**
  - Updated `TRADING_LOGIC.md` to reflect the post-refactor architecture
  - Added current module responsibilities and shared helper layer overview
  - Updated lot-size documentation to reflect prediction-driven execution

- **Fixed Startup Failure in Ollama Service**
  - Resolved inconsistent tabs/spaces indentation in `ollama_service.py`
  - Fixed `TabError` during startup when running `python logika.py`
  - Verified that `logika.py` starts, connects to MT5, and enters the service loops without immediate traceback

## 2026-03-24 (Margin Handling in Standard Execution Mode)

- **Standard Mode Margin Validation**
  - `lot_size` from the final Gemini decision is validated against current free margin before execution
  - `take_profit` remains disabled in standard mode
  - Trades with insufficient margin are skipped instead of switching to a separate local balance-based lot formula

## 2026-03-08 (Code Refactoring - DRY Principle)

- **Extracted Shared Market Data Functions to `market_data.py`**
  - Created new module `market_data.py` with common MT5 data collection utilities
  - Removed duplicate code from `logika.py` and `ollama_service.py`
  - Shared functions: `simple_moving_average()`, `rsi_wilder()`, `to_iso_utc()`, `candle_rows_to_json_rows()`, `indicator_rows()`, `get_symbols()`, `copy_rates()`, `collect_symbol_payload()`
  - Both main logic and Ollama service now import from single source
  - Benefits: Easier maintenance, consistent behavior, reduced code duplication

## 2026-03-08 (Ollama Service - Direct MT5 Data Collection)

- **Ollama Service Now Fetches Data Directly from MT5**
  - Replaced `copy_market_data_to_ollama_source()` with `collect_market_data_from_mt5()`
  - Service no longer depends on main logic's data collection cycle
  - Now fully independent - establishes own MT5 connection and fetches market data
  - Added MT5 helper functions: `simple_moving_average()`, `rsi_wilder()`, `candle_rows_to_json_rows()`, `indicator_rows()`, `get_symbols()`, `copy_rates()`, `collect_symbol_payload()`
  - MT5 connection initialized on service startup, shutdown on service stop
  - Same data collection logic as main trading system (all timeframes, RSI, MA indicators)

- **Benefits**
  - Ollama service can start immediately without waiting for main logic's margin trigger
  - True parallel execution - both services operate independently
  - No dependency on shared JSON files in SERVICE_DEST_FOLDER root

## 2026-03-08 (Main Logic Hardening)

- **Fixed Hourly Comparison Across Day Boundary (Ollama Service)**
  - Replaced hour-only check with UTC `YYYYMMDDHH` key comparison
  - Prevents false "already processed" matches between different days with the same hour

- **Made Current-Hour Prediction Folder Selection Deterministic**
  - When multiple folders exist within the same hour, system now selects the latest timestamped folder
  - Avoids random/iteration-order dependent folder reuse

- **Respected Filter Result for Existing Predictions**
  - Main flow now checks whether predictions remain after filtering
  - If all predictions are filtered out, trading step is skipped safely

- **Added Symbol Consistency Validation for Ollama Reuse**
  - Reused Ollama prediction is rejected on `symbol` mismatch against the processed file symbol
  - Prevents accidental cross-symbol reuse due to malformed file content

- **Reduced Unnecessary IO in Trading Logic**
  - Market data JSON is loaded only when Gemini fallback is actually needed
  - Removed unused retry-tracking variable

## 2026-03-08 (Gemini Flow + Ollama Reuse)

- **Main Trading Logic Updated to Reuse Prepared Ollama Predictions**
  - Before `ask_gemini_prediction`, each symbol now checks `SERVICE_DEST_FOLDER/ollama/predikce/{symbol}.json`
  - If Ollama prediction exists and its `timestamp` is not older than 1 hour, it is reused directly
  - Reused Ollama prediction is copied into current run folder: `SERVICE_DEST_FOLDER/<timestamp>/predikce/{symbol}.json`
  - If Ollama prediction is missing, invalid, or older than 1 hour, logic falls back to Gemini (`ask_gemini_prediction`)

- **Validation Rules for Ollama Reuse**
  - Required fields: `BUY`, `SELL`, `HOLD`, `reasoning`, `timestamp`
  - `timestamp` must be valid ISO datetime and within the last 60 minutes
  - Reused payload keeps compatibility with downstream automat (`{symbol}.json` format)

- **Observability**
  - Added runtime counters in logs:
    - Reused from Ollama (<=1h)
    - Generated by Gemini

## 2026-03-08 (Ollama Service Integration)

- **Added Independent Ollama Prediction Service**
  - New parallel service running alongside main Gemini logic
  - Generates predictions using local Ollama AI (deepseek-coder-v2 model)
  - Runs in separate thread - fully independent from main trading loop
  - Can be enabled/disabled via `OLLAMA_ENABLED` in .env (changeable during runtime)
  - Predictions saved to `SERVICE_DEST_FOLDER/ollama/predikce/`

- **Ollama Service Features**
  - Auto-detects if predictions already exist for current hour (skips re-processing)
  - Copies market data to `ollama/source/` folder for analysis
  - Uses same prediction format as Gemini: `symbol`, `BUY`, `SELL`, `HOLD`, `reasoning`
  - Additional metadata: `timestamp`, `model` for tracking
  - 10-minute cycle interval between prediction runs
  - Graceful shutdown on Ctrl+C

- **Configuration**
  - Added `OLLAMA_ENABLED=true` to .env and .env.example
  - Added `OLLAMA_URL=http://localhost:11434/api/generate`
  - Added `OLLAMA_MODEL=deepseek-coder-v2`
  - Service checks .env dynamically - can be toggled without restart

- **File Structure**
  - Source files: `{symbol}.json` (e.g., `EURUSD_ecn.json`)
  - Prediction files: `{symbol}.json` (compatible with downstream automats)
  - Timestamp stored inside JSON, not in filename

## 2026-03-08 (Final Decision Strategy Update)

- **Enhanced Gemini Final Decision Context (Swing + Fees + TP)**
  - Updated final-decision Gemini prompt to reflect swing trading style (positions can stay open for multiple days)
  - Added explicit requirement for daily profit orientation in decision context
  - Added transaction cost context: 0.10 USD fee per 0.01 lot
  - Gemini now returns `take_profit` in final decision JSON (in addition to symbol/action/lot_size/reasoning)

- **Added Hybrid Execution Mode (Every N-th Trade Fully Gemini-Controlled)**
  - New env setting: `GEMINI_FULL_CONTROL_EVERY_N_TRADES` (default/recommended: `3`)
  - Every N-th successful trade uses Gemini `lot_size` and `take_profit`
  - Other trades keep local lot formula and execute without take profit
  - Trade mode is selected by counting successful historical trades from `trade_logs/trades.csv`

- **Trade Execution Enhancements**
  - `execute_trade()` now supports optional take profit
  - Added TP validation for BUY/SELL direction and positive numeric value before order send

- **Config and Stability**
  - Added `GEMINI_FULL_CONTROL_EVERY_N_TRADES=3` to `.env.example`
  - Fixed main loop indentation regression in `logika.py`
  - Added `pytz` to `requirements.txt`

## 2026-03-08 (Restricted Trading Hours)

- **Added Restricted Trading Hours Protection (23:00-23:30 CET/CEST)**
  - Forex market behaves unpredictably during 23:00-23:30 CET/CEST - no trades allowed
  - Added `is_in_restricted_trading_hours()` function to check if current time is within restricted period
  - Added `wait_until_trading_allowed()` function to pause system until 23:30 (sleeps in 10-second intervals)
  - Integrated check at beginning of each cycle: if restricted hours → sleep 30 minutes (no analysis, no downloads)
  - Integrated check before trading: if trading signal triggered in restricted hours → discard signal and wait until 23:30
  - Double-check mechanism ensures no trades occur during unpredictable market conditions
  - Added `pytz` dependency for accurate CET/CEST timezone handling
  - Console output shows countdown timer and reason for pause
  - System automatically resumes at 23:30 without manual intervention

## 2026-03-05 (Infinite Trading Loop)

- **Converted to Infinite Trading Automat**
  - After trade execution, system now automatically restarts monitoring cycle
  - Removed exit-after-trade logic, replaced with infinite while loop
  - Added cycle counter for tracking iterations
  - Simplified account status output to single line (reduces console clutter)
  - Process runs until manually stopped (Ctrl+C)
  - Brief 2-second pause between cycles
  - Error handling: failed trades don't crash the automat, just restart cycle

## 2026-03-05 (Trade Execution)

- **Added Automatic Trade Execution**
  - After Gemini makes final decision, system now automatically executes the trade
  - Added `calculate_lot_size()` function with custom formula: `floor((balance + 500) / 500) / 100`
  - Ignores lot_size recommendation from Gemini, calculates independently based on balance
  - Added `execute_trade()` function to send orders to MT5
  - Uses MT5's ORDER_FILLING_IOC (Immediate-Or-Cancel) for execution
  - Comprehensive logging of trade execution (symbol, action, lot_size, price, order ID)
  - Error handling for failed trades (doesn't crash process)
  - Example: balance 1893 → lot_size 0.04

## 2026-03-05 (MT5 API Fix)

- **Fixed TradePosition Commission Error**
  - Issue: Attempted to access non-existent `commission` attribute on MT5 TradePosition objects
  - Removed commission field from `get_open_positions()` (commission not stored in open positions)
  - Simplified PnL calculation to use MT5's built-in `pos.profit` (more accurate)
  - get_open_positions() now returns: symbol, type, open_time, volume, open_price, current_price, pnl, swap

## 2026-03-05 (JSON Parsing Fix)

- **Fixed JSON Parsing Error in Predictions**
  - Issue: Gemini responses contained markdown code blocks (```json ... ```), causing JSON parsing errors
  - Added `_clean_gemini_response()` function to both trading_logic.py and final_decision.py
  - Strips markdown formatting before saving/loading predictions
  - Updated `filter_predictions()` and `load_predictions()` to handle both old and new format files
  - Backwards compatible with existing prediction files

## 2026-03-05 (Configuration Update)

- **Made Margin Threshold Configurable**
  - Added `TRADING_MARGIN_THRESHOLD` parameter to .env (default: 20%)
  - account_monitor.py now loads threshold from environment instead of hardcoded value
  - Changed default threshold from 10% back to 20%
  - New helper function `_get_margin_threshold()` handles env loading with fallback

## 2026-03-05 (Documentation Update)

- **Enhanced Documentation**
  - Updated README.md with module overview and output file descriptions
  - Expanded TRADING_LOGIC.md with detailed module descriptions:
    - logika.py (orchestration)
    - account_monitor.py (margin monitoring) 
    - trading_logic.py (MT5 data + Gemini predictions)
    - final_decision.py (intelligent final decision-making)
  - Clarified prediction filtering logic and output structure

## 2026-03-05

- **Added Final Trading Decision Module**
  - Created `final_decision.py` with intelligent decision-making based on predictions and account state
  - Queries open positions from MT5 account (time, volume, price, PnL, swap, commission)
  - Combines remaining predictions (BUY/SELL >= 35%) with open positions and account state
  - Sends comprehensive context to Gemini AI for final trading recommendation
  - Output: Single symbol + BUY/SELL action + recommended lot size
  - Results saved to `<SERVICE_DEST_FOLDER>/geminipredictions/PREDIKCE_<timestamp>.json`
  - Process exits after final decision is made

- **Updated trading_logic.py**
  - Changed return type to tuple (success: bool, predictions_folder: Optional[Path])
  - Allows main flow to pass predictions folder to final decision module

- **Major Refactor: New Trading Logic Workflow**
  - Removed hourly scheduler for MT5 data downloads
  - New single-run account monitoring (one-time check instead of continuous)
  - Intelligent prediction reuse: checks for existing predictions from current hour
    - If found: reuses and filters them (faster)
    - If not found: downloads fresh data + gets new Gemini predictions
  - Added prediction filtering: removes predictions where both BUY and SELL < 35%
  - Added retry logic: attempts to get Gemini prediction up to 2 times per symbol
  - Process now exits after trading logic completes (no scheduler loop)

- **Fixed: Trading Logic Trigger was Non-Blocking**
  - Issue: Monitor detected stop condition but scheduler kept sleeping, trading logic started only on Ctrl+C
  - Solution: Implemented `trading_trigger_event` threading.Event for real-time signaling
  - Monitor now sets event immediately when margin > 10% condition is met
  - Scheduler checks event during sleep loop and breaks immediately
  - Trading logic now starts within 1 second of condition detection (instead of waiting for next scheduler cycle)
  
- **Added Trading Logic with Gemini AI Integration**
  - Created `trading_logic.py` module for automated trading predictions
  - Integration with Gemini AI API for market analysis
  - Gemini configuration moved to local `.env` (GEMINI_API_KEY, GEMINI_URL)
  - Automatic processing of all market data files from `SERVICE_DEST_FOLDER`
  - AI predictions based on RSI, MA, candlestick patterns and fundamental analysis
  - Organized output structure: `<timestamp>/source/` and `<timestamp>/predikce/`
  
- **Enhanced Account Monitoring**
  - Added free margin percentage display in console output
  - Changed stop condition threshold from 20% to 10% free margin
  - Monitor now returns boolean to signal when trading logic should trigger
  - Added thread-safe communication between monitor and main logic
  
- **Updated Main Logic Flow**
  - Modified `logika.py` to trigger trading logic when free margin exceeds 10%
  - Added automatic exit after trading logic completion (no continued monitoring)
  - Improved thread management and graceful shutdown
  
- **Dependencies**
  - Added `httpx>=0.27.0` for Gemini API communication
  - Updated `requirements.txt`
  
- **Documentation**
  - Added `TRADING_LOGIC.md` with workflow description and usage examples
  - Documented Gemini prediction format and folder structure

## 2026-03-04

- Added `logika.py` with full hourly scheduler logic (start immediately, then every hour by default).
- Added MetaTrader 5 integration:
  - symbol discovery by suffix (`_ecn` by default)
  - `4H` and `D1` candles for last 30 days
  - RSI and MA calculation for both timeframes
- Added JSON export per symbol into `SERVICE_DEST_FOLDER`.
- Added `.env` driven configuration (destination, intervals, periods, MT5 credentials).
- Added `README.md` with setup and run instructions.
- Added `requirements.txt` with Python dependencies.
