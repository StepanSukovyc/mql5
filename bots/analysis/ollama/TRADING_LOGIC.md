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

## Role AI

### Gemini

- používá se jako **advisory ranking layer**, ne jako exekuční autorita
- vrací doporučený symbol a směr, případně seřazený shortlist kandidátů a reasoning pro log
- shortlist se ořezává přes `GEMINI_ADVISORY_MAX_CANDIDATES`, takže Gemini neurčuje neomezeně dlouhou kandidátní frontu
- nad stejným stavem se zbytečně neopakuje díky:
  - `GEMINI_DECISION_CACHE_MINUTES`
  - `GEMINI_REJECTION_COOLDOWN_MINUTES`
- perzistentní stav je uložen v `trade_logs/gemini_advisory_state.json`

### Ollama

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
- kandidát spadá do whitelistu a projde mean-reversion pravidly

Používané filtry:

- H4 ADX pod maximem pro range režim
- spread pod limitem
- odchylka od VWAP větší než násobek ATR
- BUY: close pod dolním Bollinger pásmem a velmi nízké RSI2
- SELL: close nad horním Bollinger pásmem a velmi vysoké RSI2
- volitelný news block

Paralelní strategie má vlastní risk profil i vlastní session guardy.

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

`final_decision.py` před pokusem o obchod ověří, jestli je daný profil uvnitř svého okna. Pokud ne, profil se přeskočí a runtime pokračuje bez exekuce tohoto setupu.

Vedle toho dál platí globální swap blok okno z `logika.py`, které zastaví celý trading flow bez ohledu na strategii.

## Ownership a správa pozic

`strategy_context.py` zajišťuje jednotné označení a rozpoznání pozic:

- primární strategie má vlastní `magic` a `strategy_id`
- paralelní strategie má vlastní `magic` a `strategy_id`
- primární strategie může podle konfigurace spravovat i manuální nebo legacy pozice
- paralelní strategie je od legacy správy oddělená

Komentáře obchodů používají marker ve tvaru `ga:<strategy_id>`.

## Správa ztrátových pozic

Runtime obsahuje dvě vzájemně doplňující se vrstvy pro čištění starých ztrátových pozic.

### Denní loss-cleanup strategie (`loss_cleanup_strategy.py`)

- Spouští se jednou denně v nakonfigurovaný čas (výchozí 12:45 Prague time)
- Budget = realizovaný zisk **předchozího dne** z MT5 deal history (profit + swap + commission + fee)
- Po odečtení buffer rezervy (výchozí 2 % balance) hledá jednu ztrátovou pozici starší než 7 dní, která se do budgetu vejde
- Může pozici **skutečně uzavřít** (pokud `LOSS_CLEANUP_STRATEGY_DRY_RUN=false`)

### Měsíční rolling advisory strategie (`monthly_loss_cleanup_strategy.py`)

- Spouští se jednou denně (výchozí 13:00 Prague time)
- Budget je počítán přes **plovoucí okno 30 kalendářních dní**
- `target = effective_active_days × 50 USD`, kde `effective_active_days = max(aktivní_obchodní_dny, MIN_ACTIVE_DAYS)`
- Aktivní obchodní dny = počet unikátních dat v okně s aspoň jedním uzavřeným obchodem (automaticky pokrývá svátky, víkendy i výpadky bota)
- `surplus = realized_profit_30d − target`
- Pokud surplus > 0: greedy výběr ztrátových pozic starších než 30 dní od největší ztráty dolů
- **Nikdy nezavírá pozice** — pouze zapíše doporučení do `trade_logs/monthly_loss_cleanup_recommendations.json`
- Slouží jako podklad pro ruční rozhodnutí operátora

### Konfigurace monthly advisory

| Klíč | Výchozí | Popis |
|---|---|---|
| `MONTHLY_LOSS_CLEANUP_ENABLED` | `true` | Zapnutí/vypnutí |
| `MONTHLY_LOSS_CLEANUP_HOUR` | `13` | Hodina spuštění (Prague) |
| `MONTHLY_LOSS_CLEANUP_MINUTE` | `0` | Minuta spuštění |
| `MONTHLY_LOSS_CLEANUP_DAILY_TARGET_USD` | `50.0` | Denní cíl v USD |
| `MONTHLY_LOSS_CLEANUP_MIN_ACTIVE_DAYS` | `15` | Floor pro výpočet targetu |
| `MONTHLY_LOSS_CLEANUP_MIN_POSITION_AGE_DAYS` | `30` | Minimální věk pozice |

## Logy a stavové soubory

Runtime zapisuje více specializovaných logů:

- `trade_logs/decision_log.jsonl`
- `trade_logs/execution_log.jsonl`
- `trade_logs/risk_log.jsonl`
- `trade_logs/ai_log.jsonl`
- `trade_logs/gemini_advisory_state.json`
- `trade_logs/trade_decision_audit.csv`
- `trade_logs/trade_decision_snapshot.csv`
- `trade_logs/monthly_loss_cleanup_recommendations.json`
- `trade_logs/monthly_loss_cleanup_state.json`

Význam nových CSV souborů:

- `trade_decision_audit.csv`: úplná historie rozhodovacích kroků. Je vidět, kterou strategii runtime zkoušel, pro jaký symbol, jestli obchod provedl, a pokud ne, proč ne.
- `trade_decision_snapshot.csv`: pouze poslední známý stav bez historie. Obsahuje vždy aktuální poslední řádek pro `primary`, `parallel` a případně `cycle`, takže je vhodný pro rychlou ruční kontrolu bez procházení celé historie.

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
- signal rules primární i paralelní strategie
- synthetic risk sizing
- advisory cache a rejection cooldown
- local fallback candidate queue
- session guardy pro strategické profily

## Stručné shrnutí

Aktuální architektura je záměrně konzervativnější než původní Gemini-led flow:

- AI navrhuje, ale lokální pravidla rozhodují
- risk je lokální a deterministický
- paralelní strategie je fallback, ne druhý nezávislý exekuční engine
- opakované Gemini dotazy jsou omezené cache a cooldownem
- obchodování je svázané jak globálním swap blokem, tak session okny jednotlivých strategií