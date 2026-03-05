# MT5 Hourly Collector (Python)

Tento modul kazdou hodinu (od okamziku spusteni) nacita data z lokalne beziciho MetaTrader 5 a uklada je do JSON souboru pro kazdy symbol.

## Co skript dela

1. Nacte konfiguraci z `.env`.
2. Pripoji se k MetaTrader 5 (`MetaTrader5` Python package).
3. Ziska vsechny symboly s koncovkou `_ecn` (nebo jinou dle konfigurace).
4. Pro kazdy symbol stahne posledních N period pro vsechny timeframy:
   - `1h` (1 hodina) - posledních 30 period
   - `4h` (4 hodiny) - posledních 30 period
   - `day` (1 den) - posledních 30 period
   - `week` (1 tyden) - posledních 30 period
   - `month` (1 mesic) - posledních 30 period
   - Oscilatory (RSI + MA) pro kazdy timeframe
5. Zapise vystup do `SERVICE_DEST_FOLDER/<SYMBOL>.json`.
6. Po dokonceni ceka do dalsiho hodinoveho intervalu.

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
```

**LOOKBACK_PERIODS** - Počet posledních period, které se mají stahovat pro každý timeframe. Výchozí 30.

## Spusteni

```bash
python logika.py
```

Skript provede prvni cyklus okamzite po startu a pak kazdych `RUN_INTERVAL_SECONDS` sekund.

## Poznamky

- MetaTrader 5 terminal musi bezet lokalne ve stejnem uzivatelskem kontextu.
- Pokud nektery symbol selze, skript pokracuje na dalsi symbol.
- Ukonceni skriptu: `Ctrl+C`.

