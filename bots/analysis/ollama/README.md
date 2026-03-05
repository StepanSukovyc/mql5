# MT5 Hourly Collector (Python)

Automatický obchodní systém s AI predikcemi. Skript kontroluje marži, stahuje data a dotazuje se Gemini AI na obchodní signály.

## Co skript dělá

1. Načte konfiguraci z `.env`.
2. Připojí se k MetaTrader 5 (`MetaTrader5` Python package).
3. Spustí monitoring volné marže (jednoho).
4. Jakmile marže > 10%:
   - Zkontroluje, zda existují předpovědi z **aktuální hodiny**
   - **Pokud ano**: používá je (bez stahování nových dat)
   - **Pokud ne**: stáhne data a získá nové předpovědi od Gemini AI
5. Filtruje slabé předpovědi (BUY < 35% AND SELL < 35% → smaže)
6. Vykonáný proces skončí (bez pokračujícího scheduleru)

## Struktura vystupu

Kazdy soubor ma tvar:

```json
{
  "symbol": "EURUSD_ecn",
  "generated_at": "2026-03-04T10:00:00+00:00",
  "lookback_periods": 30,
  "current_price": 1.08654,
  "candles": {
    "1h": [
      {
        "time": "...",
        "open": 1.082,
        "high": 1.085,
        "low": 1.081,
        "close": 1.084,
        "tick_volume": 1234,
        "spread": 12,
        "real_volume": 0
      }
    ],
    "4h": [...],
    "day": [...],
    "week": [...],
    "month": [...]
  },
  "oscillators": {
    "1h": {
      "rsi": [{ "time": "...", "value": 51.23 }],
      "ma": [{ "time": "...", "value": 1.08654 }]
    },
    "4h": {...},
    "day": {...},
    "week": {...},
    "month": {...}
  }
}
```

## Instalace

Vytvorte virtualni prostredi a nainstalujte zavislosti:

```bash
pip install -r requirements.txt
```

## Konfigurace `.env`

Zkopirujte `bots/analysis/ollama/.env.example` na `bots/analysis/ollama/.env` a upravte hodnoty:

```env
SERVICE_DEST_FOLDER=C:/path/to/output/folder

# Optionalni (defaulty jsou uvedene v zavorce)
MT5_SYMBOL_SUFFIX=_ecn
LOOKBACK_PERIODS=30
RUN_INTERVAL_SECONDS=3600
RSI_PERIOD=14
MA_PERIOD=20
PRETTY_JSON=true

# Optionalni login (kdyz MT5 session neni dostupna automaticky)
# MT5_LOGIN=12345678
# MT5_PASSWORD=your_password
# MT5_SERVER=YourBroker-Server

# Gemini AI konfigurace pro trading logic
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_URL=https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent
```

**LOOKBACK_PERIODS** - Počet posledních period, které se mají stahovat pro každý timeframe. Výchozí 30.

## Spusteni

```bash
python logika.py
```

Skript níže:
1. Spustí monitoring volné marže (jedenkrát)
2. Pokud marže > 10%:
   - Zkontroluje existující predikce z aktuální hodiny
   - Používá je, nebo stáhne nová data a získá nové predikce
   - Filtruje slabé signály
3. **Skončí** (bez pokračného monitoringu)

## Automatické spuštění v cron

Chcete-li spouštět skript opakovaně (např. každou hodinu), použijte cron:

```bash
0 * * * * cd /path/to/bots/analysis/ollama && python logika.py
```

Skript bude spuštěn na začátku každé hodiny.

## Poznamky

- MetaTrader 5 terminal musi bezet lokalne ve stejnem uzivatelskem kontextu.
- Pokud nektery symbol selze, skript pokracuje na dalsi symbol.
- Monitorování probíhá v **background threadu**, nezablokuje tedy ostatní procesy
- Optimalizace: Pokud jsou k dispozici předpovědi z aktuální hodiny, jsou používány (bez nového stahování)
- Ukonceni skriptu: `Ctrl+C` (normálně skript sám skončí po obchodování)

