# Trading Logic - Gemini AI Integration + Ollama Service

## Přehled Systému

Komplexní event-driven trading systém monitoruje volnou marži a dělá inteligentní obchodní rozhodnutí. **Nově** běží paralelně nezávislý **Ollama Service** pro kontinuální generování predikcí pomocí lokálního AI modelu.

**Hlavní proces (Gemini AI - nekonečný cyklus):**
1. **Kontroluje kritické hodiny** - pokud je 23:00-23:30 CET/CEST, počká do 23:30 (bez analýz)
2. **Monitoruje volnou marži** - kontroluje stav účtu
3. **Rozhoduje se pružně**:
   - Pokud existují predikce z **aktuální hodiny** → používá je (reuse)
   - Pokud ne → stáhne data z MT5 + získá nové predikce od Gemini AI
   - Pro každý symbol se před dotazem na Gemini kontroluje `SERVICE_DEST_FOLDER/ollama/predikce/{symbol}.json`
   - Pokud je `timestamp` validní a soubor není starší než 1 hodina, použije se Ollama predikce
   - Pokud Ollama predikce chybí / je nevalidní / je starší než 1h, provede se fallback na `ask_gemini_prediction`
4. **Filtruje slabé predikce** - odstraňuje soubory kde BUY < 35% AND SELL < 35%
5. **Kontroluje kritických hodin (znovu)** - pokud je trading signal v 23:00-23:30, zahodí ho a čeká
6. **Dělá finální rozhodnutí** - kombinuje zbývající predikce se stavem účtu a otevřenými pozicemi
   - Gemini AI vybere **1 měnový pár**, rozhodne BUY/SELL, navrhne lot_size a take_profit
   - V promptu je explicitně swing styl (ne intraday), denní cíl zisků a poplatek 0.10 USD za 0.01 lotu
7. **Aplikuje režim exekuce podle pořadí obchodu** (`GEMINI_FULL_CONTROL_EVERY_N_TRADES`, default 3)
   - Každý N-tý obchod: použije se lot_size + take_profit z Gemini
   - Ostatní obchody: lot_size se vypočítá lokálně, take_profit se nepoužije
   - Pokud lokálně vypočtený `lot_size` neprojde margin checkem (`Insufficient margin`), systém provede fallback na `lot_size` z finální Gemini predikce
8. **Provede obchod** - automaticky otevře pozici na MT5
9. **Restart cyklu** - po provedení obchodu se vrací na krok 1 (nekonečná smyčka)
10. **Ukončení** - Ctrl+C

### Restricted Trading Hours (23:00-23:30 CET/CEST)

Forex trh se v tomto období chová nepředvídatelně. systém tedy:
- **Zastavuje se** (lock) každodenně od 23:00 do 23:30
- **Vypíná analýzu** - žádné stahování dat, žádné Gemini AI dotazy
- **Blokuje obchody** - jakékoli signály jsou zahozeny
- **Automaticky obnovuje** v 23:30 bez zásahu

Dvojitá kontrola zajišťuje bezpečnost:
1. Na začátku cyklu: Pokud je restricted time → sleep na 30 minut
2. Před obchodováním: Pokud je trading trigger v restricted time → zahodí signál a čeká

## Ollama Service (Paralelní Proces)

**Nezávislá smyčka běžící v samostatném threadu:**

1. **Kontrola aktivace** - čte `OLLAMA_ENABLED` z .env (dynamicky, lze měnit za běhu)
2. **Pokud disabled** → spí 5 minut a opakuje krok 1
3. **Pokud enabled**:
   - Zkopíruje aktuální tržní data z `SERVICE_DEST_FOLDER` do `ollama/source/`
   - Pro každý symbol:
     - Zkontroluje, zda predikce z aktuální hodiny už existuje (podle `mtime` souboru)
     - Pokud ano → přeskočí (data jsou platná celou hodinu)
     - Pokud ne → pošle data na Ollama API (model: deepseek-coder-v2)
     - Parsuje JSON odpověď a extrahuje `BUY`, `SELL`, `HOLD`, `reasoning`
     - Uloží do `ollama/predikce/{symbol}.json` s metadaty (`timestamp`, `model`)
   - Tyto soubory pak hlavní trading flow reuse-ne, pokud jsou čerstvé (<= 1h)
   - Čeká 10 minut a opakuje krok 1
4. **Graceful shutdown** - zastaví se při Ctrl+C společně s hlavním procesem

**Výhody:**
- Běží nezávisle - negeneruje blokování hlavního procesu
- Lokální AI - žádné API limity, žádné cloudy
- Hodinové cykly - respektuje validitu dat (nepřepočítává stejnou hodinu)
- Kompatibilní výstup - stejný formát jako Gemini (`symbol`, `BUY`, `SELL`, `HOLD`, `reasoning`)
- Lze vypnout/zapnout za běhu změnou `OLLAMA_ENABLED` v .env
- Hlavní logika umí predikce přímo převzít do `SERVICE_DEST_FOLDER/<timestamp>/predikce/` bez nového AI dotazu

**Příklad výstupu:**
```json
{
  "symbol": "EURUSD_ecn",
  "BUY": 45,
  "SELL": 30,
  "HOLD": 25,
  "reasoning": "Na základě RSI a MA analýzy doporučuji...",
  "timestamp": "2026-03-08T15:30:45+00:00",
  "model": "deepseek-coder-v2"
}
```

## Nový Workflow

```
┌─────────────────────────────────────────────────────┐
│ Main Process Starts                                 │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
        ┌──────────────────────┐
        │ Account Monitor      │
        │ (check once)         │
        └──────────┬───────────┘
                   │
                   ▼ margin > 20%?
                   │
       ┌───────────┴───────────┐
       │                       │
       NO                      YES
       │                       │
       │                ┌──────▼────────────┐
       │                │ Check for existing│
       │                │ predictions from  │
       │                │ current hour      │
       │                └──┬────────────┬──┘
       │                   │            │
       │                FOUND        NOT FOUND
       │                   │            │
       │          ┌────────▼──┐  ┌──────▼──────────┐
       │          │ Use        │  │ Download MT5    │
       │          │ existing   │  │ data + Query    │
       │          │ predictions│  │ Gemini AI       │
       │          └────┬───────┘  └────┬───────────┘
       │               │               │
       │        ┌──────▼──────────────▼┐
       │        │ Filter predictions   │
       │        │ (BUY < 35% AND       │
       │        │  SELL < 35% →delete) │
       │        └──────┬───────────────┘
       │               │
       │        ┌──────▼──────────────────────┐
       │        │ FINAL DECISION MODULE       │
       │        │ (final_decision.py)         │
       │        │                             │
       │        │ • Get open positions        │
       │        │ • Get account state         │
       │        │ • Combine with predictions  │
       │        │ • Ask Gemini for final      │
       │        │   recommendation:           │
       │        │   - 1 symbol (BUY/SELL)     │
       │        │ • Calculate lot_size:       │
       │        │   floor((balance+500)/500)  │
       │        │   /100                      │
       │        │ • If margin is insufficient │
       │        │   fallback to Gemini lot    │
       │        │ • Execute trade on MT5      │
       │        │ • Save to PREDIKCE_         │
       │        │   <timestamp>.json          │
       │        └──────┬──────────────────────┘
       │               │
       │        ┌──────▼──────┐
       │        │ Restart     │
       │        │ Cycle       │
       │        └──────┬──────┘
       │               │
       │               └──────┐
       │                      │
       └──────────────────────┴───────┐
                                      │
                              ┌───────▼────────┐
                              │ Loop back to   │
                              │ Account Monitor│
                              └────────────────┘
                                      │
                              ┌───────▼────────┐
                              │ (Infinite loop,│
                              │  exit: Ctrl+C) │
                              └────────────────┘
```

## Struktura výstupu

```
<SERVICE_DEST_FOLDER>/
  ├── <timestamp>/
  │   ├── source/          # Původní JSON soubory s tržními daty
  │   └── predikce/        # Gemini AI predikce (BUY/SELL >= 35%)
  │
  ├── ollama/              # 🆕 Ollama Service výstupy
  │   ├── source/          # Kopie tržních dat pro Ollama
  │   └── predikce/        # Ollama AI predikce ({symbol}.json)
  │
  └── geminipredictions/
      └── PREDIKCE_<timestamp>.json   # Finální rozhodnutí (1 pár + akce + lot)
```

### Příklad Final Decision formátu:

```json
{
  "recommended_symbol": "EURUSD_ecn",
  "action": "BUY",
  "lot_size": 0.5,
   "take_profit": 1.105,
  "reasoning": "Technická analýza a stav účtu doporučují vstup do long pozice..."
}
```

### Příklad timestamp: `20260305_143022`

Timestamp je vždy generován v UTC. Pokud je aktuální čas 19:45 UTC a existuje složka `20260305_19xxxx/predikce/`, bude se používat.

## Predikční Formát

Každá predikce je JSON soubor s názvem `SYMBOL.json`:

```json
{
  "symbol": "EURMXN_ecn",
  "BUY": 40,
  "SELL": 30,
  "HOLD": 30,
  "reasoning": "Technická analýza ukazuje..."
}
```

**Filtrování**: Pokud `BUY < 35` **AND** `SELL < 35`, soubor se smaže (příliš nejistý signal)

## Opakované Pokusy (Retry)

Když Gemini API vrátí chybu pro nějaký symbol:
- 1. pokus → chyba → 2. pokus
- Pokud 2. pokus selže → symbol se přeskočí

Zaznamenáno v logeních.

## Finální Rozhodnutí (final_decision.py)

Po filtrování zbývajících predikcí (BUY/SELL >= 35%):

1. **Načtení dat z MT5:**
   - Všechny aktuálně otevřené pozice (čas otevření, volume, cena, PnL, swap, poplatek)
   - Aktuální stav účtu (balance, equity, volná marže)

2. **Příchystávání kontextu pro Gemini:**
   - Kombinuje zbývající predikce se stavem účtu a pozicemi
   - Vytváří komplexní kontext pro finální rozhodnutí

3. **Dotaz na Gemini AI:**
   - Očekávané výstupy: 1 měnový pár + BUY/SELL + doporučená velikost lotu + take_profit
   - Gemini bere v úvahu Risk Management (volná marže, otevřené pozice)
   - Gemini bere v úvahu styl obchodování: swing (pozice i několik dní), snaha o denní ziskovost a trading fee (0.10 USD za 0.01 lot)
   - **DIVERZIFIKACE:** Gemini preferuje symboly bez otevřených pozic. Pokud už pozice na doporučovaném symbolu existuje a aktuální tržní cena je blízko vstupní ceny (< 0.5% rozdíl), Gemini **povinně vybírá jiný kandidát** z dostupných predikcí pro bezpečnou diverzifikaci portfolia.

4. **Uložení a provedení obchodu:**
   - Parsuje JSON odpověď od Gemini (symbol, action, lot_size, take_profit)
   - Aplikuje režim `GEMINI_FULL_CONTROL_EVERY_N_TRADES`:
     - Každý N-tý obchod: použije Gemini lot_size i take_profit
     - Ostatní obchody: použije lokální lot vzorec a obchoduje bez take_profit
   - Provede obchod na MT5 (BUY nebo SELL)
   - Uloží rozhodnutí do: `<SERVICE_DEST_FOLDER>/geminipredictions/PREDIKCE_<timestamp>.json`
   - Proces se poté ukončí

## Lot Size Calculation

Standardní režim (většina obchodů) počítá lot přes sdílený helper `trade_risk.calculate_lot_size()`:

```
lot_size = floor((balance + 500) / 500) / 100
```

**Příklady:**
- Balance 1893 → (1893 + 500) / 500 = 4.786 → floor = 4 → 4/100 = **0.04**
- Balance 2500 → (2500 + 500) / 500 = 6.0 → floor = 6 → 6/100 = **0.06**
- Balance 500 → (500 + 500) / 500 = 2.0 → floor = 2 → 2/100 = **0.02**

Každý N-tý obchod (N = `GEMINI_FULL_CONTROL_EVERY_N_TRADES`) používá lot_size a take_profit přímo z Gemini odpovědi.

## Module Description

### 1. logika.py - Main Orchestration
Hlavní skript, který koordinuje **nekonečný obchodní cyklus** se zásadou Forex market safety:
- Inicializuje MT5 připojení a konfiguraci
- Běží v nekonečné smyčce (while True)
- **Kontroluje restricted trading hours** (23:00-23:30 CET/CEST)
   - Pokud je v restricted hours → `wait_until_trading_allowed()` (sleep 30 minut, bez analýz)
   - Pokud je trading trigger v restricted hours → zahodí signál a čeká do 23:30
- Běží v nekonečné smyčce (while True)
- Každý cyklus: spouští account_monitor v background threadu
- Čeká na signál překročení 20% marže
- Rozhoduje: reuse existujících predikcí nebo download nových dat
- Volá final_decision modul pro obchodní rozhodnutí
- Po dokončení obchodu **restartuje cyklus** (vrací se na monitoring)
- Ukončení: Ctrl+C

**Klíčové funkce:**
- `is_in_restricted_trading_hours()` - kontroluje, zda je čas v 23:00-23:30 CET/CEST
- `wait_until_trading_allowed()` - počká do 23:30 bez jakýchkoli akcí (spí v 10-sec intervalech)
- `find_predictions_folder_for_current_hour()` - hledá existující predikce z aktuální hodiny
- `process_existing_predictions()` - aplikuje filtrování na existující predikce
- `main()` - hlavní orchestrační nekonečný cyklus

### 2. account_monitor.py - Account Monitoring
Monitoruje stav účtu v background threadu:
- Pravidelně kontroluje **volnou marži** v procenta
- Signalizuje překročení **20% prahu** (konfigurovatelné v .env) pomocí threading.Event
- Zobrazuje info o účtu (zůstatek, equity, marže)
- Běží bez blokování hlavního vlákna

**Klíčové funkce:**
- `get_account_state_snapshot()` - dotaz do MT5 včetně timestampu
- `print_account_status()` - výpis na konzoli (včetně % volné marže)
- `check_stop_condition()` - ověří margin > threshold%, nastavuje event
- `run_account_monitor()` - monitoring loop v threadu

### 3. trading_logic.py - Trading Predictions
Stahuje data a generuje predikce:
- Stahuje OHLC data z MT5 pro všechny symboly
- Pro každý symbol nejdřív kontroluje čerstvou Ollama predikci (`SERVICE_DEST_FOLDER/ollama/predikce/{symbol}.json`)
- Pokud je Ollama predikce validní a max 1h stará, zkopíruje ji do běžné predikční složky cyklu
- Pokud není dostupná, dotazuje se Gemini AI na predikci (BUY%, SELL%)
- **Filtruje slabé signály:** Odstraňuje soubory kde BUY < 35% AND SELL < 35%
- Vrací cestu ke složce s filtrovanými předpověďmi

**Klíčové funkce:**
- `ask_gemini_prediction()` - dotaz na Gemini s tržními daty
- `filter_predictions()` - smaže soubory s nízkými skóre
- `run_trading_logic()` - orchestruje stahování → dotaz → filtrování

**Vrací:** `tuple[bool, Optional[Path]]` - úspěch a cesta ke složce predikcí

### 4. final_decision.py - Final Decision Orchestration
Řídí finální workflow, ale většinu specializované logiky deleguje do sdílených helper modulů:
- Načítá filtrované predikce, stav účtu a otevřené pozice
- Spouští Gemini dotaz pro finální doporučení
- Aplikuje obchodní režim podle pořadí obchodu (`GEMINI_FULL_CONTROL_EVERY_N_TRADES`)
- Ukládá finální JSON rozhodnutí do `geminipredictions/PREDIKCE_<timestamp>.json`
- Předává hotové parametry exekuční vrstvě

**Klíčové funkce:**
- `make_final_trading_decision()` - hlavní orchestrátor finální fáze
- `_resolve_trade_parameters()` - volí STANDARD vs FULL GEMINI režim
- `_parse_decision()` - validuje a parsuje Gemini JSON odpověď

### 5. Shared Helper Modules
Refaktor rozdělil původní monolit do menších odpovědností:

**MT5 / account / symbol vrstva:**
- `account_state.py` - stav účtu, raw account info, login účtu
- `mt5_connection.py` - inicializace a shutdown MT5 spojení
- `mt5_symbols.py` - symbol metadata, tick data, current price
- `mt5_positions.py` - serializace otevřených pozic

**Gemini / decision vrstva:**
- `gemini_config.py` - načtení `GEMINI_API_KEY` a `GEMINI_URL`
- `gemini_decision.py` - čištění Gemini odpovědí, načtení predikcí, finální Gemini decision query

**Trading / execution vrstva:**
- `trading_validation.py` - validace symbolu, lot size a marže
- `trade_execution.py` - logování a provedení obchodu na MT5
- `trade_history.py` - čtení historie úspěšných obchodů z CSV
- `trade_risk.py` - výpočet standardního lot size

**Expected Gemini Response:**
```json
{
  "recommended_symbol": "EURUSD_ecn",
  "action": "BUY",
  "lot_size": 0.5,
   "take_profit": 1.105,
  "reasoning": "Kombinovaná analýza prediktivního modelu..."
}
```

## Gemini Predikce

Každá predikce obsahuje:

```json
{
  "symbol": "EURUSD_ecn",
  "BUY": 45,
  "SELL": 30,
  "HOLD": 25,
  "reasoning": "RSI ukazuje překoupenost, MA trend je neutrální..."
}
```

## Konfigurace

### account_monitor.py
- **Práh (Threshold)**: Načítá se z `TRADING_MARGIN_THRESHOLD` v .env (default 20%)
  - Lze jednoduše změnit bez editace kódu
  - Příklad: `TRADING_MARGIN_THRESHOLD=25` pro 25% prah
- **Interval**: 60 sekund (parametr `check_interval_seconds`)

### trading_logic.py
- **Gemini API**: Konfigurace v lokálním `.env` souboru
  - `GEMINI_API_KEY` - váš Gemini API klíč
  - `GEMINI_URL` - Gemini API endpoint
- **Delay**: 5 sekund mezi dotazy (respektování API limitů)
- **Retry**: 60 sekund při quota exceeded (429)

### final_decision.py
- `GEMINI_FULL_CONTROL_EVERY_N_TRADES` - každý N-tý obchod je plně svěřen Gemini (lot_size + take_profit)
- Fee kontext v promptu: `0.10 USD` za `0.01` lotu
- Swing kontext v promptu: pozice mohou být otevřené i několik dní, ale cílem je denní ziskovost

## Použití

### Automatický běh
Stačí spustit hlavní skript:
```bash
python logika.py
```

### Standalone trading logic (testování)
```bash
python trading_logic.py [cesta_ke_složce]
```

## Požadavky

```
MetaTrader5>=5.0.45
httpx>=0.27.0
```

Instalace:
```bash
pip install -r requirements.txt
```

## Bezpečnost a Validace

### Ochrana proti chybám
- Soubory nejsou zpracovány dvakrát v jednom cyklu
- Kontrola existence souborů před přesunem
- Graceful error handling při selhání Gemini API
- Ochrana proti duplicitním dotazům

### Validace před obchodem
1. **Symbol Validation** (`validate_symbol()`)
   - Kontrola existence symbolu přes `mt5.symbol_info()`
   - Automatické přidání symbolu do MarketWatch pokud není viditelný
   - Ověření, že trading je povolen pro daný symbol
   - V případě selhání: symbol se přidá do exclusion listu a systém požádá Gemini o jiný symbol (max 3 pokusy)

2. **Lot Size Validation** (`validate_lot_size()`)
   - Načtení min/max/step limitů od brokera
   - Automatická úprava lotu na broker-kompatibilní hodnotu
   - Zaokrouhlení na správný lot_step
   - Ochrana proti příliš malým nebo velkým pozicím

3. **Margin Requirements Check** (`check_margin_requirements()`)
   - Přesný výpočet potřebné marže přes `mt5.order_calc_margin()`
   - Porovnání s dostupnou volnou marží
   - Prevence obchodů s nedostatečnou marží

4. **Diverzifikace Portfolia**
   - Gemini AI preferuje symboly bez otevřených pozic
   - Pokud pozice na symbolu existuje, zkontroluje vstupní cenu
   - Při rozdílu < 0.5% mezi aktuální a vstupní cenou **povinně vybírá jiný symbol**
   - Ochrana proti duplicitním pozicím za podobnou cenu na stejném páru

### Gemini API Quota Management
- **Automatická suspenze** při dosažení limitu (HTTP 429)
- Systém přestane dotazovat Gemini až do půlnoci následujícího dne (UTC)
- Trading automat pokračuje v běhu (pouze přeskakuje dotazy)
- Automatické obnovení po půlnoci

### Trade Logging
- **CSV záznam všech obchodů** do `SERVICE_DEST_FOLDER/trade_logs/trades.csv`
- Loguje úspěchy i selhání včetně error zpráv
- Formát: timestamp, symbol, action, lot_size, price, success, error_msg
- Automatické vytvoření hlaviček při prvním použití

### Error Recovery
- Oddělené zachycení network errors (`httpx.HTTPError`) vs obecné výjimky
- Detailní error zprávy v logu i CSV
- Retry mechanismus pro failed symbol validation (max 3 pokusy)
- Graceful handling při selhání kterékoliv validační fáze

## Poznámky

- Po dokončení trading logiky se skript **automaticky ukončí** (nepokračuje v monitoringu)
- Mezitím běžící jiná logika může vytvářet nové soubory - ty nejsou zpracovány
- Všechny chyby jsou logovány, ale nezastaví zpracování ostatních symbolů
