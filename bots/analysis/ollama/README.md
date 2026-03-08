# MT5 Hourly Collector (Python)

Automatický obchodní systém s AI rozhodováním. Skript běží jako **nekonečný automat**: kontroluje marži, stahuje data, filtruje signály, vytváří finální obchodní doporučení pomocí Gemini AI, **automaticky provádí obchody** a **opakuje celý cyklus**.

**Nově přidáno**: Paralelní **Ollama Service** - nezávislá smyčka generující predikce pomocí lokálního AI modelu.

## Co skript dělá

**Hlavní cyklus (Gemini AI):**

1. Načte konfiguraci z `.env`.
2. Připojí se k MetaTrader 5 (`MetaTrader5` Python package).
3. Spustí monitoring volné marže.
4. Jakmile marže > 20%:
   - Zkontroluje, zda existují předpovědi z **aktuální hodiny**
   - **Pokud ano**: používá je (bez stahování nových dat)
   - **Pokud ne**: stáhne data a získá nové předpovědi od Gemini AI
  - Při tvorbě predikce pro každý symbol nejdřív zkontroluje `SERVICE_DEST_FOLDER/ollama/predikce/{symbol}.json`
  - Pokud je `timestamp` v souboru čerstvý (max 1 hodina), reuse-ne Ollama predikci místo volání Gemini
  - Pokud Ollama predikce neexistuje / je nevalidní / je starší než 1h, použije fallback `ask_gemini_prediction`
5. Filtruje slabé předpovědi (BUY < 35% AND SELL < 35% → smaže)
6. Dělá **finální rozhodnutí**:
   - Kombinuje zbývající predikce se stavem účtu a otevřenými pozicemi
  - Gemini AI vybere **1 měnový pár**, rozhodne **BUY/SELL**, navrhne `lot_size` a `take_profit`
  - V promptu zohledňuje swing styl (nejde o intraday), denní cíl ziskovosti a poplatek `0.10 USD` za každých `0.01` lotu
7. **Režim exekuce dle pořadí obchodu** (`GEMINI_FULL_CONTROL_EVERY_N_TRADES`, default 3):
  - Každý N-tý obchod: použije se `lot_size` i `take_profit` od Gemini
  - Ostatní obchody: `lot_size` se počítá vzorcem `floor((balance + 500) / 500) / 100` a `take_profit` se nepoužije
8. **Provede obchod** na MT5 podle aktivního režimu
9. Uloží rozhodnutí do `geminipredictions/PREDIKCE_<timestamp>.json`
10. **Vrátí se na krok 3** (restart monitoring)

**Automatické pozastavení v kritických hodinách:**
- **23:00-23:30 CET/CEST**: Trh se chová nepředvídatelně, žádné obchody a analýzy
  - Cyklus se zastaví (lock) na 30 minut
  - Jakákoli připravená rozhodnutí se zahodí
  - Obchody se automaticky obnoví v 23:30

**Ukončení:** Ctrl+C

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

# Každý N-tý obchod je plně řízen Gemini (lot_size + take_profit)
GEMINI_FULL_CONTROL_EVERY_N_TRADES=3

# Ollama service konfigurace (nezávislé predikce)
OLLAMA_ENABLED=true
OLLAMA_URL=http://localhost:11434/api/generate
OLLAMA_MODEL=deepseek-coder-v2
```

**LOOKBACK_PERIODS** - Počet posledních period, které se mají stahovat pro každý timeframe. Výchozí 30.

## Spusteni

**Příprava (pokud chcete používat Ollama):**
```bash
# Ujistěte se, že Ollama server běží
ollama serve

# Stáhněte model deepseek-coder-v2
ollama pull deepseek-coder-v2
```

**Spuštění aplikace:**
```bash
python logika.py
```

Skript běží jako **nekonečný obchodní automat**:
1. Spustí **Ollama Service** v samostatném threadu
2. Spustí monitoring volné marže (hlavní logika)
3. Když marže > 20%:
   - Zkontroluje existující predikce z aktuální hodiny
  - Používá je, nebo stáhne nová data a získá nové predikce
  - Pro každý symbol preferuje čerstvou Ollama predikci (<= 1h), jinak volá Gemini
   - Filtruje slabé signály
   - Provede obchod
4. **Restart cyklu** (vrací se na krok 2)
5. **Ukončení:** Ctrl+C (zastaví obě smyčky korektně)

**Poznámka:** Ollama Service běží paralelně celou dobu a generuje vlastní predikce nezávisle. **Stahuje data přímo z MT5** (vlastní připojení) - není závislý na hlavní logice.

## Automatické spuštění

Protože skript běží jako nekonečný automat, stačí ho spustit **jednou** při startu systému:

### Linux (systemd service)
Vytvoř service file `/etc/systemd/system/mt5-trading.service`:

```ini
[Unit]
Description=MT5 Trading Automat
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/bots/analysis/ollama
ExecStart=/usr/bin/python3 logika.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Spuštění:
```bash
sudo systemctl enable mt5-trading
sudo systemctl start mt5-trading
```

### Windows (Task Scheduler)
Vytvoř task, který spustí `python logika.py` při startu systému.

## Moduly Systému

- **logika.py** - Hlavní orchestrace nekonečného cyklu (monitoring → predikce → finální rozhodnutí → obchod → restart)
- **account_monitor.py** - Monitoruje volnou marži a signalizuje překročení 20% prahu (single-line output)
- **trading_logic.py** - Stahuje data z MT5, preferuje čerstvé Ollama predikce, fallbackuje na Gemini a filtruje slabé signály
- **final_decision.py** - Kombinuje predikce se stavem účtu, dělá finální rozhodnutí a provádí obchod
- **ollama_service.py** - Paralelní služba generující predikce pomocí lokálního Ollama AI (běží v samostatném threadu)
- **market_data.py** - Sdílené utility pro sběr dat z MT5 (RSI, MA, svíčky) - používají logika.py i ollama_service.py

## Výstupní Soubory

- `<SERVICE_DEST_FOLDER>/<timestamp>/predikce/*.json` - Filtrované predikce
- `<SERVICE_DEST_FOLDER>/geminipredictions/PREDIKCE_<timestamp>.json` - Finální rozhodnutí
- `<SERVICE_DEST_FOLDER>/ollama/predikce/{symbol}.json` - Předchystané Ollama predikce (použitelné v hlavní logice při stáří <= 1h)

## Poznamky

- MetaTrader 5 terminal musí běžet lokálně ve stejném uživatelském kontextu
- Pokud některý symbol selže, skript pokračuje na další symbol
- Monitorování probíhá v **background threadu**, nezablokuje tedy ostatní procesy
- **Ollama Service**: Stahuje data **přímo z MT5** (vlastní připojení), není závislý na hlavní logice
- Optimalizace: Pokud je k dispozici čerstvá Ollama predikce symbolu (max 1h), použije se místo volání Gemini
- Hardening: Kontrola "už zpracováno v této hodině" porovnává UTC datum+hodinu (YYYYMMDDHH), ne pouze hodinu
- Hardening: Pokud existuje více predikčních složek v aktuální hodině, systém vybere nejnovější timestamp
- Hardening: Pokud filtr smaže všechny existující predikce, obchodní krok se bezpečně přeskočí
- Hardening: Reuse Ollama predikce ověřuje konzistenci symbolu, aby nedošlo ke křížení párů
- Finální rozhodnutí se dělá na **právě jednom měnovém páru** s vypočtenou velikostí lotu
- Každý `N`-tý obchod (`GEMINI_FULL_CONTROL_EVERY_N_TRADES`) používá `lot_size + take_profit` od Gemini
- Ostatní obchody používají vlastní lot výpočet: `floor((balance + 500) / 500) / 100` a bez take profit
- **Nekonečný loop:** Skript běží dokola, dokud není ručně zastaven
- Ukončení skriptu: `Ctrl+C`

