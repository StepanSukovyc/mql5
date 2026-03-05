# Changelog

## 2026-03-05

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
