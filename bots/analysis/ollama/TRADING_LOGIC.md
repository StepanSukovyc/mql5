# Trading Logic

## Přehled

Aktuální runtime už není postavený na tom, že Gemini přímo řídí exekuci. Systém je rozdělený do tří lokálních vrstev:

1. **Signal layer**: lokální pravidla ověří, jestli kandidát opravdu odpovídá obchodovatelnému setupu.
2. **Risk layer**: `risk_engine.py` spočítá `lot_size`, syntetický interní stop a lokální `take_profit`.
3. **Execution layer**: MT5 exekuce ověří globální limit otevřených pozic, otevře obchod, zapíše vlastnictví strategie a uloží audit logy.

Na začátku cyklu se načte aktuální počet pozic přímo z MT5. Pokud je počet roven nebo vyšší než `MT5_MAX_OPEN_POSITIONS` (výchozí `17`), přeskočí se načítání predikcí, AI advisory, signal validation i risk calculation pro všechny strategie. Správa již otevřených pozic zůstává aktivní. Stejný limit se znovu ověří těsně před odesláním příkazu brokerovi, aby se pokryl souběh s jiným procesem. Při chybě této finální kontroly se nový obchod z bezpečnostních důvodů neotevře.

Gemini a Ollama dodávají predikce a advisory ranking. Cloud Ollama má navíc vlastní vstupní strategický profil, ale ani v něm AI neurčuje finální velikost pozice ani řízení rizika.

Pořadí obchodu nepřepíná režim AI ani výpočet lotu a TP. Původní `GEMINI_FULL_CONTROL_EVERY_N_TRADES` byla odstraněna z runtime; případná položka ve starší konfiguraci se ignoruje.

## Automatický balance cap

Pro výpočet dostupné marže strategie používá aktuální balance zaokrouhlený dolů na celé tisíce USD. Například balance `11125` vytvoří strategický cap `11000`; zbylých `125` USD je bezpečnostní rezerva. Při balance `9999` je cap `9000` a rezerva `999` USD.

Výpočet v [`account_state.py`](account_state.py) je `max(floor(raw_balance / 1000) × 1000, 0)`. Tento cap je efektivní `balance` ve strategickém stavu účtu; rezerva se odečte z raw free margin před kontrolou požadované marže objednávky. Při balance pod `1000` je cap nulový.

`TRADING_ACCOUNT_BALANCE_CAP` runtime již nečte: nejde o aktivní konfigurační proměnnou a její případné nastavení nemění cap. V aktuálních `bots/analysis/ollama/.env` a `.env.example` už není. Hodnota `TRADING_ACCOUNT_BALANCE_CAP=11000` zůstává v podkladové kopii [`tasks/strategie/podklady/.env`](../../../tasks/strategie/podklady/.env), zatímco [`tasks/aktualni/.env`](../../../tasks/aktualni/.env) obsahuje `10000`. Tyto položky i komentáře odvozující limity od této proměnné jsou historické, nikoli popis aktuálního výpočtu.

Při nedostatečné marži se `trade_execution.py` pokusí nejvýše šestkrát přepočítat menší objem podle aktuálně dostupné marže a znovu ho ověří proti brokerovu minimu, maximu a kroku objemu. Každý pokus ponechává pětiprocentní rezervu. Stejný omezený postup se použije při brokerově `NO_MONEY` odmítnutí; při jiné chybě se neopakuje. Pokud by menší objem klesl pod brokerovo minimum, obchod se odmítne. Tato úprava nemění ani nepřidává broker-side stop loss.

## Maržové prahy: probuzení cyklu versus aktivace strategií

`TRADING_MARGIN_THRESHOLD` a `TRADING_TRIGGER_MARGIN_THRESHOLD` se stále používají, ale nejsou zaměnitelné. Hodnoty se zadávají v procentech: `5` znamená 5 %, nikoli poměr `5.0`.

### `TRADING_TRIGGER_MARGIN_THRESHOLD`: brána hlavního plánovače

[`check_stop_condition()`](account_monitor.py) ukončí čekací monitor a nastaví `trading_trigger_event`, pokud:

`raw_margin_free / balance × 100 > TRADING_TRIGGER_MARGIN_THRESHOLD`

`balance` je automaticky zaokrouhlený strategický cap, nikoli raw balance nebo equity. Při nulovém capu helper vrací poměr `0`. Používá se skutečná raw free margin bez odečtení rezervy; na kontrolu marže konkrétní objednávky se rezerva nadále vztahuje.

[`logika.py`](logika.py) čeká na tento event před přípravou predikcí a finální rozhodovací fází. Splnění triggeru tedy pouze dovolí zahájit rozhodovací cyklus, nezaručuje otevření obchodu a nenahrazuje maržové, signální, session ani poziční guardy jednotlivých strategií. Správa již otevřených pozic běží nezávisle.

V aktuální `.env` je `TRADING_TRIGGER_MARGIN_THRESHOLD=5`. Při capu `11000` se monitor probudí až při raw free margin **nad `550`**, nikoli při přesných `550`. Vyšší trigger může zadržet celý vstupní cyklus, i když některá fallback strategie už splňuje svůj nižší práh.

Pokud proměnná chybí nebo nejde převést na číslo, `_get_margin_threshold()` použije minimum prahů Primary a zapnutých profilů Parallel, Reversal a Scalping. **Cloud Ollama, Gemini Prediction a Quant tento automatický výběr nezahrnuje**; nejde tedy obecně o minimum všech sedmi vstupních profilů.

### `TRADING_MARGIN_THRESHOLD`: výchozí práh Primary

[`get_primary_strategy_context()`](strategy_context.py) čte `TRADING_MARGIN_THRESHOLD` jako výchozí hodnotu pro `PRIMARY_STRATEGY_ACTIVATION_MARGIN_PERCENT`. Výchozí hodnota při chybějícím/nečíselném nastavení je `20`.

Pokud je `PRIMARY_STRATEGY_ACTIVATION_MARGIN_PERCENT` explicitně a platně nastavený, má přednost. Aktuální `.env` obsahuje obě hodnoty `20`, takže změna samotného `TRADING_MARGIN_THRESHOLD` nyní primární práh nezmění. Bez explicitního primárního nastavení se jeho změna přenese také na odvozené prahy Parallel, Reversal, Quant a na výchozí práh Cloud Ollama.

Vstupní profily kontrolují stejný maržový poměr `raw_margin_free / balance × 100`, ale k aktivaci jim stačí **`>=` jejich vlastní práh**. Aktuální konfigurace dává:

| Profil | Maržový práh | Odvození |
|---|---|---|
| Cloud Ollama | 20 % | `OLLAMA_CLOUD_STRATEGY_ACTIVATION_MARGIN_PERCENT` není v aktuální `.env` nastavený, dědí Primary |
| Primary | 20 % | explicitní `PRIMARY_STRATEGY_ACTIVATION_MARGIN_PERCENT=20` |
| Parallel | 15 % | `max(Primary − PARALLEL_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT, 0)` = `20 − 5` |
| Reversal | 13 % | `max(Primary − REVERSAL_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT, 0)` = `20 − 7` |
| Quant | 10 % | `max(Primary − QUANT_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT, 0)` = `20 − 10` |
| Scalping | 5 % | samostatné `SCALP_ACTIVATION_MARGIN_PERCENT=5.0` |

Například při capu `11000` a raw free margin `1100` (10 %) trigger `5` cyklus probudí, ale Cloud Ollama, Primary, Parallel a Reversal mají maržovou bránu zavřenou. Quant a Scalping mohou splnit maržovou aktivaci; stále musí projít všemi ostatními podmínkami. Tyto prahy nejsou brokerův „margin level“ (`equity / used margin`) ani velikost rizika na obchod.

## Hlavní tok runtime

Pořadí pokusů o nový vstup v [`make_final_trading_decision()`](final_decision.py) se při každém cyklu určuje podle historických realizovaných ztrát aktivních profilů; není pevně dané.

Při `OLLAMA_CLOUD_ENABLED=true` dodává jednotlivé symbolové predikce Ollama Cloud. Profily `ollama_cloud` a `gemini_prediction` používají stejnou sadu predikcí, ale vlastní finální advisory: Ollama Cloud, respektive Gemini. Při vypnuté Cloud službě zůstává dostupná starší cesta `trading_logic.py` s Gemini predikcemi. `PARALLEL_STRATEGY_ENABLED=false` vypíná mean-reversion profil.

Při řazení se sečtou absolutní hodnoty záporného `net_profit` uzavřených pozic ze všech dostupných řádků v `trade_logs/closed_trade_outcomes.csv`. `net_profit` zahrnuje zisk/ztrátu, komisi, swap i poplatky. Jde o výsledky, které outcome tracker uložil; starší uzavřené MT5 pozice, které v CSV nejsou, se automaticky nedopočítávají. Nižší celková ztráta znamená vyšší prioritu. Profil bez historie má ztrátu `0`; při stejné ztrátě rozhoduje vzestupné abecední pořadí `strategy_id` bez ohledu na velikost písmen. Pokud soubor s výsledky chybí, aktivní profily se tedy seřadí abecedně. Pořadí i ztráta každého profilu se zapisují jako událost `strategy_priority_order` do `decision_log.jsonl`.

Jde o sedm možných vstupních profilů, nikoli o pořadí volání AI. Vypnuté profily se do řazení nezařadí; kandidáti a aktivační podmínky se nadále kontrolují samostatně při pokusu. Po prvním úspěšném otevření obchodu rozhodovací funkce vrátí `True` a další profily se v daném cyklu nezkoušejí.

1. `logika.py` hlídá globální časové blokace a limit otevřených pozic. Při `OLLAMA_CLOUD_ENABLED=true` běží cloudová predikční služba ve vlastním vlákně.
2. Pokud je Cloud Ollama zapnutá, plánovač použije JSON predikce z `ollama_cloud/predikce` jako společný vstup pro rozhodovací profily a nevolá per-symbol Gemini predikce. Gemini se použije pouze pro finální advisory. Bez Cloud služby zůstává původní cesta přes `trading_logic.py`.
3. Plánovač volá `final_decision.py`, pokud má běžnou predikční složku nebo cloudové predikce. Funkce načte otevřené pozice, provede správu skalpovacích pozic, ověří globální limit a načte kandidáty i stav účtu.
4. Aktivní profily se seřadí podle součtu historických realizovaných ztrát a při shodě podle `strategy_id`.
5. **Cloud Ollama** při zapnutí a dostupných cloudových predikcích zkusí vlastní trend-following profil. Cloudový advisory shortlist doplní lokální ranking těchto predikcí.
6. **Gemini Prediction** používá symbolové predikce z Cloud Ollama a Gemini advisory ranking. Má stejný trend-following validator a risk/session profil jako Cloud Ollama, ale samostatnou konfiguraci a ownership.
7. **Primary (`gemini_primary`)** používá vybranou symbolovou sadu a Gemini advisory ranking. Cloud profil si přitom zachovává vlastní Ollama advisory a samostatné strategy ID.
8. **Parallel** a **Reversal** používají společnou kandidátní frontu: běžné predikce s advisory rankingem, nebo při jejich absenci lokální ranking cloudových predikcí.
9. **Quant** používá vlastní kandidáty z raw market dat, nikoli AI shortlist.
10. **Scalping** používá vlastní M5 data z MT5. Vyžaduje mimo jiné maržový práh alespoň `SCALP_ACTIVATION_MARGIN_PERCENT` a volný limit skalpovacích pozic.

Pokud je `final_decision.py` zavolána bez použitelných běžných i cloudových predikcí a bez quant kandidátů, může zkusit rovnou scalping. To neznamená, že jej hlavní plánovač volá v každém cyklu bez predikcí: bez běžné predikční složky i cloudových JSON souborů rozhodovací fázi přeskočí.

Po každé vyzkoušené strategii se v konzoli zobrazí souhrn zamítnutých a přeskočených kandidátů, počty jednotlivých důvodů a přechod na další strategii. Stejná data se ukládají do `trade_logs/decision_log.jsonl` jako `strategy_attempt_summary` s `reason_counts` a `fallback_to`. Po úspěšném obchodu se další strategie nezkouší; pokud neuspěje ani poslední, výpis oznámí vyčerpání fallbacku. Jeden kandidát může nesplnit více pravidel, takže součet počtů důvodů může být vyšší než počet kandidátů. Pořadí ani vstupní a rizikové filtry se tím nemění.

## Role AI

### Ollama Cloud (vlastní strategie i advisory)

- **Vlastní vstupní profil** `ollama_cloud` se při `OLLAMA_CLOUD_ENABLED=true` řadí podle své historické ztráty. Čte predikce ze `SERVICE_DEST_FOLDER/ollama_cloud/predikce`, používá trend-following validaci a vlastní risk, session, cooldown a ownership konfiguraci.
- **Advisory služba** `_resolve_ollama_cloud_advisory_candidates()` poskytuje finální ranking pouze cloudovému profilu `ollama_cloud`.
- `OLLAMA_CLOUD_ENABLED` řídí cloudovou predikční službu a aktivaci vstupního profilu. Když je Cloud zapnutá, její per-symbol predikce jsou také vstupem Gemini profilů.
- Shortlist obsahuje symbol, action a reasoning. Není autoritou pro konečný vstup: kandidáta mohou odmítnout lokální filtry, cooldown, risk nebo exekuce. Za shortlist se připojí lokálně seřazení kandidáti bez duplicit symbol/směr.
- Historicky pojmenované `GEMINI_ADVISORY_MAX_CANDIDATES` (výchozí `3`) omezuje počet vrácených cloudových advisory kandidátů, nikoli celkový počet lokálních kandidátů. `GEMINI_DECISION_CACHE_MINUTES` řídí advisory cache a `GEMINI_REJECTION_COOLDOWN_MINUTES` krátký cooldown po chybě exekuce.
- Běžná odmítnutí kandidáta (signálové filtry, limity, chybějící data nebo otevřená pozice na symbolu) se znovu vyhodnocují v dalším cyklu. Cooldown se ukládá zvlášť pro strategy ID, symbol a směr pouze po selhání exekuce; výchozí `GEMINI_REJECTION_COOLDOWN_MINUTES=5` omezuje opakování neúspěšného příkazu. Původní důvod a expirace jsou v `trade_logs/gemini_advisory_state.json`; starší cooldowny z jiných důvodů se ignorují a odstraní při kontrole kandidáta.
- API konfigurace: `OLLAMA_CLOUD_URL`, `OLLAMA_CLOUD_MODEL`, `OLLAMA_CLOUD_API_KEY`, `OLLAMA_CLOUD_TIMEOUT_SECONDS`.

### Gemini Prediction

- Samostatný vstupní profil `gemini_prediction` je řízen `GEMINI_PREDICTION_ENABLED`.
- Při zapnuté Cloud službě používá její per-symbol predikce a `_resolve_gemini_advisory_candidates()` pro finální výběr; nevytváří další Gemini predikci pro každý symbol.
- Trend-following validace, risk výpočet a výchozí obchodní okno odpovídají Cloud Ollama profilu. Má vlastní strategy ID `gemini_prediction`, magic `234700` a rejection cooldown.

### Gemini

- konfigurace pro Argon: `VERTEX_AI_MODEL=gemini-4-argon`, `VERTEX_AI_REGION=eu`; SDK vyžaduje `google-genai>=1.73.1`. Pro multi-region `eu`/`us` používá SDK i vlastní Vertex REST cesta host `aiplatform.<location>.rep.googleapis.com`
- Argon dostává `thinking_level=MEDIUM`, bez nepodporovaného `thinking_budget`. Pro predikci i advisory má limit alespoň `8192` výstupních tokenů včetně thinking; nejde o záruku dokončení každého reasoning požadavku. Gemini 2.5 Flash zachovává původní SDK budget `0` a limity `512`/`768`
- primární pokus používá nakonfigurovaný Vertex model. Pokud jsou nastavené `GEMINI_API_KEY` a `GEMINI_URL`, při chybě následuje API-key REST fallback (aktuálně Gemini 2.5 Flash), který nezaručuje evropské zpracování. Bez obou hodnot zůstávají retry a případný REST fallback na Vertex se service account autentizací
- úspěšný SDK požadavek loguje skutečný `model`, `region`, `transport=google-genai-sdk`, thinking nastavení a tokenový limit; API-key fallback loguje `transport=legacy-gemini-api`. Pouhý úspěch cyklu tedy není důkaz použití Argonu
- selhání každého Vertex/REST/API-key transportního pokusu se zapisuje do `trade_logs/gemini_request_errors.jsonl` včetně statusu, modelu, regionu a diagnostiky; konečné selhání advisory se navíc zapisuje do `trade_logs/gemini_decision_failures.jsonl`
- HTTP 429 už nezpůsobí lokální pozastavení Gemini do půlnoci; další požadavky může nadále odmítnout samotný Google Cloud/Vertex kvůli svým limitům
- nadále slouží jako **upstream predikční vrstva** — generuje per-symbol market analýzu (BUY/SELL% + reasoning)
- tyto predikce jsou vstupem pro `gemini_prediction` a primární profil; vlastní Cloud Ollama profil používá své cloudové predikce
- finální výběr (advisory ranking) už Gemini neprovádí
- advisory cache je oddělená podle poskytovatele; stav cooldownu je v témže souboru, ale klíčuje se podle strategie, symbolu a směru
- perzistentní stav je uložen v `trade_logs/gemini_advisory_state.json`

### Lokální Ollama

- je volitelný lokální scanner/predikční služba v upstream části toku
- může běžet paralelně, ale nemusí
- pokud má čerstvé predikce, runtime je může znovu použít a omezit počet Gemini dotazů
- neřídí `lot_size`, `take_profit` ani finální trade execution

## Primární strategie

Primární profil `primary` je jeden ze vstupních profilů a jeho aktuální pořadí závisí na historické ztrátě. Název „primární“ neznamená pevné místo v pořadí. Je to trend-following vrstva nad lokálními pravidly v `signal_rules.py`, s vlastním ownership `gemini_primary`, odlišným od `ollama_cloud_primary` i `gemini_prediction`.

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

Při zapnutém `VIRTUAL_STOP_LOSS_ENABLED` se u nových čtvrtečních/pátečních vstupů (Praha) nejprve uloží nouzový ATR stop. Gemini poté z aktuálních svíček a indikátorů předpoví absolutní cenu virtuálního SL a zdůvodní ji. AI nesmí zvětšit riziko nad nouzový limit; při nedostupnosti AI, neúplných datech či neplatné odpovědi zůstane nouzový stop a uloží se chyba. Cenu hlídá minutový monitor, nikoli další AI dotaz. Již evidované úrovně se nemění, starší neevidované pozice se nepřebírají. Nejde o garantovaný limit ztráty: monitor musí běžet a nadále platí swapové blokovací okno a riziko skluzu. Ostré uzavírání vyžaduje `VIRTUAL_STOP_LOSS_DRY_RUN=false` a po změně kódu či konfigurace restart procesů.

## Index profil: ownership a výstupy, nikoli samostatná vstupní strategie

Přítomnost `INDEX_STRATEGY_ENABLED=true` v `.env` neznamená, že aktuální runtime spouští samostatnou index strategii. V [`final_decision.py`](final_decision.py) není indexový `StrategyExecutionProfile`, indexový signal validator ani pokus o indexový vstup. Index proto není jedním ze vstupních profilů řazených podle historických ztrát a nemá vlastní implementovanou BUY/SELL signální logiku.

Co aktuální kód skutečně používá:

- [`get_index_strategy_context()`](strategy_context.py) načítá identitu `INDEX_STRATEGY_ID` (výchozí `gemini_indices`), magic `INDEX_STRATEGY_MAGIC` (výchozí `234100`) a `INDEX_MANAGE_LEGACY_POSITIONS` (aktuálně `false`). Pozice s tímto magic nebo odpovídajícím komentářem se rozpoznají jako indexové; primary je nepřebírá jako manuální/legacy pozice.
- [`get_profit_protection_contexts()`](profit_protection_strategy.py) zahrnuje index kontext bez podmínky `INDEX_STRATEGY_ENABLED`. Již otevřené indexové pozice bez broker-side TP tedy může spravovat obecná profit protection, pokud je tato ochrana zapnutá. Platí její společná pravidla čistého zisku a držení pozice, nikoli zvláštní indexové signály. Pozice s TP tato ochrana nepřebírá.
- [`trade_outcomes.py`](trade_outcomes.py) rozpoznává výchozí magic `234100` jako `gemini_indices` při evidenci výsledků uzavřených obchodů.

Rozdíl mezi konfigurací a zapojením do runtime:

| Parametry / aktuální hodnoty v `.env` | Skutečné použití |
|---|---|
| `INDEX_STRATEGY_ENABLED=true` | Getter přepínače v `profit_protection_strategy.py` nemá volajícího. Nezapíná vstupní strategii ani nepodmiňuje zařazení do profit protection. |
| `INDEX_SESSION_START_HOUR_UTC=7`, `INDEX_SESSION_END_HOUR_UTC=19`, `INDEX_FRIDAY_CUTOFF_HOUR_UTC=15` | Načítají se do index kontextu, ale bez indexové vstupní větve se jako indexové vstupní session guardy nepoužijí. Neomezují obecnou profit protection na toto okno. |
| `INDEX_MAX_OPEN_POSITIONS=0`, `INDEX_STRATEGY_ACTIVATION_MARGIN_PERCENT` | Kontext je umí načíst, ale pro samostatný indexový vstup se nekontrolují, protože takový vstup není zapojený. |
| `INDEX_STRATEGY_SYMBOL_WHITELIST` | Čte jej klasifikační helper `is_index_symbol()` v `instrument_utils.py`. Tento helper má aktuálně volající jen v testech, nikoli v živém rozhodovacím toku; whitelist tedy není aktivním indexovým vstupním filtrem. |
| `INDEX_MAX_SPREAD_POINTS=60`, `INDEX_MAX_LOT_CAP=0.10`, `INDEX_BALANCE_STEP_USD=1500`, `INDEX_LOT_PER_BALANCE_STEP=0.01` | Aktuální runtime tyto proměnné nečte. Nejde o vynucené limity indexových obchodů otevíraných jinými profily. |
| `INDEX_MAX_TRADES_PER_DAY=0`, `INDEX_MAX_TRADES_PER_SYMBOL_PER_DAY=0`, `INDEX_STRATEGY_SERVICE_SUBDIR=indices_strategy` | Aktuální runtime je nečte; samostatný indexový plánovač ani výstupní složku podle nich nevytváří. |

Konfigurovaný whitelist je `US100_ecn, US500_ecn, US30_ecn, GER40_ecn, FRA40_ecn, UK100_ecn, JP225_ecn, EU50_ecn, HKD50_ecn, FANG_ecn, USOIL_ecn`. Jde o konfigurovaný seznam, nikoli důkaz samostatného obchodování těchto symbolů; obsahuje také ropu `USOIL_ecn`.

Indexové instrumenty mohou obchodovat ostatní vstupní profily, pokud jsou v jejich kandidátech a projdou jejich pravidly. Takový obchod má ownership, session, spread a sizing daného profilu, nikoli automaticky `gemini_indices` ani limity `INDEX_*`. Samotné vypnutí `INDEX_STRATEGY_ENABLED` proto nezakazuje ostatním profilům obchodovat indexy a nevypíná správu již rozpoznaných indexových pozic.

## Paralelní strategie

Paralelní profil v `parallel_strategy_mean_reversion.py` je samostatný vstupní profil, jehož pořadí určuje historická ztráta. Nespouští se souběžně s ostatními profily. Lze jej vypnout přes `PARALLEL_STRATEGY_ENABLED=false`.

Aktivuje se jen když:

- žádný dříve seřazený profil neotevřel obchod
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

Reverzní profil v `reversal_pattern_strategy.py` je samostatný vstupní profil, jehož pořadí určuje historická ztráta. Nespouští se souběžně s ostatními profily.

Aktivuje se jen když:

- `REVERSAL_STRATEGY_ENABLED=true`
- žádný dříve seřazený profil neotevřel obchod
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

Quant profil v `quant_math_strategy.py` je samostatný vstupní profil, jehož pořadí určuje historická ztráta. Funguje výhradně na lokálních matematických metrikách bez AI predikcí.

Aktivuje se jen když:

- `QUANT_STRATEGY_ENABLED=true`
- žádný dříve seřazený profil neotevřel obchod
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

Skalpovací profil v `liquidity_sweep_scalping_strategy.py` je samostatný vstupní profil, jehož pořadí určuje historická ztráta. Obchoduje výhradně na M5 svíčkách načtených přímo z MT5 přes `copy_rates_from_pos`.

Aktivuje se jen když:

- `SCALP_ENABLED=true`
- žádný dříve seřazený profil neotevřel obchod
- `raw_margin_free / balance × 100 >= SCALP_ACTIVATION_MARGIN_PERCENT` (výchozí 5 %); `balance` je automatický cap a při nulovém capu se profil neaktivuje. Aktivační kontrola scalping používá raw free margin, zatímco kontrola marže objednávky odečítá rezervu
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

## Session a časová omezení

Každý strategy profile má vlastní obchodní okno. Začátek lze nastavit v čase `Europe/Prague`; tento čas automaticky přechází mezi CET a CEST. Konec okna a páteční cutoff zůstávají v UTC.

### Cloud Ollama strategie

- `OLLAMA_CLOUD_SESSION_START_HOUR_PRAGUE`
- `OLLAMA_CLOUD_SESSION_START_MINUTE_PRAGUE`
- `OLLAMA_CLOUD_SESSION_END_HOUR_UTC`
- `OLLAMA_CLOUD_FRIDAY_CUTOFF_HOUR_UTC`

### Primární strategie

- `PRIMARY_SESSION_START_HOUR_PRAGUE`
- `PRIMARY_SESSION_START_MINUTE_PRAGUE`
- `PRIMARY_SESSION_END_HOUR_UTC`
- `PRIMARY_FRIDAY_CUTOFF_HOUR_UTC`

### Paralelní strategie

- `PARALLEL_SESSION_START_HOUR_PRAGUE`
- `PARALLEL_SESSION_START_MINUTE_PRAGUE`
- `PARALLEL_SESSION_END_HOUR_UTC`
- `PARALLEL_FRIDAY_CUTOFF_HOUR_UTC`

### Reverzní strategie

- `REVERSAL_SESSION_START_HOUR_PRAGUE`
- `REVERSAL_SESSION_START_MINUTE_PRAGUE`
- `REVERSAL_SESSION_END_HOUR_UTC`
- `REVERSAL_FRIDAY_CUTOFF_HOUR_UTC`

### Quant strategie

- `QUANT_SESSION_START_HOUR_PRAGUE`
- `QUANT_SESSION_START_MINUTE_PRAGUE`
- `QUANT_SESSION_END_HOUR_UTC`
- `QUANT_FRIDAY_CUTOFF_HOUR_UTC`

### Index strategie

- `INDEX_SESSION_START_HOUR_PRAGUE`
- `INDEX_SESSION_START_MINUTE_PRAGUE`
- `INDEX_SESSION_END_HOUR_UTC`
- `INDEX_FRIDAY_CUTOFF_HOUR_UTC`

### Liquidity Sweep Scalping strategie

Session logika je implementována přímo uvnitř `LiquiditySweepScalpingStrategy.passes_session_filter()` a konfigurována přes:

- `SCALP_SESSION_LONDON_ENABLED` / `SCALP_SESSION_LONDON_START_HOUR_PRAGUE` / `SCALP_SESSION_LONDON_START_MINUTE_PRAGUE` / `SCALP_SESSION_LONDON_END_HOUR_UTC`
- `SCALP_SESSION_NEWYORK_ENABLED` / `SCALP_SESSION_NEWYORK_START_HOUR_PRAGUE` / `SCALP_SESSION_NEWYORK_START_MINUTE_PRAGUE` / `SCALP_SESSION_NEWYORK_END_HOUR_UTC`
- `SCALP_FRIDAY_CUTOFF_HOUR_UTC`

Kompatibilita je zachována: pokud není nastaven pražský začátek, runtime dál čte původní proměnnou `*_SESSION_START_HOUR_UTC`.

Gemini prediction profil přebírá začátek z Cloud Ollama profilu, pokud nemá vlastní `GEMINI_PREDICTION_SESSION_START_HOUR_PRAGUE` a `GEMINI_PREDICTION_SESSION_START_MINUTE_PRAGUE`.

`final_decision.py` před pokusem o obchod ověří, jestli je daný profil uvnitř svého okna. Pokud ne, profil se přeskočí a runtime pokračuje bez exekuce tohoto setupu.

Vedle toho dál platí globální swap blok okno z `logika.py`, které zastaví celý trading flow bez ohledu na strategii.

## Ownership a správa pozic

`strategy_context.py` zajišťuje jednotné označení a rozpoznání pozic:

| Strategie | Magic | Strategy ID |
|---|---|---|
| Primární | 234000 | `gemini_primary` |
| Index (ownership bez vlastní vstupní větve) | 234100 | `gemini_indices` |
| Paralelní | 234200 | `parallel_mean_reversion` |
| Reverzní | 234300 | `reversal_pattern` |
| Quant | 234400 | `quant_math` |
| Cloud Ollama | 234500 | `ollama_cloud_primary` |
| Scalping | 234600 | `liquidity_sweep_scalping` |
| Gemini Prediction | 234700 | `gemini_prediction` |

- primární strategie může podle konfigurace spravovat i manuální nebo legacy pozice
- všechny ostatní strategie jsou od legacy správy oddělené
- scalping strategie ukládá rozšířená metadata pozic (invalidation level, emergency ref) do `scalping_position_state.json`

Komentáře obchodů používají marker ve tvaru `ga:<strategy_id>`.

### Minutový profit cleanup

Pro ECN účet podle dodaného výpisu broker účtuje celou komisi při vstupním dealu (`in`); při výstupním dealu (`out`) další komise účtována není. Výpočet proto nesmí přidávat odhadovaný poplatek za zavření: používá součet skutečných `commission` a `fee` z MT5 historie všech dealů dané pozice, včetně vstupního.

`profit_cleanup_strategy.py` obsahuje dva nezávislé výstupní scénáře, oba zapínané přes `PROFIT_CLEANUP_STRATEGY_ENABLED` a respektující swap blokovací okno:

- Minutová kontrola běží každou minutu a zavírá pozici, pokud čistý zisk `profit + swap + skutečně zaúčtovaná commission + fee` dynamický cíl přísně překročí. Náklady čte z MT5 deal historie podle identifikátoru pozice; `None` (chyba historie) se nepovažuje za nulové náklady a průchod se zapíše jako chyba bez zavření kandidáta. Cíl je `PCZ = max((0,01 × position_volume / reference_volume) × balance, 0,005 USD)`, přičemž `reference_volume = (int(balance / 500) + 1) × 0,01`.
- Denní uzavírání běží v okně **21:50 až 22:00 Europe/Prague** a zavře pozici při čistém zisku alespoň `0,50 USD` po započtení skutečných nákladů z MT5 historie a swapu. Nezávisí na stáří pozice a nekoliduje s pětidenním pravidlem profit protection.

`PROFIT_CLEANUP_STRATEGY_DRY_RUN=false` povoluje skutečné close příkazy. Výsledky a důvody obou scénářů zapisuje do `trade_logs/profit_cleanup.csv`; sloupec `fee` obsahuje skutečný součet `commission + fee` z MT5 (typicky zápornou částku).

### Hodinové Gemini přehodnocení všech pozic ze zisku dne

[`daily_profit_exit_strategy.py`](daily_profit_exit_strategy.py) implementuje „Nápad I“: přehodnocuje všechny otevřené pozice účtu, včetně manuálních, legacy, scalping a pozic s broker-side TP. Nemění jejich ownership, TP ani velikost a neotevírá nové obchody.

- `DAILY_PROFIT_EXIT_ENABLED` zapíná vrstvu (výchozí `false`).
- `DAILY_PROFIT_EXIT_DRY_RUN` řídí skutečné close příkazy (výchozí `true`). Aktuální lokální `.env` je podle schválení nastavena na enabled/live; `.env.example` zůstává bezpečně disabled/dry-run.
- `DAILY_PROFIT_EXIT_MIN_RETAINED_USD=1.0` zachovává nejméně 1 USD dnešního výdělku.
- `DAILY_PROFIT_EXIT_SLIPPAGE_BUFFER_USD=1.0` přidává další 1 USD rezervy pro skluz. Limity vyžadují USD účet; chybná konfigurace nebo nečíselné/nekonečné hodnoty vyhodnocení ukončí s chybou.

Aktivace používá pouze dnešní realizovaný trading výsledek od půlnoci `Europe/Prague` do okamžiku kontroly: součet `profit + swap + commission + fee` všech BUY/SELL dealů. Zahrnuje vstupní komise zaplacené dnes, nikoli vklady, výběry, kredit nebo floating profit. Samostatné záporné brokerovy charges, komise a úroky rozpočet dále snižují; jejich kladné protějšky jej nezvyšují. Účetní korekce či zrušené dealy zablokují vyhodnocení do vyjasnění historie. Chyba historie ani chybějící/nečíselné náklady nejsou nulový výdělek: žádný close se při nich nepovolí.

Rozhodovací požadavek se spouští nejvýše jednou za 60 minut, pouze při realizovaném výsledku **větším než zachovávaný zisk + slippage buffer**, při otevřených pozicích a před **21:15 Europe/Prague**. Po 21:15 nové přehodnocení ani jeho exekuce neproběhnou. Globální swap blok zůstává respektovaný. Při vyčerpání rozpočtu vrstva dočasně nepokračuje. Nezávisí na `TRADING_TRIGGER_MARGIN_THRESHOLD`, dostupnosti vstupních predikcí nebo úspěchu vstupní strategie.

Monitor spouští oddělený worker, takže čekání na Gemini nezastaví ostatní minutové management úlohy. Mutex a souborový lock ve stejné servisní složce brání souběhu této vrstvy v několika vláknech/procesech. Timestamp pokusu se uloží před voláním; chyba nebo restart nezpůsobí opakování AI požadavku v témže 60minutovém intervalu.

Gemini dostane aktuální účet, realizovaný zisk, všechny pozice a čerstvá H1/H4/D1 data s indikátory. Chybějící svíčky nebo požadované RSI/EMA/ATR/ADX indikátory ukončí vyhodnocení s chybou. Může vrátit **HOLD pro všechny**: není povinnost využít denní rozpočet. Posuzuje současnou strukturu trhu; původní vstupní záměr není uložený a model jej nesmí vymýšlet. Data mohou obsahovat právě vznikající svíčku, kterou prompt výslovně zakazuje považovat za uzavřené potvrzení.

Samostatné JSON schema vyžaduje pro každý dodaný ticket právě jednu odpověď HOLD/CLOSE s odůvodněním. Neznámé/duplicitní tickety, chybějící posouzení nebo jiná akce odpověď zneplatní. Modelový výběr používá Gemini Vertex konfiguraci a její stávající fallback politiku, ne Ollama ranking nebo vstupní BUY/SELL schema.

Před každým navrženým close se znovu ověří:

1. enabled/live režim, shutdown, swap blok a konec 21:15; odpověď starší 10 minut nebo z jiného obchodního dne se neexekuuje;
2. shoda MT5 účtu (login/server) s uloženým stavem, existence pozice, její ticket/identifier, symbol, směr, objem, čas otevření a magic;
3. aktuální čistý P/L `profit + swap + skutečně zaúčtované commission + fee`, čerstvý tick (nejvýše 120 sekund) a MT5 odhad P/L uzavření na aktuálním bid/ask;
4. dnešní realizovaný zisk a dostatečný rozpočet na horší z odhadovaných ztrát (celkový čistý P/L, nový cash increment bez již zaplacených vstupních komisí, nebo quote-based close P/L se swapem).

Při dnešním realizovaném zisku 20 USD a rezervách 1 + 1 USD lze pokrýt nejvýše 18 USD odhadované ztráty. Původní vstupní komise mohou už být v dnešním výsledku; používá se konzervativně horší odhad, aby se rozpočet nepřecenil. Skluz, nové obchody či souběžné výstupy jiných vrstev přesto mohou výsledek změnit: nejde o garantovaný kladný den ani garantovaný stop-loss.

Gemini může seřadit více CLOSE návrhů a za jedno přehodnocení se může uzavřít **více pozic postupně**, bez pevného limitu počtu. Před každým odesláním se atomicky uloží pending marker. Dokud MT5 nepotvrdí zánik pozice podle identifieru a odpovídající výstupní deal v historii, další AI/close této vrstvy jsou blokované. Po potvrzení se pokračuje dalším návrhem z téže odpovědi Gemini, ale znovu se načtou aktuální pozice, kotace a dnešní realizovaný výsledek a ověří všechny podmínky včetně rozpočtu a času. V dry-run se zaznamenává více způsobilých návrhů, jejich odhadované ztráty se průběžně odečítají od simulovaného rozpočtu; hypotetické zisky jej nezvyšují. Neúspěšný, částečný nebo nejednoznačný close se slepě neopakuje; stav vyžaduje kontrolu skutečných dealů a pozice. Ostatní management vrstvy nadále běží.

Logy v `SERVICE_DEST_FOLDER/trade_logs`:

- `daily_profit_exit.jsonl`: start review, úplný prompt odeslaný Gemini (`ai_request`, včetně účtu/pozic/tržních dat), model a validovaná HOLD/CLOSE rozhodnutí s reasoning (`ai_review`), lokální zamítnutí, would-close, požadavek a výsledek close, následné potvrzení a chyby. Log může obsahovat citlivý stav obchodního účtu; chraň přístup k `trade_logs`;
- `daily_profit_exit_state.json`: poslední pokus a případný pending close (neodstraňovat bez ověření MT5 historie);
- `daily_profit_exit_state.lock`: souborový lock, samotná existence souboru neznamená běžící worker.

### Profit protection starých pozic

`profit_protection_strategy.py` používá pro všechny své výstupy jednotný čistý zisk `profit + swap + skutečně zaúčtovaná commission + fee`. Náklady se načítají z MT5 deal historie podle identifikátoru pozice; nedostupná nebo prázdná historie se neinterpretuje jako nulové náklady a pozice se podle takového výpočtu nezavře. Ochrana zisku, pravidlo `PROFIT_PROTECTION_STALE_HOURS` (výchozí 12 hodin) i výstup po `PROFIT_PROTECTION_MAX_HOLD_DAYS` (výchozí 5 dní; minimálně `0,50 USD`) sdílejí tento výpočet. Při změně výpočtu se dříve uložený vrchol zisku zahodí a vytvoří se znovu ve stejné účetní bázi. Pravidla se vztahují pouze na pozice spravované profit protection; pozice s broker-side TP tato vrstva nespravuje.

### Swap rollover cleanup

`swap_rollover_cleanup_strategy.py` během swap blokovacího okna uzavírá způsobilé pozice při čistém zisku alespoň `0,10 USD`. Stejně jako profit cleanup počítá `profit + swap + skutečně zaúčtovaná commission + fee` z MT5 deal historie podle identifikátoru pozice; nedostupná nebo prázdná historie nedovolí uzavření podle předpokládaných nulových nákladů. Pozice spravované profit protection a skalpovací strategií tato úklidová větev nadále přeskočí.

### Večerní blokace nových vstupů

Nové pozice se neotevírají od 21:50 do 00:00 `Europe/Prague`. Plánovač zahodí připravené signály a počká, až zákaz skončí; exekuční vrstva kontroluje čas ještě jednou bezprostředně před odesláním MT5 příkazu. Ostatní strategie zavírání a správy již otevřených pozic běží dál. Vedle tohoto pevného večerního okna zůstává aktivní také samostatná blokace kolem swap rolloveru.

### Take profit při záporném swapu

Samostatný minutový monitor v `account_monitor.py` kontroluje všechny otevřené pozice účtu, včetně manuálních, legacy a skalpovacích pozic, bez filtru magic čísla:

- pokud je skutečně zaúčtovaný `position.swap < 0` a pozice má `position.tp > 0`, odstraní broker-side TP příkazem `TRADE_ACTION_SLTP` s `tp=0.0`
- při swapu `>= 0` ponechá stávající TP beze změny; chybějící TP se nenastavuje ani neobnovuje
- pozice bez TP se nemodifikuje; nerozhoduje zisk/ztráta pozice ani predikovaná swapová sazba instrumentu
- těsně před modifikací znovu načte pozici podle ticketu, ověří swap a TP a zachová aktuální stop loss; pozici nezavírá
- kontrola běží před profit protection a rollover cleanup, nezávisle na vstupních strategiích, marži a globálním swap blok okně; toto pravidlo není dry-run a neřídí se dry-run přepínači cleanup strategií
- chyby MT5 jsou hlášeny v konzoli a logu, neblokují ostatní management úlohy ani další pozice; neúspěšné odstranění se zkusí znovu při další kontrole

Pravidlo pracuje s již otevřenými pozicemi; výpočet TP při vstupu do nového obchodu se nemění. Po odstranění TP zůstávají ostatní existující pravidla správy pozic aktivní podle svých dosavadních podmínek.

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
- `trade_logs/candidate_rejection_summary.json`: jeden řádek na unikátní kombinaci strategie/symbol/směr s neúspěšnou exekucí a počtem opakovaných cooldown přeskočení
- `trade_logs/trade_outcomes_state.json`: stav pozic čekajících na uzavření
- `trade_logs/closed_trade_outcomes.csv`: jeden uzavřený výsledek na position ID, včetně čistého P/L po profit/commission/swap/fee, doby držení a R-multiple, pokud je znám počáteční risk. R-multiple vychází z plánovaného syntetického risku, nejde o riziko omezené broker-side SL.
- `trade_logs/position_management_monitor.jsonl`: heartbeat monitoru, odstranění TP (`negative_swap_tp_removed`) a chyby (`negative_swap_tp_removal_error` nebo `position_management_task_error`); zápis vyžaduje `SERVICE_DEST_FOLDER`

Význam nových CSV souborů:

- `trade_decision_audit.csv`: úplná historie rozhodovacích kroků. Je vidět, kterou strategii runtime zkoušel, pro jaký symbol, jestli obchod provedl, a pokud ne, proč ne.
- `trade_decision_snapshot.csv`: pouze poslední známý stav bez historie. Obsahuje vždy aktuální poslední řádek pro `primary`, `parallel`, `reversal` a případně `cycle`, takže je vhodný pro rychlou ruční kontrolu bez procházení celé historie.
- `parallel_strategy_status.csv` a `reversal_strategy_status.csv`: poslední detailní stav každé sekundární strategie, včetně root rejection důvodu, rule failures a cooldown expiry.
- `candidate_rejection_summary.json`: opakované přeskočení po selhání exekuce se již nezapisuje jako nový řádek do `trade_decision_audit.csv`; počítá se odděleně od prvotního selhání.
- `closed_trade_outcomes.csv`: běžící monitor páruje uzavřené MT5 dealy podle position ID. Nové pozice se párují přes opening-order ticket; tracker proto předpokládá hedging účet. Výsledky pozic otevřených před nasazením trackeru lze zaznamenat bez počátečního risku; v takovém případě zůstane `r_multiple` prázdné. Nové souhrny odmítnutí a výsledků se začnou tvořit od nasazení této verze; staré auditní záznamy se automaticky nemigrují.

V audit logu jsou teď navíc vidět i pomocné přechodové informace:

- `candidate_source` a `candidate_queue`: skutečný zdroj kandidáta, například `ollama_cloud_live_advisory_candidate_1`, `ollama_cloud_cached_advisory_candidate_1`, `local` nebo `quant`. Cloudové zdroje se v `candidate_queue` zatím ponechávají v plném tvaru zdroje
- `candidate_rank`: pořadí kandidáta v dané frontě
- `queue_transition` s důvodem `gemini_candidates_exhausted_local_fallback`: historická větev pro přechod z kandidátů označených jako Gemini na lokální ranking. Aktuální cloudové advisory zdroje tuto událost nespouštějí; její absence neznamená, že se lokální fallback nezkusil

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
- odstranění TP při záporném swapu, ponechání při nulovém/kladném swapu, zachování aktuálního SL, všechny typy ownership a opakování po chybě MT5
- omezené zmenšení objemu při nedostatku marže a odmítnutí při dosažení brokerova minima
- souhrn unikátních odmítnutých kandidátů a opakovaných cooldown přeskočení
- uzavřené výsledky podle MT5 position ID včetně čistého P/L, doby držení a dostupného R-multiple

## Stručné shrnutí

Aktuální architektura je záměrně konzervativnější než původní Gemini-led flow:

- AI navrhuje, ale lokální pravidla rozhodují
- risk je lokální a deterministický
- vstupní profily se v každém cyklu řadí podle historické realizované ztráty; při shodě podle `strategy_id`
- pořadí i peněžní ztráty profilů jsou v `decision_log.jsonl` jako `strategy_priority_order`
- Cloud Ollama má vlastní profil a zároveň poskytuje advisory ranking pro Primary; tyto dvě role se nesmějí zaměňovat
- Gemini Prediction má vlastní profil a Gemini advisory ranking; jeho rejection cooldown není sdílený s Cloud Ollama
- Index má samostatný ownership pro správu existujících pozic, ale není zapojený jako vstupní strategie; `INDEX_STRATEGY_ENABLED` ani indexové spread/lot limity nové vstupy neřídí
- cap se automaticky odvozuje z aktuální balance po tisících; historické `TRADING_ACCOUNT_BALANCE_CAP` v podkladových env souborech nemá účinek
- každý profil se pokusí otevřít obchod jen tehdy, když žádný dříve seřazený profil obchod neotevřel; scalping navíc potřebuje nakonfigurovaný maržový práh (výchozí alespoň 5 %)
- scalping pracuje přímo s M5 OHLCV daty z MT5, není závislý na AI predikcích ani precomputed indikátorech
- opakované cloudové advisory dotazy omezuje cache; opakované odmítnuté kandidáty omezuje rejection cooldown
- obchodování je svázané jak globálním swap blokem, tak session okny jednotlivých strategií
- správa skalpovacích pozic (exit podmínky) běží vždy při každém cyklu, nezávisle na ostatních strategiích