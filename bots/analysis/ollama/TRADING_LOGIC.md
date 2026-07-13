# Trading Logic

## Přehled

Aktuální runtime už není postavený na tom, že Gemini přímo řídí exekuci. Systém je rozdělený do tří lokálních vrstev:

1. **Signal layer**: lokální pravidla ověří, jestli kandidát opravdu odpovídá obchodovatelnému setupu.
2. **Risk layer**: `risk_engine.py` spočítá `lot_size`, syntetický interní stop a lokální `take_profit`.
3. **Execution layer**: MT5 exekuce otevře obchod, zapíše vlastnictví strategie a uloží audit logy.

Gemini a Ollama jsou teď pomocné predikční vrstvy. Nejsou autoritou pro finální velikost pozice ani pro řízení rizika.

## Hlavní tok runtime

1. `logika.py` hlídá swap blok okno a mimo něj spouští obchodní cyklus.
2. `trading_logic.py` připraví predikce:
   - v economy mode bere čerstvá MT5 data a používá Gemini jen když je potřeba
   - mimo economy mode může znovu použít čerstvé Ollama predikce nebo spadnout na Gemini fallback
3. Slabé predikce se odfiltrují.
4. `final_decision.py` načte stav účtu, otevřené pozice a dostupné predikce.
5. Gemini může vrátit seřazený advisory shortlist kandidátů pro symbol a směr.
6. Runtime zkusí maximálně `GEMINI_ADVISORY_MAX_CANDIDATES` Gemini kandidátů, výchozí hodnota je `3`.
7. Pokud žádný z těchto Gemini kandidátů neprojde validací, cooldownem nebo exekucí, runtime přejde na čistě lokální kandidátní frontu bez Gemini.
8. Zbytek rozhodnutí už běží lokálně nad kandidátní frontou.
9. Pokud primární strategie nenajde proveditelný obchod, runtime může přejít na paralelní mean-reversion strategii.
10. Pokud neuspěje ani paralelní profil a `REVERSAL_STRATEGY_ENABLED=true`, runtime může zkusit třetí reversal-pattern fallback.

## Role AI

### Ollama Cloud (primary advisory)

- pro **finální výběr instrumentu a směru** pro primární strategii je nyní autoritou Ollama Cloud
- volá se přes `_resolve_ollama_cloud_advisory_candidates()` v `final_decision.py`
- vrací seřazený shortlist kandidátů (symbol, action, reasoning) ve stejném formátu jako dříve Gemini
- platí stejné limity: `GEMINI_ADVISORY_MAX_CANDIDATES`, `GEMINI_DECISION_CACHE_MINUTES`, `GEMINI_REJECTION_COOLDOWN_MINUTES`
- konfigurace: `OLLAMA_CLOUD_URL`, `OLLAMA_CLOUD_MODEL`, `OLLAMA_CLOUD_API_KEY`, `OLLAMA_CLOUD_TIMEOUT_SECONDS`

### Gemini

- nadále slouží jako **upstream predikční vrstva** — generuje per-symbol market analýzu (BUY/SELL% + reasoning)
- tyto predikce jsou vstupem pro advisory vrstvu (Ollama Cloud)
- finální výběr (advisory ranking) už Gemini neprovádí
- cache a rejection cooldown jsou sdílené s Ollama Cloud advisory path
- perzistentní stav je uložen v `trade_logs/gemini_advisory_state.json`

### Lokální Ollama

- je volitelný lokální scanner/predikční služba v upstream části toku
- může běžet paralelně, ale nemusí
- pokud má čerstvé predikce, runtime je může znovu použít a omezit počet Gemini dotazů
- neřídí `lot_size`, `take_profit` ani finální trade execution

## Primární strategie

Primární profil je trend-following vrstva nad lokálními pravidly v `signal_rules.py`.

Typické filtry:

- D1 EMA200 směr
- H4 EMA50 vs EMA200
- H1 close vs EMA20
- RSI pásmo
- H4 ADX minimum
- ATR/close minimum
- spread limit
- volitelný news block

Pokud primární strategie kandidáta potvrdí, `risk_engine.py` spočítá:

- risk per trade
- syntetickou stop vzdálenost podle ATR
- výsledný `lot_size`
- lokální `take_profit` jako násobek `R`

Broker-side stop loss se neposílá. Syntetický stop slouží pro sizing, interní kontrolu rizika a logování.

## Paralelní strategie

Paralelní profil v `parallel_strategy_mean_reversion.py` je fallback, ne hlavní tok.

Aktivuje se jen když:

- primární strategie neotevřela obchod
- účet splňuje vlastní maržový práh aktivace
- paralelní profil nepřekročil svůj limit otevřených pozic
- kandidát spadá do povoleného sekundárního universe a projde mean-reversion pravidly

Pravidla pro symbol universe:

- pokud je `PARALLEL_SYMBOL_WHITELIST` vyplněný, parallel strategie smí obchodovat jen symboly z whitelistu
- pokud je `PARALLEL_SYMBOL_WHITELIST` prázdný, parallel strategie smí obchodovat celý non-crypto universe, tedy Forex, indexy a další povolené CFD

Používané filtry:

- H4 ADX pod maximem pro range režim
- spread pod limitem
- odchylka od VWAP větší než násobek ATR
- BUY: close pod dolním Bollinger pásmem a velmi nízké RSI2
- SELL: close nad horním Bollinger pásmem a velmi vysoké RSI2
- volitelný news block

Paralelní strategie má vlastní risk profil i vlastní session guardy.

## Reverzní strategie

Reverzní profil v `reversal_pattern_strategy.py` je třetí fallback vrstva. Nespouští se jako konkurent primary strategie, ale až po primary a paralelní mean reversion větvi.

Aktivuje se jen když:

- `REVERSAL_STRATEGY_ENABLED=true`
- primary ani parallel strategie neotevřely obchod
- účet splňuje vlastní maržový práh aktivace
- reversal profil nepřekročil limit otevřených pozic
- kandidát spadá do povoleného sekundárního universe a projde reverzní pattern validací

Pravidla pro symbol universe:

- pokud je `REVERSAL_SYMBOL_WHITELIST` vyplněný, reversal strategie smí obchodovat jen symboly z whitelistu
- pokud je `REVERSAL_SYMBOL_WHITELIST` prázdný, reversal strategie smí obchodovat celý non-crypto universe, tedy Forex, indexy a další povolené CFD

Používané filtry:

- H4 ADX pod maximem, aby trh nebyl příliš trendový proti reversal vstupu
- spread pod limitem
- pattern musí mít minimální velikost vůči ATR
- svíčka se musí dotknout dolního nebo horního extrému přes Bollinger pásma s tolerancí v ATR
- BUY: bullish engulfing nebo hammer-like pin bar, přeprodané RSI, close stále pod VWAP a dost silná potvrzovací zavírací cena
- SELL: bearish engulfing nebo rejection candle, překoupené RSI, close stále nad VWAP a dost silná potvrzovací zavírací cena
- volitelný news block

Reverzní strategie má vlastní risk profil, session guardy i samostatný status log.

## Session a časová omezení

Každý strategy profile má vlastní UTC obchodní okno.

### Primární strategie

- `PRIMARY_SESSION_START_HOUR_UTC`
- `PRIMARY_SESSION_END_HOUR_UTC`
- `PRIMARY_FRIDAY_CUTOFF_HOUR_UTC`

### Paralelní strategie

- `PARALLEL_SESSION_START_HOUR_UTC`
- `PARALLEL_SESSION_END_HOUR_UTC`
- `PARALLEL_FRIDAY_CUTOFF_HOUR_UTC`

### Reverzní strategie

- `REVERSAL_SESSION_START_HOUR_UTC`
- `REVERSAL_SESSION_END_HOUR_UTC`
- `REVERSAL_FRIDAY_CUTOFF_HOUR_UTC`

`final_decision.py` před pokusem o obchod ověří, jestli je daný profil uvnitř svého okna. Pokud ne, profil se přeskočí a runtime pokračuje bez exekuce tohoto setupu.

Vedle toho dál platí globální swap blok okno z `logika.py`, které zastaví celý trading flow bez ohledu na strategii.

## Ownership a správa pozic

`strategy_context.py` zajišťuje jednotné označení a rozpoznání pozic:

- primární strategie má vlastní `magic` a `strategy_id`
- paralelní strategie má vlastní `magic` a `strategy_id`
- reverzní strategie má vlastní `magic` a `strategy_id`
- primární strategie může podle konfigurace spravovat i manuální nebo legacy pozice
- paralelní a reverzní strategie jsou od legacy správy oddělené

Komentáře obchodů používají marker ve tvaru `ga:<strategy_id>`.

## Správa ztrátových pozic

Runtime obsahuje jednu aktivní strategii pro čištění starých ztrátových pozic a dvě deaktivované legacy vrstvy.

### Týdenní surplus cleanup (`weekly_surplus_cleanup_strategy.py`) — **aktivní**

- Spouští se jednou za ISO týden v nakonfigurovaný den (výchozí pátek) a hodinu UTC (výchozí 15:00)
- Počítá realizovaný P&L celého týdne: pondělí 00:00 UTC → čas spuštění (profit + swap + commission + fee z closing deals)
- **Minimální příjem** = `WEEKLY_CLEANUP_MIN_PROFIT_USD` (fixní USD), nebo auto: `TRADING_ACCOUNT_BALANCE_CAP × WEEKLY_CLEANUP_MIN_INCOME_PERCENT / 100` (výchozí 10 %)
- Pokud `týdenní_zisk < minimum` → přeskočí bez jakékoli akce (minimum je vždy chráněno)
- Pokud `týdenní_zisk ≥ minimum`: `surplus = týdenní_zisk − minimum` = budget na cleanup
- Kandidáti: otevřené pozice starší než `WEEKLY_CLEANUP_MIN_POSITION_AGE_DAYS` dní (výchozí 7), ve ztrátě
- Řazení: nejstarší první, při shodě nejmenší ztráta — maximalizuje počet uzavřených pozic ze stejného budgetu
- Greedy výběr: zavírá pozice dokud budget stačí
- Může pozici **skutečně uzavřít** (pokud `WEEKLY_CLEANUP_DRY_RUN=false`)
- Přeskakuje se během swap rollover blok okna
- Stav (ISO week key) je persistovaný v `trade_logs/weekly_surplus_cleanup_state.json`
- Logy v `trade_logs/weekly_surplus_cleanup.csv`

### Konfigurace weekly surplus cleanup

| Klíč | Výchozí | Popis |
|---|---|---|
| `WEEKLY_CLEANUP_ENABLED` | `true` | Zapnutí/vypnutí |
| `WEEKLY_CLEANUP_DRY_RUN` | `true` | Bezpečný start — pouze loguje, nezavírá |
| `WEEKLY_CLEANUP_RUN_WEEKDAY` | `4` | Den spuštění (0=Po … 4=Pá) |
| `WEEKLY_CLEANUP_RUN_HOUR_UTC` | `15` | Hodina UTC |
| `WEEKLY_CLEANUP_MIN_PROFIT_USD` | `0` | Fixní minimum v USD (0 = použij % výpočet) |
| `WEEKLY_CLEANUP_MIN_INCOME_PERCENT` | `10.0` | % z `TRADING_ACCOUNT_BALANCE_CAP` |
| `WEEKLY_CLEANUP_MIN_POSITION_AGE_DAYS` | `7` | Minimální věk pozice |

### Denní loss-cleanup strategie (`loss_cleanup_strategy.py`) — **deaktivována**

- `LOSS_CLEANUP_STRATEGY_ENABLED=false`
- Nahrazena týdenní surplus cleanup strategií

### Měsíční rolling advisory strategie (`monthly_loss_cleanup_strategy.py`) — **deaktivována**

- `MONTHLY_LOSS_CLEANUP_ENABLED=false`
- Nahrazena týdenní surplus cleanup strategií

## Logy a stavové soubory

Runtime zapisuje více specializovaných logů:

- `trade_logs/decision_log.jsonl`
- `trade_logs/execution_log.jsonl`
- `trade_logs/risk_log.jsonl`
- `trade_logs/ai_log.jsonl`
- `trade_logs/gemini_advisory_state.json`
- `trade_logs/trade_decision_audit.csv`
- `trade_logs/trade_decision_snapshot.csv`
- `trade_logs/parallel_strategy_status.csv`
- `trade_logs/reversal_strategy_status.csv`
- `trade_logs/weekly_surplus_cleanup.csv`
- `trade_logs/weekly_surplus_cleanup_state.json`
- `trade_logs/monthly_loss_cleanup_recommendations.json`
- `trade_logs/monthly_loss_cleanup_state.json`

Význam nových CSV souborů:

- `trade_decision_audit.csv`: úplná historie rozhodovacích kroků. Je vidět, kterou strategii runtime zkoušel, pro jaký symbol, jestli obchod provedl, a pokud ne, proč ne.
- `trade_decision_snapshot.csv`: pouze poslední známý stav bez historie. Obsahuje vždy aktuální poslední řádek pro `primary`, `parallel`, `reversal` a případně `cycle`, takže je vhodný pro rychlou ruční kontrolu bez procházení celé historie.
- `parallel_strategy_status.csv` a `reversal_strategy_status.csv`: poslední detailní stav každé sekundární strategie, včetně root rejection důvodu, rule failures a cooldown expiry.

V audit logu jsou teď navíc vidět i pomocné přechodové informace:

- `candidate_queue`: jestli byl kandidát z Gemini nebo z lokálního rankingu
- `candidate_rank`: pořadí kandidáta v dané frontě
- `queue_transition` s důvodem `gemini_candidates_exhausted_local_fallback`: okamžik, kdy runtime po vyčerpání Gemini shortlistu přešel na local-only flow

Tím je oddělené:

- co navrhla AI
- co schválila lokální pravidla
- jak byl spočten risk
- co bylo skutečně exekuováno

## Testy

Všechny testy pro tento Python trading stack jsou nově v `bots/analysis/ollama/tests/`.

Důležité pokrytí:

- indikátory a market data payload
- signal rules primární, paralelní i reverzní strategie
- synthetic risk sizing
- advisory cache a rejection cooldown
- local fallback candidate queue
- session guardy pro strategické profily

## Stručné shrnutí

Aktuální architektura je záměrně konzervativnější než původní Gemini-led flow:

- AI navrhuje, ale lokální pravidla rozhodují
- risk je lokální a deterministický
- paralelní i reverzní strategie jsou fallback vrstvy, ne samostatné nezávislé exekuční enginy
- opakované Gemini dotazy jsou omezené cache a cooldownem
- obchodování je svázané jak globálním swap blokem, tak session okny jednotlivých strategií