# Trading Logic

## Přehled

Aktuální runtime už není postavený na tom, že Gemini přímo řídí exekuci. Systém je rozdělený do tří lokálních vrstev:

1. **Signal layer**: lokální pravidla ověří, jestli kandidát opravdu odpovídá obchodovatelnému setupu.
2. **Risk layer**: `risk_engine.py` spočítá `lot_size`, syntetický interní stop a lokální `take_profit`.
3. **Execution layer**: MT5 exekuce ověří globální limit otevřených pozic, otevře obchod, zapíše vlastnictví strategie a uloží audit logy.

Na začátku cyklu se načte aktuální počet pozic přímo z MT5. Pokud je počet roven nebo vyšší než `MT5_MAX_OPEN_POSITIONS` (výchozí `17`), přeskočí se načítání predikcí, AI advisory, signal validation i risk calculation pro všechny strategie. Správa již otevřených pozic zůstává aktivní. Stejný limit se znovu ověří těsně před odesláním příkazu brokerovi, aby se pokryl souběh s jiným procesem. Při chybě této finální kontroly se nový obchod z bezpečnostních důvodů neotevře.

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
11. Pokud neuspěje ani reversal a `QUANT_STRATEGY_ENABLED=true`, runtime zkusí čtvrtý quant-math fallback.
12. Pokud neuspěje ani quant a `SCALP_ENABLED=true` a volná marže překračuje `SCALP_ACTIVATION_MARGIN_PERCENT`, runtime zkusí liquidity-sweep scalping fallback.
13. Pokud neuspěje ani scalping a `CHAOTIC_STRATEGY_ENABLED=true`, runtime zkusí poslední Chaotic fallback.

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

## Quant strategie

Quant profil v `quant_math_strategy.py` je čtvrtý fallback. Funguje výhradně na lokálních matematických metrikách bez AI predikcí.

Aktivuje se jen když:

- `QUANT_STRATEGY_ENABLED=true`
- primary, parallel ani reversal strategie neotevřely obchod
- účet splňuje vlastní maržový práh aktivace
- quant profil nepřekročil limit otevřených pozic
- kandidát dosáhne minimálního skóre `QUANT_MIN_SIGNAL_SCORE`

Používané filtry:

- H4 ADX minimum pro trendové prostředí
- spread pod limitem
- EMA vzdálenost jako proxy impulsu
- directional streak a curvature scoring
- volitelný news block

Quant strategie má vlastní risk profil a session guardy. Výstupem je `quant_strategy_status.csv`.

## Liquidity Sweep Scalping strategie

Skalpovací profil v `liquidity_sweep_scalping_strategy.py` je šestý a poslední fallback ve výkonnostním řetězci. Obchoduje výhradně na M5 svíčkách načtených přímo z MT5 přes `copy_rates_from_pos`.

Aktivuje se jen když:

- `SCALP_ENABLED=true`
- žádná předchozí strategie v daném cyklu neotevřela obchod
- volná marže / balance >= `SCALP_ACTIVATION_MARGIN_PERCENT` (výchozí 5 %)
- skalpovací profil nepřekročil `SCALP_MAX_POSITIONS_TOTAL`

Základní obchodní logika (Liquidity Sweep / False Breakout):

- **LONG**: poslední uzavřená svíčka prorazila lokální minimum posledních N svíček LOW cenou a zavřela zpět NAD tímto minimem (odmítnutí nízké ceny — sweep stop-lossů)
- **SHORT**: poslední uzavřená svíčka prorazila lokální maximum HIGH cenou a zavřela zpět POD tímto maximem (odmítnutí vysoké ceny)

Scoring systém (0 – 105 bodů, vstup jen pokud score ≥ `SCALP_MIN_SIGNAL_SCORE`):

| Podmínka | Body |
|---|---|
| Liquidity sweep detekován | +40 |
| Engulfing potvrzení | +20 |
| Silný wick | +15 |
| EMA kontext | +15 |
| ATR filter | +5 |
| Spread filter | +5 |
| Session filter | +5 |

Používané filtry:

- spread pod per-symbol limitem (`SCALP_MAX_SPREAD_{SYMBOL}`)
- ATR v povoleném rozsahu (příliš nízký = stagnace, příliš vysoký = přílišná volatilita)
- session filtr: London (07:00–11:00 UTC) a New York (13:30–17:00 UTC), pátek cutoff 16:00 UTC
- maximálně 1 pozice na symbol (`SCALP_MAX_POSITIONS_PER_SYMBOL`)
- celkový USD exposure limit (`SCALP_MAX_USD_EXPOSURE`)
- denní loss limit (`SCALP_MAX_DAILY_LOSS`) a account drawdown limit

Exit management (bez klasického broker-side stop lossu):

- **Emergency stop reference**: vypočtená úroveň (1.5× ATR od low/high svíčky), kontrolována každý cyklus — urgency=emergency
- **Invalidation level**: logické zneplatnění setupu (cena prolomí úroveň ve špatném směru) — urgency=emergency
- **Max floating loss**: absolutní USD limit na pozici (`SCALP_MAX_FLOATING_LOSS_PER_TRADE`) — urgency=emergency
- **Max holding time**: `SCALP_MAX_HOLDING_BARS × timeframe_minutes` (výchozí 8 svíček × 5 min = 40 min) — urgency=normal
- **Account drawdown**: pokud účet překročí `SCALP_MAX_ACCOUNT_DRAWDOWN_PERCENT` — urgency=emergency

Ochrana duplicitního vstupu:

- `_last_processed_bar_time[symbol]` ukládá Unix timestamp poslední zpracované svíčky
- nová svíčka = nový timestamp → zpracuje se; stejný timestamp → přeskočí se

Persistence:

- metadata pozic (invalidation level, emergency ref, max loss, lot size, score, čas otevření) jsou uložena v `trade_logs/scalping_position_state.json`
- správa pozic (`manage_existing_positions()`) se volá **vždy** při každém cyklu v `final_decision.py`, nezávisle na tom, zda jiné strategie obchodovaly

Skalpovací strategie má vlastní `ScalpingAppContext` (DI kontejner) a `magic` číslo `234600`.

## Chaotic strategie

Chaotic profil je opt-in poslední fallback v `final_decision.py`. Aktivuje se až poté, co žádná předchozí strategie v cyklu neotevřela obchod.

Aktivuje se jen když:

- `CHAOTIC_STRATEGY_ENABLED=true`
- volná marže / balance je přísně mezi `CHAOTIC_MIN_FREE_MARGIN_PERCENT` a `CHAOTIC_MAX_FREE_MARGIN_PERCENT`
- počet otevřených pozic označených `CHAOTIC_STRATEGY_MAGIC` nebo `ga:CHAOTIC_STRATEGY_ID` je nižší než `CHAOTIC_MAX_OPEN_POSITIONS` (výchozí `2`)
- Cloud Ollama vrátí platný symbol a směr z nefiltrovaných AI predikcí

Chaotic záměrně obchází běžné signalové filtry, cooldowny, denní limity, whitelisty a session pravidla ostatních strategií. Neobchází technické ochrany MT5: validaci symbolu, brokerový lot step, dostupnou efektivní marži a platný směr Take Profitu.

Exekuce je TP-only:

- brokerovi se nikdy neposílá Stop Loss
- TP je vzdálen `CHAOTIC_TAKE_PROFIT_ATR_MULTIPLIER × ATR(1H)` od vstupu
- lot se zaokrouhluje dolů tak, aby odhadovaná marže nepřekročila `CHAOTIC_POSITION_MARGIN_PERCENT` efektivního kapitálu
- zdrojová data pro ATR se hledají v Cloud Ollama, archivní i economy složce; bez platných dat se obchod neotevře

Strategie nemá páteční cutoff ani vlastní session okno. Nadále však platí globální večerní swap rollover blok z `logika.py`.

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

### Quant strategie

- `QUANT_SESSION_START_HOUR_UTC`
- `QUANT_SESSION_END_HOUR_UTC`
- `QUANT_FRIDAY_CUTOFF_HOUR_UTC`

### Liquidity Sweep Scalping strategie

Session logika je implementována přímo uvnitř `LiquiditySweepScalpingStrategy.passes_session_filter()` a konfigurována přes:

- `SCALP_SESSION_LONDON_ENABLED` / `SCALP_SESSION_LONDON_START_HOUR_UTC` / `SCALP_SESSION_LONDON_END_HOUR_UTC`
- `SCALP_SESSION_NEWYORK_ENABLED` / `SCALP_SESSION_NEWYORK_START_HOUR_UTC` / `SCALP_SESSION_NEWYORK_END_HOUR_UTC`
- `SCALP_FRIDAY_CUTOFF_HOUR_UTC`

### Chaotic strategie

Chaotic nepoužívá vlastní UTC session ani páteční cutoff. Řídí se pouze globálním swap blok oknem.

`final_decision.py` před pokusem o obchod ověří, jestli je daný profil uvnitř svého okna. Pokud ne, profil se přeskočí a runtime pokračuje bez exekuce tohoto setupu.

Vedle toho dál platí globální swap blok okno z `logika.py`, které zastaví celý trading flow bez ohledu na strategii.

## Ownership a správa pozic

`strategy_context.py` zajišťuje jednotné označení a rozpoznání pozic:

| Strategie | Magic | Strategy ID |
|---|---|---|
| Primární | 234000 | `gemini_primary` |
| Index | 234100 | `gemini_indices` |
| Paralelní | 234200 | `parallel_mean_reversion` |
| Reverzní | 234300 | `reversal_pattern` |
| Quant | 234400 | `quant_math` |
| Cloud Ollama | 234500 | `ollama_cloud_primary` |
| Scalping | 234600 | `liquidity_sweep_scalping` |
| Chaotic | 234700 | `chaotic` |

- primární strategie může podle konfigurace spravovat i manuální nebo legacy pozice
- všechny ostatní strategie jsou od legacy správy oddělené
- scalping strategie ukládá rozšířená metadata pozic (invalidation level, emergency ref) do `scalping_position_state.json`
- chaotic strategie je výchozím stavem vypnutá; běží až jako poslední fallback při volné marži mezi `CHAOTIC_MIN_FREE_MARGIN_PERCENT` a `CHAOTIC_MAX_FREE_MARGIN_PERCENT`. Má nejvýše `CHAOTIC_MAX_OPEN_POSITIONS` současně otevřených pozic, používá nefiltrované AI predikce, posílá pouze Take Profit ve vzdálenosti `CHAOTIC_TAKE_PROFIT_ATR_MULTIPLIER × ATR` a objem omezuje na `CHAOTIC_POSITION_MARGIN_PERCENT` efektivního kapitálu jako odhadovanou požadovanou marži.

Komentáře obchodů používají marker ve tvaru `ga:<strategy_id>`.

## Správa ztrátových pozic

`hybrid_loss_exit_strategy.py` je jediný automatický loss-exit mechanismus. Denní `loss_cleanup_strategy.py`, týdenní `weekly_surplus_cleanup_strategy.py` a měsíční `monthly_loss_cleanup_strategy.py` byly odstraněny spolu s jejich konfigurací.

Hybrid vyhodnocuje pouze ztrátové pozice strategie `chaotic`, otevřené od `HYBRID_EXIT_ANALYSIS_START_DATE`; starší pozice jsou vždy `legacy_excluded`. Analýza běží nejdříve po uplynutí `HYBRID_EXIT_CHECK_INTERVAL_MINUTES` (výchozí 15 minut) a pouze pokud je skutečná volná marže `raw_margin_free / raw_balance` nižší než `HYBRID_EXIT_MAX_FREE_MARGIN_PERCENT` (výchozí 5 %). Pokud marže limit nesplňuje, interval se nespotřebuje a hybrid se vyhodnotí při nejbližším dalším monitoringu po jejím poklesu pod limit. Stáří je počítáno v aktivních obchodních hodinách s výjimkou konfigurovaného FX víkendu. Stará pozice mimo break-even buffer může být uzavřena až při alespoň dvou negativních reason codes z uzavřených H1/H4 svíček. Výchozí `HYBRID_EXIT_DRY_RUN=true` jen loguje rozhodnutí.

Detailní audit zapisuje `trade_logs/hybrid_loss_exit.csv` a `trade_logs/hybrid_loss_exit_events.jsonl`.

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
- `trade_logs/quant_strategy_status.csv`
- `trade_logs/scalping_position_state.json`
- `trade_logs/hybrid_loss_exit.csv`
- `trade_logs/hybrid_loss_exit_events.jsonl`

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
- fallback řetězec má šest vrstev: primary → parallel → reversal → quant → (cloud ollama) → scalping
- scalping je poslední záloha — spouští se jedině pokud žádná předchozí strategie neobchodovala a marže > 5 %
- scalping pracuje přímo s M5 OHLCV daty z MT5, není závislý na AI predikcích ani precomputed indikátorech
- opakované Gemini dotazy jsou omezené cache a cooldownem
- obchodování je svázané jak globálním swap blokem, tak session okny jednotlivých strategií
- správa skalpovacích pozic (exit podmínky) běží vždy při každém cyklu, nezávisle na ostatních strategiích