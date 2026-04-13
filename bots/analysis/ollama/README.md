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
  - `lot_size` se vždy použije z finální Gemini predikce
  - Každý N-tý obchod: použije se i `take_profit` od Gemini
  - Ostatní obchody: `take_profit` se nepoužije
8. **Minutový profit cleanup** (`PROFIT_CLEANUP_STRATEGY_ENABLED`, default `true`):
  - Běží v account monitoru každou minutu od spuštění, ale pouze mimo swap blokovací okno
  - Vezme aktuální bilanci účtu `B` a spočítá referenční objem `VOLUME = ((int)(B / 500) + 1) * 0.01`
  - Pro každou otevřenou pozici spočítá čistý zisk `ZISK = profit + swap - fee`, kde `fee = 0.10 USD` za každých `0.01` lotu
  - Cílový zisk pozice `PCZ` počítá jako `(0.01 * L / VOLUME) * B`, kde `L` je objem pozice; minimum `PCZ` je `0.005`
  - Pokud `ZISK > PCZ`, pozice je vhodná k uzavření a strategie ji uzavře
  - V jednom průchodu uzavírá všechny aktuálně vhodné pozice
  - Přepínač `PROFIT_CLEANUP_STRATEGY_DRY_RUN` (default `true`) pouze vypíše kandidáty a zapíše audit bez skutečného zavření pozic
9. **Swap rollover cleanup** (`SWAP_ROLLOVER_CLEANUP_STRATEGY_ENABLED`, default `true`):
  - Běží v account monitoru každou minutu, ale pouze uvnitř swap blokovacího okna
  - Swap blokovací okno se nyní bere vždy z pevného ručního intervalu z `.env` přes `SWAP_BLOCK_START_*` a `SWAP_BLOCK_END_*`
  - Aktuální interval je `22:30-23:30` v čase `Europe/Prague` a používá se stejně pro lock i rollover cleanup
  - Projde všechny otevřené pozice, které mají aktuální `profit > 0`
  - Spočítá čistý zisk `ZISK = profit + swap - fee`, kde `fee = 0.10 USD` za každých `0.01` lotu
  - Pokud je čistý zisk alespoň `0.10 USD`, pozice je vhodná k uzavření kvůli vyhnutí se swapu
  - Audit log zapisuje i skip/no-candidate průchody, takže je vidět, zda strategie byla mimo okno nebo uvnitř okna nic nenašla
  - Přepínač `SWAP_ROLLOVER_CLEANUP_STRATEGY_DRY_RUN` (default `true`) pouze vypíše kandidáty a zapíše audit bez skutečného zavření pozic
10. **Denní loss cleanup** (`LOSS_CLEANUP_STRATEGY_ENABLED`, default `true`):
  - Spustí se nejvýše jednou za pražský den po čase `LOSS_CLEANUP_STRATEGY_HOUR:LOSS_CLEANUP_STRATEGY_MINUTE` (default `12:45`)
  - Použije realizovaný výsledek za předchozí uzavřený pražský den z historie MT5 dealů jako `profit + swap + commission + actual deal.fee`
  - Pro diagnostiku dál loguje i `daily_clean_profit`, tedy čistý součet `profit` jen z uzavřených pozic referenčního dne podle `position_id`
  - Tento údaj není totéž jako aktuální floating P/L otevřených pozic v panelu Obchodování
  - Modelový poplatek `0.10 USD` za každých `0.01` lotu se nepoužívá pro `daily_realized_profit`; používá se jen při hodnocení ztráty kandidátní otevřené pozice
  - K realizovanému výsledku z předchozího dne přičte jen záporný aktuální open P/L (`equity - raw_balance`), aby nezavíral další ztrátu v momentě, kdy jsou otevřené pozice už celkově v mínusu
  - Od takto upraveného bezpečného rozpočtu odečte `LOSS_CLEANUP_BALANCE_BUFFER_PERCENT` % z aktuální bilance účtu a získá limit `Z` (default `2`)
  - Z otevřených pozic starších než 7 dní najde největší ztrátovou pozici, jejíž ztráta včetně swapu a poplatku `0.10 USD` za každých `0.01` lotu je stále menší než `Z`
  - Zároveň kandidáta odmítne, pokud by po jeho uzavření klesl tento bezpečný rozpočet pod `0.00`
  - Pokud mají dva bezpeční kandidáti stejnou ztrátu, ponechá první nalezenou pozici
  - Pokud taková pozice existuje, uzavře ji; jinak neudělá nic
  - Stavový soubor `trade_logs/loss_cleanup_state.json` brání tomu, aby se po restartu proces spustil vícekrát ve stejný pražský den
  - V čase swap blokovacího okna se cleanup nespouští stejně jako běžné obchodování
  - Přepínač `LOSS_CLEANUP_STRATEGY_DRY_RUN` (default `true`) vypíše kandidáta a zaloguje akci, ale pozici skutečně nezavře
11. **Provede obchod** na MT5 podle aktivního režimu
12. Uloží rozhodnutí do `geminipredictions/PREDIKCE_<timestamp>.json`
13. **Vrátí se na krok 3** (restart monitoring)

**Automatické pozastavení v swap blokovacím okně:**
- Blokace se řídí pevným ručním intervalem z `.env`
  - Systém se zastaví v intervalu `SWAP_BLOCK_START_*` až `SWAP_BLOCK_END_*`
  - Jakákoli připravená rozhodnutí se v tomto okně zahodí
  - Aktuální konfigurace je `22:30-23:30` v čase `Europe/Prague`

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
PROFIT_CLEANUP_STRATEGY_ENABLED=true
PROFIT_CLEANUP_STRATEGY_DRY_RUN=true
SWAP_ROLLOVER_CLEANUP_STRATEGY_ENABLED=true
SWAP_ROLLOVER_CLEANUP_STRATEGY_DRY_RUN=true

# Manualni fallback block window, kdyz MT5 historie neposkytne pouzitelny rollover cas
SWAP_BLOCK_START_HOUR=22
SWAP_BLOCK_START_MINUTE=30
SWAP_BLOCK_END_HOUR=23
SWAP_BLOCK_END_MINUTE=30
# SWAP_ROLLOVER_LOOKBACK_DAYS=14
# SWAP_BLOCK_HALF_WINDOW_MINUTES=30
LOSS_CLEANUP_STRATEGY_ENABLED=true
LOSS_CLEANUP_STRATEGY_HOUR=12
LOSS_CLEANUP_STRATEGY_MINUTE=45
LOSS_CLEANUP_BALANCE_BUFFER_PERCENT=2
LOSS_CLEANUP_STRATEGY_DRY_RUN=true

# Ollama service konfigurace (nezávislé predikce)
OLLAMA_ENABLED=true
OLLAMA_URL=http://localhost:11434/api/generate
OLLAMA_MODEL=deepseek-coder-v2
```

Rucni blokovaci okno `SWAP_BLOCK_START_*` az `SWAP_BLOCK_END_*` je interpretovano v case `Europe/Prague`. Audit a trade logy zustavaji ulozene v UTC.

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

Pro rychlé ověření výpočtu profit cleanup bez MT5 můžete spustit:

```bash
python verify_profit_cleanup_strategy.py
```

Automatické testy výpočtu spustíte takto:

```bash
python -m unittest test_profit_cleanup_strategy.py
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
- **profit_cleanup_strategy.py** - Volitelná minutová strategie pro uzavírání všech otevřených profitních pozic, které překročí svůj vypočtený limit `PCZ`
- **verify_profit_cleanup_strategy.py** - Lokální validační skript pro výpočet `VOLUME`, `ZISK` a `PCZ` na zadaných scénářích
- **loss_cleanup_strategy.py** - Volitelná hodinová strategie pro uzavření jedné starší ztrátové pozice podle limitu `Z`
- **trading_logic.py** - Stahuje data z MT5, preferuje čerstvé Ollama predikce, fallbackuje na Gemini a filtruje slabé signály
- **final_decision.py** - Kombinuje predikce se stavem účtu, dělá finální rozhodnutí a provádí obchod
- **ollama_service.py** - Paralelní služba generující predikce pomocí lokálního Ollama AI (běží v samostatném threadu)
- **market_data.py** - Sdílené utility pro sběr dat z MT5 (RSI, MA, svíčky) - používají logika.py i ollama_service.py

## Výstupní Soubory

- `<SERVICE_DEST_FOLDER>/<timestamp>/predikce/*.json` - Filtrované predikce
- `<SERVICE_DEST_FOLDER>/geminipredictions/PREDIKCE_<timestamp>.json` - Finální rozhodnutí
- `<SERVICE_DEST_FOLDER>/ollama/predikce/{symbol}.json` - Předchystané Ollama predikce (použitelné v hlavní logice při stáří <= 1h)
- `<SERVICE_DEST_FOLDER>/trade_logs/profit_cleanup.csv` - Audit minutové profit cleanup strategie včetně `B`, referenčního `VOLUME`, `ZISK`, `PCZ` a výsledku close pokusu
- `<SERVICE_DEST_FOLDER>/trade_logs/loss_cleanup.csv` - Audit hodinové cleanup strategie včetně hodnot `daily_clean_profit`, `daily_realized_profit`, `Z` a případně uzavřené pozice
- `<SERVICE_DEST_FOLDER>/trade_logs/loss_cleanup_daily_deals.csv` - Diagnostický snapshot všech dealů, které MT5 API při cleanup běhu skutečně vrátilo, včetně `actual_fee`, `modeled_fee` a `realized_component`
- Dokud testujete, nechte `PROFIT_CLEANUP_STRATEGY_DRY_RUN=true`; po ověření změňte na `false`
- Dokud testujete, nechte `LOSS_CLEANUP_STRATEGY_DRY_RUN=true`; po ověření změňte na `false`
- `LOSS_CLEANUP_BALANCE_BUFFER_PERCENT=2` určuje, jak velkou část aktuální raw bilance má loss cleanup ponechat jako bezpečnostní rezervu před zavřením ztrátové pozice

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
- Finální rozhodnutí se dělá na **právě jednom měnovém páru** s velikostí lotu převzatou z Gemini predikce
- Každý `N`-tý obchod (`GEMINI_FULL_CONTROL_EVERY_N_TRADES`) používá `lot_size + take_profit` od Gemini
- Ostatní obchody používají `lot_size` od Gemini a bez take profit
- **Nekonečný loop:** Skript běží dokola, dokud není ručně zastaven
- Ukončení skriptu: `Ctrl+C`

