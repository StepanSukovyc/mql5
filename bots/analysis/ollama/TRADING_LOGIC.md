# Trading Logic - Gemini AI Integration

## Přehled Systému

Komplexní event-driven trading systém монitoruje volnou marži a dělá inteligentní obchodní rozhodnutí.

**Fáze procesu:**
1. **Monitoruje volnou marži** - controls account status once
2. **Rozhoduje se pružně**:
   - Pokud existují predikce z **aktuální hodiny** → používá je (reuse)
   - Pokud ne → stáhne data z MT5 + získá nové predikce od Gemini AI
3. **Filtruje slabé predikce** - odstraňuje soubory kde BUY < 35% AND SELL < 35%
4. **Dělá finální rozhodnutí** - kombinuje zbývající predikce se stavem účtu a otevřenými pozicemi
   - Gemini AI vybere **1 měnový pár** a rozhodne BUY/SELL
   - Doporučí velikost lotu na základě volné marže a risk managementu
5. **Ukončuje proces** - po finálním rozhodnutí skončí (bez scheduleru)

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
                   ▼ margin > 10%?
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
       │        │                            │
       │        │ • Get open positions       │
       │        │ • Get account state        │
       │        │ • Combine with predictions │
       │        │ • Ask Gemini for final     │
       │        │   recommendation:          │
       │        │   - 1 symbol (BUY/SELL)    │
       │        │   - Recommended lot size   │
       │        │ • Save to PREDIKCE_        │
       │        │   <timestamp>.json         │
       │        └──────┬───────────────────┘
       │               │
       │        ┌──────▼──────┐
       │        │ Exit        │
       │        └─────────────┘
       │
       └──────────────────────┐
                              │
                       ┌──────▼──────┐
                       │ Exit        │
                       │ (no trading)│
                       └─────────────┘
```

## Struktura výstupu

```
<SERVICE_DEST_FOLDER>/
  ├── <timestamp>/
  │   ├── source/          # Původní JSON soubory s tržními daty
  │   └── predikce/        # Gemini AI predikce (BUY/SELL >= 35%)
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
   - Očekávané výstupy: 1 měnový pár + BUY/SELL + doporučená velikost lotu
   - Gemini bere v úvahu Risk Management (volná marže, otevřené pozice)

4. **Uložení:**
   - Výsledek se uloží do: `<SERVICE_DEST_FOLDER>/geminipredictions/PREDIKCE_<timestamp>.json`
   - Proces se poté ukončí (bez automulálního otevřování obchodu)

## Module Description

### 1. logika.py - Main Orchestration
Hlavní skript, který koordinuje celý proces:
- Inicializuje MT5 připojení a konfiguraci
- Spouští account_monitor v background threadu
- Čeká na signál překročení 10% marže
- Rozhoduje: reuse existujících predikcí nebo download nových dat
- Volá final_decision modul pro obchodní rozhodnutí
- Ukončuje se po finálním rozhodnutí

**Klíčové funkce:**
- `find_predictions_folder_for_current_hour()` - hledá existující predikce z aktuální hodiny
- `process_existing_predictions()` - aplikuje filtrování na existující predikce
- `main()` - hlavní orchestrační cyklus

### 2. account_monitor.py - Account Monitoring
Monitoruje stav účtu v background threadu:
- Pravidelně kontroluje **volnou marži** v procenta
- Signalizuje překročení **10% prahu** pomocí threading.Event
- Zobrazuje info o účtu (zůstatek, equity, marže)
- Běží bez blokování hlavního vlákna

**Klíčové funkce:**
- `get_account_info()` - dotaz do MT5
- `print_account_status()` - výpis na konzoli (včetně % volné marže)
- `check_stop_condition()` - ověří margin > 10%, nastavuje event
- `run_account_monitor()` - monitoring loop v threadu

### 3. trading_logic.py - Trading Predictions
Stahuje data a generuje predikce:
- Stahuje OHLC data z MT5 pro všechny symboly
- Dotazuje se Gemini AI na predikce (BUY%, SELL%)
- **Filtruje slabé signály:** Odstraňuje soubory kde BUY < 35% AND SELL < 35%
- Vrací cestu ke složce s filtrovanými předpověďmi

**Klíčové funkce:**
- `ask_gemini_prediction()` - dotaz na Gemini s tržními daty
- `filter_predictions()` - smaže soubory s nízkými skóre
- `run_trading_logic()` - orchestruje stahování → dotaz → filtrování

**Vrací:** `tuple[bool, Optional[Path]]` - úspěch a cesta ke složce predikcí

### 4. final_decision.py - Final Trading Decision
Dělá finální inteligentní obchodní rozhodnutí:
- Načítá **všechny otevřené pozice** z MT5 (bez filtrování)
- Sbírá **stav účtu** (zůstatek, equity, margin %)
- Načítá **filtrované predikce** (BUY/SELL >= 35%)
- Dotazuje se Gemini na finální doporučení
- Ukladdá výsledek do `geminipredictions/PREDIKCE_<timestamp>.json`

**Klíčové funkce:**
- `get_open_positions()` - vrací seznam pozic s PnL, swap, poplachy
- `get_account_state()` - vrací stav účtu (balance, equity, margin %)
- `load_predictions()` - načítá filtrované predikce
- `ask_gemini_final_decision()` - Gemini query pro finální doporučení
- `make_final_trading_decision()` - orchestruje celý proces

**Expected Gemini Response:**
```json
{
  "recommended_symbol": "EURUSD_ecn",
  "action": "BUY",
  "lot_size": 0.5,
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

## Bezpečnost

- Soubory nejsou zpracovány dvakrát v jednom cyklu
- Kontrola existence souborů před přesunem
- Graceful error handling při selhání Gemini API
- Ochrana proti duplicitním dotazům

## Poznámky

- Po dokončení trading logiky se skript **automaticky ukončí** (nepokračuje v monitoringu)
- Mezitím běžící jiná logika může vytvářet nové soubory - ty nejsou zpracovány
- Všechny chyby jsou logovány, ale nezastaví zpracování ostatních symbolů
