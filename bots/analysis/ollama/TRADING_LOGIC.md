# Trading Logic - Gemini AI Integration + Ollama Service

## Přehled Systému

Komplexní event-driven trading systém monitoruje volnou marži a dělá inteligentní obchodní rozhodnutí. Standardně teď běží v **úsporném režimu**, ve kterém se průběžně obnovují MT5 data, ale předanalýza přes lokální Ollama se nespouští. Mimo úsporný režim může paralelně běžet nezávislý **Ollama Service** pro kontinuální generování predikcí pomocí lokálního AI modelu.

Nově jsou nad AI rozhodování přidané i **tvrdé programové guardraily**, ale AI zůstává hlavním rozhodovacím jádrem:

- data už neobsahují jen `RSI` a `SMA`, ale i `ATR`, `Bollinger Bands` a aktuální `spread`
- před dotazem na Gemini se pro každý symbol dopočítá **market regime**: `trend`, `range`, `disorder`, `unknown`
- do AI se pouští jen kandidáti, kteří projdou session/spread filtrem a mají potvrzený trendový setup
- finální `take_profit` se primárně počítá programově z `ATR_H1` a síly trendu; Gemini TP zůstává fallback
- přibyla samostatná **profit protection** vrstva, která uzavírá jen ziskové pozice po retracementu nebo po dlouhém držení, nikdy ne ztrátové
- systém nově umí spouštět dvě oddělené strategie: hlavní AI větev a paralelní indexovou AI větev
- obě strategie si značkují obchody vlastním `strategy_id` a `magic`, takže následná správa umí sahat jen na jejich vlastní pozice

**Hlavní proces (Gemini AI - nekonečný cyklus):**
1. **Kontroluje swap blok okno** - používá pevný `.env` interval `SWAP_BLOCK_START_*` až `SWAP_BLOCK_END_*`, interpretovaný v čase `Europe/Prague`, a v aktivním okně čeká do konce blokace (bez analýz)
2. **Monitoruje volnou marži** - kontroluje stav účtu
3. **V úsporném režimu průběžně obnovuje data** - každých `ECONOMY_MODE_INTERVAL_SECONDS` sekund stáhne z MT5 čerstvá tržní data do `SERVICE_DEST_FOLDER`, ale bez Ollama analýzy
4. **Rozhoduje se pružně**:
   - Pokud je `ECONOMY_MODE_ENABLED=true` → přeskočí reuse připravených Ollama predikcí a při obchodním triggeru jde rovnou přes Gemini
   - Pokud je `ECONOMY_MODE_ENABLED=false` a existují predikce z **aktuální hodiny** → používá je (reuse)
   - Pokud ne → stáhne data z MT5 + získá nové predikce od Gemini AI
   - Pro každý symbol se před dotazem na Gemini kontroluje `SERVICE_DEST_FOLDER/ollama/predikce/{symbol}.json`
   - V úsporném režimu se tato kontrola jen diagnosticky přeskočí, protože lokální předanalýza je vypnutá
   - Pokud je `timestamp` validní a soubor není starší než limit `OLLAMA_PREDICTION_MAX_AGE_MINUTES` z `.env` (default 120 minut), použije se Ollama predikce
    - Pokud Ollama predikce chybí / je nevalidní / je starší než nastavený limit:
       - při `ECONOMY_MODE_ENABLED=true` se vždy použije Gemini
       - při `OLLAMA_FALLBACK_TO_GEMINI=true` proběhne fallback na `ask_gemini_prediction`, ale jen do limitu `OLLAMA_GEMINI_FALLBACK_MAX_INSTRUMENTS` instrumentů za cyklus
       - při `OLLAMA_FALLBACK_TO_GEMINI=false` se instrument v tomto běhu ignoruje
    - Gemini fallback větev může běžet paralelně, ale maximálně do `GEMINI_FALLBACK_MAX_PARALLEL_REQUESTS` současných dotazů
    - Po vyčerpání limitu Gemini fallbacku se další instrumenty bez čerstvé Ollama predikce ignorují
    - Pokud po tomto filtrování nezůstane žádný instrument s použitelnou predikcí, cyklus skončí bez vytvoření predikcí a bez nákupu
5. **Dopočítá režim trhu a programové vstupní guardraily**
   - z `H1/H4/D1` dat se dopočítá `ATR`, `Bollinger bandwidth`, sklon `MA` a spread
   - symbol je povolen do AI fáze jen pokud:
     - je otevřené obchodní session dané strategie
     - není pátek po cutoff hodině
     - spread nepřekračuje profilový limit strategie
     - režim trhu je `trend`
     - a platí alespoň jeden potvrzený setup (`buy_setup` nebo `sell_setup`)
   - `buy_setup` a `sell_setup` jsou pořád programově odvozené z kombinace `D1/H4/H1 + RSI + MA + ATR`; Gemini pak vybírá z už prověřených kandidátů
6. **Filtruje slabé predikce** - odstraňuje soubory kde BUY < 35% AND SELL < 35%
7. **Kontroluje swap blok okno (znovu)** - pokud trading signal přijde v rollover okně, zahodí ho a čeká
8. **Dělá finální rozhodnutí** - kombinuje zbývající predikce se stavem účtu a otevřenými pozicemi
   - Gemini AI stále vybírá **1 instrument**, rozhoduje BUY/SELL a navrhuje lot_size
   - predikce už ale obsahují i embedded `market_context`, takže AI vidí, v jakém režimu je symbol nalezený a jestli šlo o `buy_setup` nebo `sell_setup`
   - finální rozhodnutí je navíc omezené profilem strategie: whitelist symbolů, max open positions, max trades/day a max trades/symbol/day
   - Když jsou nastavené `GEMINI_API_KEY` a `GEMINI_URL`, je Vertex pouze jednorázový první pokus; při první chybě se request okamžitě přepne na `legacy-gemini-api`
9. **Aplikuje exekuční guardraily**
   - lot_size se dál bere z Gemini, ale nově dostává **balance-based cap**, protože strategie záměrně nepoužívá stop-loss a sizing tedy není risk-based
   - primární `take_profit` se dopočítá programově z `ATR_H1`
     - běžný trend: cca `2.2 * ATR_H1`
     - silný trend: cca `2.8 * ATR_H1`
   - u crypto a CFD se stále uplatňují existující safeguardy a limity vzdálenosti/fee-aware TP
10. **Vyhodnotí minutový profit cleanup** (`PROFIT_CLEANUP_STRATEGY_ENABLED`, default `true`)
   - Běží během minutového account monitoru při každém ticku monitoru, nejvýše jednou za minutu
   - Běží pouze mimo swap blok okno
   - Nově kandidáty hledá **po strategiích odděleně** podle `strategy_id` + `magic`
   - Vezme aktuální raw bilanci účtu `B` a spočítá referenční objem `VOLUME = ((int)(B / 500) + 1) * 0.01`
   - Pro každou otevřenou pozici vypočte čistý zisk `ZISK = profit + swap - fee`
   - Syntetický `fee` je `0.10 USD` za každých `0.01` lotu
   - Cílový profit `PCZ = (0.01 * L / VOLUME) * B`, minimum `PCZ` je `0.005`
   - Pokud `ZISK > PCZ`, pozice je vhodná k uzavření; v jednom běhu se uzavřou všechny takové pozice
   - Pokud je `PROFIT_CLEANUP_STRATEGY_DRY_RUN=true` (default), kandidáti se jen vypíšou a zalogují
11. **Vyhodnotí swap rollover cleanup** (`SWAP_ROLLOVER_CLEANUP_STRATEGY_ENABLED`, default `true`)
   - Běží během minutového account monitoru nejvýše jednou za minutu, ale pouze uvnitř swap blok okna
   - Nově také běží odděleně po strategiích a sahá jen na pozice označené konkrétní strategií
   - Swap blok okno se bere vždy z pevného ručního intervalu z `.env`
   - Aktuální konfigurace je `22:30-23:30` v čase `Europe/Prague`
   - Projde všechny otevřené pozice, které mají aktuální `profit > 0`
   - Pro každou spočítá čistý zisk `ZISK = profit + swap - fee`
   - Syntetický `fee` je `0.10 USD` za každých `0.01` lotu
   - Pokud `ZISK >= 0.10 USD`, pozice je vhodná k uzavření; v jednom běhu se uzavřou všechny takové pozice
   - Audit log zapisuje i skip/no-candidate průchody, takže je zpětně vidět, jestli strategie byla mimo okno nebo jen nic nenašla
   - Pokud je `SWAP_ROLLOVER_CLEANUP_STRATEGY_DRY_RUN=true` (default), kandidáti se jen vypíšou a zalogují
12. **Vyhodnotí profit protection vrstvu** (`PROFIT_PROTECTION_STRATEGY_ENABLED`, default `true`)
    - Běží mimo swap block window nejvýše jednou za minutu
    - Pracuje jen s pozicemi, které otevřela některá aktivní strategie (`gemini_primary`, `gemini_indices`)
    - Nikdy nezavírá ztrátovou pozici
    - Sleduje lokální maximum `max_net_profit` po ticketu a při dostatečném retracementu uzavírá stále ziskovou pozici
    - Umí i time-based profit exit:
       - profitní pozice starší než `PROFIT_PROTECTION_STALE_HOURS`
       - nebo profitní pozice starší než `PROFIT_PROTECTION_MAX_HOLD_DAYS`
13. **Vyhodnotí denní loss cleanup** (`LOSS_CLEANUP_STRATEGY_ENABLED`, default `true`)
   - Běží během minutového account monitoru nejvýše jednou za pražský den po čase `LOSS_CLEANUP_STRATEGY_HOUR:LOSS_CLEANUP_STRATEGY_MINUTE` (default `12:45`)
   - Použije realizovaný výsledek za předchozí uzavřený pražský den z MT5 deal historie včetně `profit`, `swap`, `commission` a `actual deal.fee`
   - Pro diagnostiku dál loguje i `daily_clean_profit`, tedy čistý součet `profit` jen z uzavřených pozic referenčního dne
   - K tomuto realizovanému výsledku přičte jen záporný aktuální open P/L (`equity - raw_balance`) a odečte `LOSS_CLEANUP_BALANCE_BUFFER_PERCENT` % z aktuální bilance účtu; tím vznikne limit `Z` (default `2`)
   - Najde otevřenou ztrátovou pozici starší než 7 dní s nejvyšší ztrátou, která je stále menší než `Z`
   - Kandidáta navíc odmítne, pokud by po close spadl efektivní profit budget pod `0.00`
   - Pokud mají dva bezpeční kandidáti stejnou ztrátu, ponechá první nalezenou pozici
   - Do ztráty počítá i swap a syntetický fee `0.10 USD` za každých `0.01` lotu
   - Stavový soubor `trade_logs/loss_cleanup_state.json` brání opakovanému spuštění ve stejný pražský den i po restartu procesu
   - Pokud je `LOSS_CLEANUP_STRATEGY_DRY_RUN=true` (default), kandidáta jen zaloguje a nic nezavírá
   - V čase swap blok okna se cleanup nespouští
14. **Provede obchod** - automaticky otevře pozici na MT5 a označí ji `strategy_id` + `magic`
15. **Restart cyklu** - po provedení obchodu se vrací na krok 1 (nekonečná smyčka)
16. **Ukončení** - Ctrl+C

## Dvě Strategie Nad Jedním Účtem

Systém teď může provozovat dvě AI strategie nad jedním účtem současně:

- `gemini_primary`
   - hlavní strategie
   - standardně používá hlavní `SERVICE_DEST_FOLDER`
   - může obchodovat celý povolený univerzum nebo volitelný whitelist
- `gemini_indices`
   - paralelní indexová strategie
   - standardně zapisuje do `SERVICE_DEST_FOLDER/indices_strategy`
   - standardně používá whitelist doporučených indexů:
      - `US100_ecn`, `US500_ecn`, `US30_ecn`, `GER40_ecn`, `FRA40_ecn`, `UK100_ecn`, `JP225_ecn`, `EU50_ecn`, `HKD50_ecn`, `FANG_ecn`

Obě strategie sdílí stejný account trigger, ale:

- mají oddělené složky s daty a predikcemi
- mají vlastní `strategy_id`
- mají vlastní `magic`
- mají vlastní denní limity obchodů
- jejich profit cleanup, rollover cleanup i profit protection už pracují jen nad jejich vlastními označenými pozicemi

## Úsporný Režim

Úsporný režim se ovládá přes `.env`:

- `ECONOMY_MODE_ENABLED=true` je výchozí nastavení
- `ECONOMY_MODE_INTERVAL_SECONDS=300` znamená refresh dat každých 5 minut

Když je úsporný režim aktivní:

- hlavní proces nespouští samostatný `Ollama Service` thread
- během čekání na obchodní trigger se na pozadí jen stahují čerstvá tržní data z MT5
- při obchodním triggeru se nepoužije reuse Ollama predikcí a analýza jde rovnou přes Gemini
- ostatní logika zůstává stejná: filtrace slabých predikcí, finální decision, cleanup strategie i exekuce obchodu

### Swap Block Window

Forex trh se v rollover okně chová nepředvídatelně. Systém tedy:
- **Zastavuje se** (lock) v pevném intervalu z `.env`, interpretovaném v čase `Europe/Prague`
- **Vypíná analýzu** - žádné stahování dat, žádné Gemini AI dotazy
- **Blokuje obchody** - jakékoli signály jsou zahozeny
- **Automaticky obnovuje** na konci vypočteného okna bez zásahu

Dvojitá kontrola zajišťuje bezpečnost:
1. Na začátku cyklu: Pokud je swap block window aktivní → čeká do konce okna
2. Před obchodováním: Pokud trading trigger dorazí uvnitř swap block window → zahodí signál a čeká

Stejné vypočtené okno platí i pro cleanup strategie, takže v tomto čase neběží běžné obchodování ani loss cleanup. Aktivní interval se vždy bere z `SWAP_BLOCK_START_*` až `SWAP_BLOCK_END_*`.

## Ollama Service (Paralelní Proces)

Tato část se používá jen při `ECONOMY_MODE_ENABLED=false`.

**Nezávislá smyčka běžící v samostatném threadu:**

1. **Kontrola aktivace** - čte `OLLAMA_ENABLED` z .env (dynamicky, lze měnit za běhu)
2. **Pokud disabled** → spí 5 minut a opakuje krok 1
3. **Pokud enabled**:
   - Zkopíruje aktuální tržní data z `SERVICE_DEST_FOLDER` do `ollama/source/`
   - Pro každý symbol:
     - Zkontroluje, zda predikce z aktuální hodiny už existuje (podle `mtime` souboru)
     - Pokud ano → přeskočí (data jsou platná celou hodinu)
     - Pokud ne → pošle data na Ollama API (model: deepseek-coder-v2)
   - Requesty na Ollama mohou běžet paralelně, omezené přes `OLLAMA_MAX_PARALLEL_REQUESTS`
   - Volitelný throttle mezi spuštěním requestů řídí `OLLAMA_REQUEST_DELAY_SECONDS`
    - Přepínač `OLLAMA_COMPACT_PROMPT` určuje, zda Ollama dostane plný raw payload nebo kompaktní shrnutí dat
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
      │                │ Economy mode?     │
      │                └──┬────────────┬──┘
      │                   │            │
      │                 YES           NO
      │                   │            │
      │          ┌────────▼──┐  ┌──────▼────────────┐
      │          │ Use        │  │ Check for existing│
      │          │ Gemini on  │  │ predictions from  │
      │          │ fresh MT5  │  │ current hour      │
      │          │ data        │  └──┬────────────┬──┘
      │          └────┬───────┘     │            │
      │               │          FOUND        NOT FOUND
      │               │             │            │
      │               │     ┌──────▼──┐  ┌──────▼──────────┐
      │               │     │ Use      │  │ Download MT5    │
      │               │     │ existing │  │ data + Query    │
      │               │     │ Ollama   │  │ Gemini AI       │
      │               │     │ output   │  └────┬───────────┘
      │               │     └────┬─────┘       │
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
      │        │ • Use Gemini lot_size       │
      │        │ • Every N-th trade also     │
      │        │   uses Gemini take_profit   │
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
   ├── indices_strategy/    # Paralelní indexová AI větev
   │   ├── <timestamp>/
   │   │   ├── source/
   │   │   └── predikce/
   │   └── geminipredictions/
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
- Pokud není aktivní legacy API-key fallback: 1. pokus → chyba → 2. pokus
- Pokud jsou nastavené `GEMINI_API_KEY` a `GEMINI_URL`: 1. Vertex pokus → chyba → okamžitý přechod na `legacy-gemini-api`
- Pokud i legacy API selže → symbol se přeskočí

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
   - Gemini bere v úvahu Risk Management (efektivní volná marže, otevřené pozice)
   - Gemini bere v úvahu styl obchodování: swing (pozice i několik dní), snaha o denní ziskovost a trading fee (0.10 USD za 0.01 lot)
   - **DIVERZIFIKACE:** Gemini preferuje symboly bez otevřených pozic. Pokud už pozice na doporučovaném symbolu existuje a aktuální tržní cena je blízko vstupní ceny (< 0.5% rozdíl), Gemini **povinně vybírá jiný kandidát** z dostupných predikcí pro bezpečnou diverzifikaci portfolia.

4. **Uložení a provedení obchodu:**
   - Parsuje JSON odpověď od Gemini (symbol, action, lot_size, take_profit)
   - Aplikuje režim `GEMINI_FULL_CONTROL_EVERY_N_TRADES`:
       - lot_size použije vždy z Gemini
       - Každý N-tý obchod: použije i Gemini take_profit
       - Ostatní obchody: obchoduje bez take_profit
   - Pokud je vybraný symbol crypto, systém před exekucí zkontroluje aktuální bid/ask cenu a `take_profit` z Gemini případně zkrátí na konzervativní maximum:
      - BUY: TP nesmí být dál než `MT5_CRYPTO_TP_DISTANCE_PERCENT` % nad aktuální ask cenou
      - SELL: TP nesmí být dál než `MT5_CRYPTO_TP_DISTANCE_PERCENT` % pod aktuální bid cenou
      - Když Gemini vrátí neplatný nebo chybějící TP, použije se fallback přesně na tuto konzervativní hranici
   - Provede obchod na MT5 (BUY nebo SELL)
   - Pokud selže symbol validation, výpočet trade parametrů nebo samotná exekuce, aktuální symbol se přidá do exclusion listu a Gemini dostane další pokus s jiným kandidátem
   - Pokud už po vyloučení nezbývá žádný vhodný symbol, finální decision fáze skončí bez obchodu
   - Uloží rozhodnutí do: `<SERVICE_DEST_FOLDER>/geminipredictions/PREDIKCE_<timestamp>.json`
   - Proces se poté ukončí

## Lot Size Calculation

Lot size se nyní bere vždy z finální Gemini predikce a před exekucí se už jen validuje proti pravidlům symbolu a dostupné marži.
- Balance 2500 → (2500 + 500) / 500 = 6.0 → floor = 6 → 6/100 = **0.06**
- Balance 500 → (500 + 500) / 500 = 2.0 → floor = 2 → 2/100 = **0.02**
- Balance 6200 při `TRADING_ACCOUNT_BALANCE_CAP=5000` → výpočet běží jako pro balance 5000 → **0.11**

Každý N-tý obchod (N = `GEMINI_FULL_CONTROL_EVERY_N_TRADES`) používá lot_size a take_profit přímo z Gemini odpovědi.

Pro crypto instrumenty se take_profit nepřebírá bez omezení. Výsledný TP je vždy zvalidovaný proti aktuální trhu a omezený přes `MT5_CRYPTO_TP_DISTANCE_PERCENT`, aby systém neotevíral crypto obchody s nepřiměřeně vzdáleným targetem.

## Module Description

### 1. logika.py - Main Orchestration
Hlavní skript, který koordinuje **nekonečný obchodní cyklus** se zásadou Forex market safety:
- Inicializuje MT5 připojení a konfiguraci
- Běží v nekonečné smyčce (while True)
- **Kontroluje swap block window**
   - Pokud broker MT5 historie vrátí použitelný rollover čas → blokuje se broker-derived okno
   - Pokud ne → použije ruční fallback interval z `.env` (`SWAP_BLOCK_START_*` až `SWAP_BLOCK_END_*`)
   - Pokud je trigger uvnitř okna → zahodí signál a čeká do konce vypočteného okna
- Běží v nekonečné smyčce (while True)
- Každý cyklus: spouští account_monitor v background threadu
- Čeká na signál překročení 20% marže
- Rozhoduje: reuse existujících predikcí nebo download nových dat
- Volá final_decision modul pro obchodní rozhodnutí
- Po dokončení obchodu **restartuje cyklus** (vrací se na monitoring)
- Ukončení: Ctrl+C

**Klíčové funkce:**
- `is_in_restricted_trading_hours()` - kontroluje, zda je čas uvnitř broker-derived nebo fallback swap block window
- `wait_until_trading_allowed()` - počká do konce aktuálního swap block window bez jakýchkoli akcí (spí v 10-sec intervalech)
- `find_predictions_folder_for_current_hour()` - hledá existující predikce z aktuální hodiny
- `process_existing_predictions()` - aplikuje filtrování na existující predikce
- `main()` - hlavní orchestrační nekonečný cyklus

### 2. account_monitor.py - Account Monitoring
Monitoruje stav účtu v background threadu:
- Pravidelně kontroluje **efektivní volnou marži** v procentech vůči efektivnímu balance
- Signalizuje překročení **20% prahu** (konfigurovatelné v .env) pomocí threading.Event
- Zobrazuje info o účtu (zůstatek, equity, marže)
- Spouští minutovou profit cleanup strategii a denní loss cleanup strategii, pokud jsou povolené
- Běží bez blokování hlavního vlákna

**Klíčové funkce:**
- `get_account_state_snapshot()` - dotaz do MT5 včetně timestampu
- `print_account_status()` - výpis na konzoli (včetně % volné marže)
- `check_stop_condition()` - ověří margin > threshold%, nastavuje event
- `run_account_monitor()` - monitoring loop v threadu

### 2a. loss_cleanup_strategy.py - Daily Loss Cleanup
Volitelná bezpečnostní strategie pro jednorázové denní odlehčení starých ztrátových pozic:
- Čte runtime konfiguraci přímo z `.env`, takže ji lze za běhu zapnout, vypnout nebo přepnout mezi dry-run a live režimem
- Vyhodnocuje se nejvýše jednou za pražský den po čase `LOSS_CLEANUP_STRATEGY_HOUR:LOSS_CLEANUP_STRATEGY_MINUTE`
- Počítá `Z` z realizovaného výsledku za předchozí uzavřený pražský den, záporného aktuálního open P/L a bufferu `LOSS_CLEANUP_BALANCE_BUFFER_PERCENT` % z aktuální bilance (default `2`)
- Předchozí realizovaný výsledek bere ze všech dealů referenčního dne včetně `profit`, `swap`, `commission` a `actual deal.fee`
- Pro diagnostiku dál počítá i `daily_clean_profit` z uzavřených pozic referenčního dne identifikovaných přes `position_id`
- Prochází otevřené pozice starší než 7 dní a vybírá největší ztrátu menší než `Z`
- Kandidáta navíc blokuje, pokud by po zavření klesl efektivní profit budget pod nulu
- Pokud mají dva kandidáti stejný `loss_amount`, zůstává vybraný první nalezený kandidát
- Do efektivní ztráty zahrnuje `profit`, `swap` a syntetický fee `0.10 USD / 0.01 lotu`
- V `LOSS_CLEANUP_STRATEGY_DRY_RUN=true` pouze vypíše kandidáta a zapíše audit do CSV
- V ostrém režimu používá `close_position_by_ticket()` ze sdílené exekuční vrstvy
- Zapisuje audit do `trade_logs/loss_cleanup.csv`
- Pro diagnostiku rozdílů proti mobilní aplikaci zapisuje i raw snapshot dealů z `history_deals_get()` do `trade_logs/loss_cleanup_daily_deals.csv`
- Zapisuje i stavový soubor `trade_logs/loss_cleanup_state.json`, který zabraňuje opakovanému spuštění ve stejný pražský den

### 2b. profit_cleanup_strategy.py - Minute Profit Cleanup
Volitelná strategie pro průběžné uzavírání otevřených profitních pozic podle objemu a velikosti účtu:
- Čte runtime konfiguraci přímo z `.env`, takže ji lze za běhu zapnout, vypnout nebo přepnout mezi dry-run a live režimem
- Vyhodnocuje se nejvýše jednou za minutu během account monitoru
- Počítá `VOLUME = ((int)(B / 500) + 1) * 0.01` z aktuální raw bilance účtu `B`
- Pro každou otevřenou pozici počítá `ZISK = profit + swap - fee`, kde `fee = 0.10 USD / 0.01 lotu`
- Cílový profit `PCZ` počítá jako `(0.01 * L / VOLUME) * B`, kde `L` je objem pozice; minimum `PCZ` je `0.005`
- V jednom běhu uzavírá všechny pozice, kde platí `ZISK > PCZ`
- V `PROFIT_CLEANUP_STRATEGY_DRY_RUN=true` pouze vypíše kandidáty a zapíše audit do CSV
- V ostrém režimu používá `close_position_by_ticket()` ze sdílené exekuční vrstvy
- Zapisuje audit do `trade_logs/profit_cleanup.csv`

### 2c. verify_profit_cleanup_strategy.py - Validation Script
Pomocný lokální skript pro ověření výpočtu profit cleanup strategie bez připojení k MT5:
- Používá stejnou helper funkci jako runtime strategie, takže nekopíruje výpočty bokem
- Umí vypsat předdefinované scénáře i scénáře z CLI argumentů ve formátu `balance volume profit swap`
- Vypisuje `VOLUME`, `fee`, `ZISK`, `PCZ` a boolean `eligible`

### 2d. test_profit_cleanup_strategy.py - Unit Tests
Lehká automatická kontrola správnosti výpočtu profit cleanup strategie:
- Používá standardní `unittest`, takže nepotřebuje nové dependency
- Ověřuje uživatelský příklad, pozitivní scénář, minimum `PCZ` i vliv swapu a fee
- Dá se spustit přes `python -m unittest test_profit_cleanup_strategy.py`

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
- Při transportním timeoutu Python SDK umí Gemini request zopakovat přes přímý Vertex REST call se stejným service accountem
- Pokud všechny Vertex pokusy selžou, umí jako poslední fallback použít starší Gemini API flow přes `GEMINI_API_KEY` a `GEMINI_URL`
- Aplikuje obchodní režim podle pořadí obchodu (`GEMINI_FULL_CONTROL_EVERY_N_TRADES`)
- Pracuje s efektivním balance/free margin podle `TRADING_ACCOUNT_BALANCE_CAP`
- Využívá embedded `market_context` z predikcí pro ATR-based TP a profile guardraily
- Respektuje session okno, páteční cutoff, max open positions a denní limity dané strategií
- Ukládá finální JSON rozhodnutí do `geminipredictions/PREDIKCE_<timestamp>.json`
- Předává hotové parametry exekuční vrstvě

## Konfigurace .env

Relevantní parametry pro risk management:

- `TRADING_MARGIN_THRESHOLD=20` určuje, při jakém poměru efektivní volné marže k efektivnímu balance se spustí trading flow
- `TRADING_ACCOUNT_BALANCE_CAP=5000` určuje maximální balance, se kterou strategie počítá lot sizing a margin check
- `PRIMARY_STRATEGY_ID`, `PRIMARY_STRATEGY_MAGIC` definují identitu hlavní strategie v MT5 objednávkách
- `INDEX_STRATEGY_ENABLED=true` zapíná paralelní indexovou strategii
- `INDEX_STRATEGY_ID`, `INDEX_STRATEGY_MAGIC` definují identitu indexové strategie
- `INDEX_STRATEGY_SYMBOL_WHITELIST` určuje přesný whitelist indexů pro paralelní větev
- `PRIMARY_MAX_TRADES_PER_DAY`, `PRIMARY_MAX_TRADES_PER_SYMBOL_PER_DAY`, `PRIMARY_MAX_OPEN_POSITIONS` určují limity hlavní strategie
- `INDEX_MAX_TRADES_PER_DAY`, `INDEX_MAX_TRADES_PER_SYMBOL_PER_DAY`, `INDEX_MAX_OPEN_POSITIONS` určují limity indexové strategie
- `PRIMARY_SESSION_START_HOUR_UTC`, `PRIMARY_SESSION_END_HOUR_UTC`, `PRIMARY_FRIDAY_CUTOFF_HOUR_UTC` určují časové obchodní okno hlavní strategie
- `INDEX_SESSION_START_HOUR_UTC`, `INDEX_SESSION_END_HOUR_UTC`, `INDEX_FRIDAY_CUTOFF_HOUR_UTC` určují časové obchodní okno indexové strategie
- `PRIMARY_MAX_SPREAD_POINTS` a `INDEX_MAX_SPREAD_POINTS` určují maximální spread pro nový vstup
- `PRIMARY_BALANCE_STEP_USD`, `PRIMARY_LOT_PER_BALANCE_STEP`, `PRIMARY_MAX_LOT_CAP` určují balance-based lot cap hlavní strategie
- `INDEX_BALANCE_STEP_USD`, `INDEX_LOT_PER_BALANCE_STEP`, `INDEX_MAX_LOT_CAP` určují balance-based lot cap indexové strategie
- `PRIMARY_MANAGE_LEGACY_POSITIONS=true` dovolí hlavní strategii převzít ruční nebo legacy pozice bez strategy commentu a s `magic=0`
- `INDEX_MANAGE_LEGACY_POSITIONS=false` nechává indexovou strategii pracovat jen s jejími vlastně označenými pozicemi
- `PRIMARY_MAX_OPEN_POSITIONS=0`, `PRIMARY_MAX_TRADES_PER_DAY=0`, `PRIMARY_MAX_TRADES_PER_SYMBOL_PER_DAY=0` vypínají tyto limity hlavní strategie úplně
- `INDEX_MAX_OPEN_POSITIONS=0`, `INDEX_MAX_TRADES_PER_DAY=0`, `INDEX_MAX_TRADES_PER_SYMBOL_PER_DAY=0` vypínají tyto limity indexové strategie úplně
- `NEWS_FILTER_ENABLED=true` zapíná externí ekonomický news filtr pro nové vstupy
- `NEWS_FILTER_API_URL` je URL ekonomického kalendáře; může používat placeholdery `{from_iso}`, `{to_iso}`, `{from_date}`, `{to_date}`, `{token}`
- `NEWS_FILTER_API_TOKEN`, `NEWS_FILTER_API_TOKEN_HEADER`, `NEWS_FILTER_API_TOKEN_PREFIX` určují autentizaci vůči externímu news API
- `NEWS_FILTER_LOOKBACK_MINUTES` a `NEWS_FILTER_LOOKAHEAD_MINUTES` určují blokované okno kolem zprávy
- `NEWS_FILTER_IMPACTS=high` určuje, jaké síly zpráv blokují nový vstup
- `NEWS_FILTER_SYMBOL_CURRENCIES` mapuje ne-FX symboly na měny pro vyhodnocení news událostí; pro indexy je explicitní mapování doporučené
- `PROFIT_CLEANUP_STRATEGY_ENABLED=true` zapíná minutovou profit cleanup strategii
- `PROFIT_CLEANUP_STRATEGY_DRY_RUN=true` zapíná bezpečný testovací režim bez skutečného zavírání profitních pozic
- `PROFIT_PROTECTION_STRATEGY_ENABLED=true` zapíná profit-only trailing/time exit vrstvu
- `PROFIT_PROTECTION_STRATEGY_DRY_RUN=true` zapíná testovací režim profit protection vrstvy
- `PROFIT_PROTECTION_ACTIVATION_USD` určuje od jakého čistého zisku se začne zamykat profit
- `PROFIT_PROTECTION_RETRACE_RATIO` určuje, jak velký retracement od maxima je ještě tolerovaný před uzavřením ziskové pozice
- `PROFIT_PROTECTION_STALE_HOURS` určuje po kolika hodinách se smí zavřít stále zisková, ale příliš dlouho držená pozice
- `PROFIT_PROTECTION_MAX_HOLD_DAYS` určuje maximální počet dní pro profitní pozici v profit protection vrstvě
- `SWAP_BLOCK_START_HOUR=22`, `SWAP_BLOCK_START_MINUTE=30`, `SWAP_BLOCK_END_HOUR=23`, `SWAP_BLOCK_END_MINUTE=30` definují ruční fallback swap blok okna, pokud MT5 historie neposkytne použitelný rollover čas
- `LOSS_CLEANUP_STRATEGY_ENABLED=true` zapíná denní cleanup strategii
- `LOSS_CLEANUP_STRATEGY_HOUR=12` určuje hodinu pražského času, po které se má cleanup vyhodnotit
- `LOSS_CLEANUP_STRATEGY_MINUTE=45` určuje minutu pražského času, po které se má cleanup vyhodnotit
- `LOSS_CLEANUP_BALANCE_BUFFER_PERCENT=2` určuje procento aktuální raw bilance, které má loss cleanup nechat jako rezervu před zavřením ztrátové pozice
- `LOSS_CLEANUP_STRATEGY_DRY_RUN=true` zapíná bezpečný testovací režim bez skutečného zavírání pozic
- Pokud je skutečný balance nižší než strop, žádná rezerva se neuplatní

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
- `gemini_config.py` - načtení Vertex AI konfigurace (`GOOGLE_APPLICATION_CREDENTIALS`, `VERTEX_AI_PROJECT_ID`, `VERTEX_AI_REGION`, `VERTEX_AI_MODEL`) a model fallback chainu
- `gemini_vertex.py` - sdílený Gemini helper pro structured JSON requesty, Vertex SDK volání, Vertex REST fallback a finální fallback na starší API-key flow
- `gemini_decision.py` - sestavení promptu, finální Gemini decision query a obsluha chyb z Vertex helper vrstvy

**Trading / execution vrstva:**
- `trading_validation.py` - validace symbolu, lot size a marže
- `trade_execution.py` - logování a provedení obchodu na MT5
- `trade_history.py` - čtení historie úspěšných obchodů z CSV
- `strategy_context.py` - značení obchodů a pozic přes `strategy_id` + `magic`
- `strategy_profile.py` - definice profilů hlavní a indexové strategie, session a denních limitů
- `market_regime.py` - programový výpočet market regime a trendových setupů před AI vrstvou
- `profit_protection_strategy.py` - profit-only správa otevřených pozic s retracement a stale-profit logikou
- `news_filter.py` - externí ekonomický kalendář, cachování událostí a blokace nových vstupů kolem high-impact news

Trade log nyní obsahuje i `strategy_id`, `magic` a `lot_source`, takže je zpětně vidět, která strategie obchod otevřela a z jakého zdroje pocházel lot.

### Doporučený `.env` blok

```env
PRIMARY_STRATEGY_ID=gemini_primary
PRIMARY_STRATEGY_MAGIC=234000
PRIMARY_MAX_OPEN_POSITIONS=4
PRIMARY_MAX_TRADES_PER_DAY=6
PRIMARY_MAX_TRADES_PER_SYMBOL_PER_DAY=2
PRIMARY_SESSION_START_HOUR_UTC=6
PRIMARY_SESSION_END_HOUR_UTC=20
PRIMARY_FRIDAY_CUTOFF_HOUR_UTC=16
PRIMARY_MAX_SPREAD_POINTS=35
PRIMARY_BALANCE_STEP_USD=1000
PRIMARY_LOT_PER_BALANCE_STEP=0.01
PRIMARY_MAX_LOT_CAP=0.10

INDEX_STRATEGY_ENABLED=true
INDEX_STRATEGY_ID=gemini_indices
INDEX_STRATEGY_MAGIC=234100
INDEX_STRATEGY_SERVICE_SUBDIR=indices_strategy
INDEX_STRATEGY_SYMBOL_WHITELIST=US100_ecn,US500_ecn,US30_ecn,GER40_ecn,FRA40_ecn,UK100_ecn,JP225_ecn,EU50_ecn,HKD50_ecn,FANG_ecn
INDEX_MAX_OPEN_POSITIONS=2
INDEX_MAX_TRADES_PER_DAY=3
INDEX_MAX_TRADES_PER_SYMBOL_PER_DAY=1
INDEX_SESSION_START_HOUR_UTC=7
INDEX_SESSION_END_HOUR_UTC=19
INDEX_FRIDAY_CUTOFF_HOUR_UTC=15
INDEX_MAX_SPREAD_POINTS=60
INDEX_BALANCE_STEP_USD=1500
INDEX_LOT_PER_BALANCE_STEP=0.01
INDEX_MAX_LOT_CAP=0.10

PROFIT_PROTECTION_STRATEGY_ENABLED=true
PROFIT_PROTECTION_STRATEGY_DRY_RUN=true
PROFIT_PROTECTION_ACTIVATION_USD=0.30
PROFIT_PROTECTION_RETRACE_RATIO=0.55
PROFIT_PROTECTION_STALE_HOURS=12
PROFIT_PROTECTION_MAX_HOLD_DAYS=5

NEWS_FILTER_ENABLED=false
NEWS_FILTER_API_URL=https://financialmodelingprep.com/stable/economic-calendar?from={from_date}&to={to_date}&apikey={token}
NEWS_FILTER_API_TOKEN=
NEWS_FILTER_API_TOKEN_HEADER=Authorization
NEWS_FILTER_API_TOKEN_PREFIX=Bearer 
NEWS_FILTER_TIMEOUT_SECONDS=10
NEWS_FILTER_LOOKBACK_MINUTES=15
NEWS_FILTER_LOOKAHEAD_MINUTES=30
NEWS_FILTER_IMPACTS=high
NEWS_FILTER_SYMBOL_CURRENCIES=US100_ecn:USD,US500_ecn:USD,US30_ecn:USD,GER40_ecn:EUR,FRA40_ecn:EUR,UK100_ecn:GBP,JP225_ecn:JPY,EU50_ecn:EUR,HKD50_ecn:HKD,FANG_ecn:USD
```

Externí news filtr blokuje jen **nové vstupy**. Nezavírá stávající ztrátové pozice a nesahá do profit protection logiky. Pokud je filtrovaný symbol z novinek zablokovaný, AI se k němu vůbec nedostane v predikční fázi; při reuse starších predikcí se blok ještě jednou ověří těsně před finální exekucí.

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
- `OLLAMA_PREDICTION_MAX_AGE_MINUTES` - maximální stáří Ollama predikce, které se ještě považuje za čerstvé
- `OLLAMA_FALLBACK_TO_GEMINI` - při `true` se stará nebo chybějící Ollama predikce nahradí dotazem na Gemini, při `false` se takový instrument ignoruje
- `OLLAMA_GEMINI_FALLBACK_MAX_INSTRUMENTS` - maximální počet instrumentů za cyklus, pro které je povolen Gemini fallback při chybějící nebo staré Ollama predikci
- `GEMINI_FALLBACK_MAX_PARALLEL_REQUESTS` - maximální počet současně běžících Gemini fallback dotazů v jednom cyklu
- `MT5_CRYPTO_SYMBOL_PATTERNS` - wildcard masky symbolů, které mají používat crypto risk profil
- `MT5_CRYPTO_MIN_SIGNAL_PERCENT` - přísnější minimální BUY/SELL confidence pro crypto
- `MT5_CRYPTO_LOT_MULTIPLIER` - násobek Gemini lot size pro crypto exekuci
- `MT5_CRYPTO_MAX_OPEN_POSITIONS` - maximální počet současně otevřených crypto pozic
- `MT5_CRYPTO_TP_DISTANCE_PERCENT` - maximální vzdálenost crypto take-profitu od aktuální tržní ceny
- `MT5_CRYPTO_ALLOW_FULL_TP_MODE` - povoluje použití Gemini TP režimu; u crypto je TP i tak konzervativně omezený na nakonfigurovanou vzdálenost
- Fee kontext v promptu: `0.10 USD` za `0.01` lotu
- Swing kontext v promptu: pozice mohou být otevřené i několik dní, ale cílem je denní ziskovost
- Vertex transport fallback: při chybě typu `httpx.ReadTimeout` bez HTTP statusu se helper pokusí stejný request provést přes přímé Vertex REST API a v logu zapíše `transport="vertex-rest-fallback"`, ale jen pokud není aktivní legacy API-key fallback
- Legacy Gemini fallback: pokud jsou nastavené `GEMINI_API_KEY` a `GEMINI_URL`, helper po první Vertex chybě přeskočí další Vertex pokusy a přejde rovnou na `transport="legacy-gemini-api"`

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
google-auth>=2.38.0
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
- Formát: timestamp, symbol, action, lot_size, lot_source, price, success, error_msg
- Automatické vytvoření hlaviček při prvním použití

### Error Recovery
- Oddělené zachycení network errors (`httpx.HTTPError`) vs obecné výjimky
- Detailní error zprávy v logu i CSV
- Retry mechanismus pro finální decision fázi (max 3 pokusy) s vylučováním symbolu po failed symbol validation, invalid trade parametrech nebo failed trade execution
- Graceful handling při selhání kterékoliv validační fáze

## Poznámky

- Po dokončení trading logiky se skript **automaticky ukončí** (nepokračuje v monitoringu)
- Mezitím běžící jiná logika může vytvářet nové soubory - ty nejsou zpracovány
- Všechny chyby jsou logovány, ale nezastaví zpracování ostatních symbolů
