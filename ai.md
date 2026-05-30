# AI Analýza Strategie

Analyzoval jsem `bots/analysis/ollama/TRADING_LOGIC.md` a navazující implementaci v `bots/analysis/ollama/trading_logic.py`, `bots/analysis/ollama/final_decision.py`, `bots/analysis/ollama/gemini_decision.py`, `bots/analysis/ollama/trade_execution.py` a exit/risk modulech. Nejzásadnější závěr je jednoduchý: systém má několik ochranných vrstev pro ziskové pozice, ale prakticky nemá tvrdý stop-loss pro nové obchody. To je velmi pravděpodobně hlavní důvod, proč bez manuálního zásahu ztrácí stabilitu.

## 1. Strategy Summary

Současná strategie je AI-driven swing systém. Trigger pro spuštění obchodního kola je volná marže nad prahem `TRADING_MARGIN_THRESHOLD`, aktuálně 20 %. Potom se načtou predikce a do finálního výběru projdou jen symboly, kde `BUY` nebo `SELL` překročí minimální threshold; standardně 35 %, pro crypto 65 %.

Vstupní logika samotná ale není deterministická. Gemini dostane účet, otevřené pozice a filtrované kandidáty a má vybrat právě jeden instrument, směr, lot a TP. Diverzifikace je jen prompt instrukce, nikoli tvrdý exekuční guard. Indikátory, které kód opravdu počítá, jsou jen SMA a RSI. Timeframy jsou `1h`, `4h`, `day`, `week`, `month`.

Exit vrstva je asymetrická. Při otevření obchodu se do requestu případně přidá jen `tp`; `sl` se nikde nenastavuje. Profit protection zavírá jen ziskové pozice při retracementu nebo stáří. Swap rollover cleanup zavírá profitní pozice v blok okně. Loss cleanup existuje, ale v aktuálním `.env` je vypnutý.

Risk management dnes znamená hlavně validaci symbolu, zaokrouhlení lotu na broker step a margin check proti capped balance. Aktuální runtime navíc používá `GEMINI_FULL_CONTROL_EVERY_N_TRADES=1`, takže TP z Gemini se používá v každém obchodu. To je významně agresivnější než dokumentovaný default 3.

## 2. Problems Identified

1. Největší slabina je absence hard stop-lossu na vstupu. Objednávka posílá pouze market deal a případně `tp`, ale ne `sl`. To znamená, že ztrátová pozice nemá předem definované maximální riziko.
2. Profit protection chrání jen profitní pozice. Pokud se obchod po vstupu otočí do ztráty, tato vrstva nepomůže, protože filtruje jen `net_profit > 0`.
3. Loss cleanup je aktuálně vypnutý. Prakticky tedy neexistuje automatická vrstva, která by systematicky odřezávala staré ztráty.
4. Entry signal není rule-based. RSI a MA se pouze posílají do AI promptu; kód nikde nevynucuje podmínku typu `RSI < 30` nebo `MA fast > MA slow`. To je problém pro robustnost, backtest i walk-forward validaci.
5. Chybí volatility/regime filter. Používají se jen SMA a RSI, bez ATR, ADX nebo Bollinger-based filtru. Strategie tak nerozlišuje trend, range a high-volatility shock režimy.
6. Chybí tvrdý portfolio risk layer. Není zde max drawdown kill-switch, max daily loss, max portfolio heat ani max trades/day. Monitoring pouze znovu spustí obchodní logiku, když se vrátí free margin nad threshold. To není risk limit, to je jen kapacitní trigger.
7. Lot sizing je příliš závislý na AI. Kód lot jen validuje a u crypto násobí `0.25`. Pokud Gemini vrátí nestabilní lot, strategie nemá deterministický přepočet podle stop distance a % risku.
8. Diverzifikace není spolehlivě vynucena. V promptu je instrukce nebrat podobný symbol, ale exekuční kód to mimo crypto cap explicitně nepočítá. AI může instrukci obcházet nebo interpretovat volně.
9. Aktuální runtime používá full Gemini TP mode v každém obchodu. Tím se ještě zvyšuje subjektivita exitu. U strategie, která už nemá SL, je to špatná kombinace.
10. Instrument universe je příliš široké a není jasně omezené. `MT5_SYMBOL_SUFFIX` je prázdný a systém umí Forex, crypto i CFD. To zvyšuje heterogenitu trhu, spreadů a volatility bez odpovídající normalizace pravidel.

## 3. Improvement Proposals

Nejdřív bych zvýšil technickou disciplínu, ne počet feature. Pro tuto strategii dává smysl přejít z „AI rozhoduje obchod“ na „AI může jen rankovat kandidáty, ale obchod se otevře až po průchodu tvrdými pravidly“.

1. **Vstupní pravidla pro trend-following variantu**

   `LONG` otevři jen pokud:
   - `D1 EMA200 slope > 0`
   - `H4 EMA50 > H4 EMA200`
   - `H1 close > H1 EMA20`
   - `H1 RSI14` mezi `52` a `68`
   - `H1 ATR14 / close >= 0.25 %`
   - spread `<= 0.15 * ATR14(H1)`
   - není high-impact news v intervalu `[-30m, +30m]`
   - neexistuje otevřená pozice na stejném symbolu

   `SHORT` zrcadlově.

2. **Výstupní pravidla**

   - `SL = 1.5 * ATR14(H1)` od vstupu
   - `TP1 = 2.2R`
   - po dosažení `+1.0R` posuň `SL` na break-even + fee buffer
   - po dosažení `+1.5R` trailing `SL = max/min poslední H1 swing ± 0.5 * ATR`
   - pokud po 24 hodinách obchod nedosáhl alespoň `+0.5R`, zavři market
   - pokud `ADX(H4) < 18` dvě po sobě jdoucí svíčky, zavři zbytek pozice

3. **Position sizing**

   - `risk_per_trade = 0.50 %` effective equity
   - `lot = floor_to_step(risk_usd / stop_distance_value_per_lot)`
   - neber lot z AI; AI může dodat jen preferovaný `confidence score`

4. **Portfolio risk**

   - `max_open_positions = 3`
   - `max_positions_per_symbol = 1`
   - `max_portfolio_heat = 1.5 %` součet otevřeného rizika
   - `max_trades_per_day = 4`
   - pokud denní realizovaná ztráta dosáhne `-1.5 %`, zastav nové vstupy do dalšího pražského dne
   - pokud equity drawdown od týdenního high dosáhne `-6 %`, aktivuj kill-switch a obchoduj jen po manuálním resetu

5. **Market regime handling**

   - trend strategii povol jen když `ADX(H4) >= 20` a `ATR percentile(60d)` je mezi `35` a `85`
   - pokud je `ADX < 18`, trend strategy disabled

6. **AI usage redesign**

   - Gemini nech jen na `ranking 3 kandidátů` nebo textový komentář
   - finální vstup musí schválit deterministic gate
   - Ollama/Gemini procenta nepoužívej jako vstup sama o sobě; používej je maximálně jako sekundární score

7. **Exit cleanup vrstvy**

   - profit protection zachovej, ale převeď ji na `R-multiple` místo absolutních USD
   - například: aktivace při `MFE >= 1.0R`, uzavření při retracementu pod `0.6 * MFE`

8. **Runtime config**

   - změň `GEMINI_FULL_CONTROL_EVERY_N_TRADES` na vysokou hodnotu nebo TP z Gemini úplně vypni, protože exit musí být odvozen z volatility a risku, ne z volného textového modelu
   - zapnout loss cleanup samo o sobě nestačí; bez SL by to byla pořád jen pozdní náplast

### Jak se na tuto strategii vztahují best practices

- **Strict rule-based logic**: dnes chybí. Konkrétně u vás je nutné odstranit volnost v entry, lot sizingu a TP.
- **Kombinace trend/momentum/volatility**: dnes máte jen trend-lite a momentum-lite přes SMA/RSI. Chybí volatility vrstva; doplňte ATR a regime classifier.
- **Proper risk management**: dnes je pouze margin guard. To není risk management obchodu. Musíte přidat SL, sizing podle risku a kill-switch.
- **Walk-forward mindset**: současný AI prompt styl je těžko stabilně testovatelný. Nejprve zmrazit pravidla a teprve pak dělat walk-forward na 6M train / 3M test rolling window.
- **Avoid over-optimization**: nepřidávejte 10 indikátorů. Stačí `EMA trend + RSI momentum + ATR volatility + spread/news filter`.

## 4. Alternative Strategy

Jako paralelní, slabě korelovanou strategii doporučuji mean-reversion pouze na major FX párech v range režimu. Původní systém je implicitně swing/trend/AI-selection. Druhá strategie by měla fungovat právě tehdy, když trend-following edge slábne.

### Tržní podmínky

- `ADX(H4) < 18`
- `ATR percentile(20d)` mezi `20` a `60`
- žádná high-impact news
- spread pod předem daným limitem
- jen `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCHF`

### Entry

`LONG`, pokud:
- `H1 close < lower Bollinger(20, 2.0)`
- `RSI(2) <= 5`
- `distance od H4 VWAP >= 1.2 * H1 ATR14`

`SHORT`, pokud:
- `H1 close > upper Bollinger(20, 2.0)`
- `RSI(2) >= 95`
- `distance od H4 VWAP >= 1.2 * H1 ATR14`

Vstup vždy až na close svíčky, ne intrabar.

### Exit

- `SL = 1.0 * ATR14(H1)` za extrémem
- `TP = middle Bollinger` nebo `1.5R`, co nastane dřív
- time stop `8 hodin`
- pokud po 3 svíčkách nedojde k mean reversion alespoň o `0.4R`, zavři

### Risk

- `risk_per_trade = 0.35 %`
- `max_2` otevřené mean-reversion obchody současně
- `max_1` obchod na symbol za den
- strategii vypni, pokud `ATR percentile > 70`, protože range edge se rozpadá

Tato druhá strategie bude slaběji korelovaná s původní, protože vydělává v netrendových režimech, kde trendový přístup typicky dostává whipsaw.

## 5. Example Rules (Pseudo-Code)

```text
# Trend-following primary strategy

if in_swap_block_window():
    skip_new_entries()

if daily_loss_pct <= -1.5 or weekly_drawdown_pct <= -6.0:
    disable_strategy("risk_kill_switch")

for symbol in tradable_symbols:
    data = load_market_state(symbol)

    regime_ok = adx_h4(symbol, 14) >= 20 and atr_percentile_h1(symbol, 20, lookback=60) in [35, 85]
    spread_ok = current_spread(symbol) <= 0.15 * atr_h1(symbol, 14)
    news_ok = not high_impact_news_within(symbol, minutes=30)

    long_signal = (
        ema_d1(symbol, 200).slope > 0 and
        ema_h4(symbol, 50) > ema_h4(symbol, 200) and
        close_h1(symbol) > ema_h1(symbol, 20) and
        52 <= rsi_h1(symbol, 14) <= 68
    )

    short_signal = (
        ema_d1(symbol, 200).slope < 0 and
        ema_h4(symbol, 50) < ema_h4(symbol, 200) and
        close_h1(symbol) < ema_h1(symbol, 20) and
        32 <= rsi_h1(symbol, 14) <= 48
    )

    if regime_ok and spread_ok and news_ok and no_open_position(symbol):
        if long_signal:
            stop_distance = 1.5 * atr_h1(symbol, 14)
            lot = size_by_risk(symbol, risk_pct=0.5, stop_distance=stop_distance)
            place_buy(symbol, lot, sl=entry - stop_distance, tp=entry + 2.2 * stop_distance)

        if short_signal:
            stop_distance = 1.5 * atr_h1(symbol, 14)
            lot = size_by_risk(symbol, risk_pct=0.5, stop_distance=stop_distance)
            place_sell(symbol, lot, sl=entry + stop_distance, tp=entry - 2.2 * stop_distance)
```

```text
# Position management

for position in open_positions:
    r_multiple = unrealized_pnl(position) / initial_risk(position)

    if r_multiple >= 1.0:
        move_stop_to_break_even_plus_fee(position)

    if r_multiple >= 1.5:
        trail_stop_using_h1_swing(position, atr_multiplier=0.5)

    if age_hours(position) >= 24 and r_multiple < 0.5:
        close_position(position, reason="time_stop")

    if regime_breakdown(position.symbol):
        close_position(position, reason="regime_invalidated")
```

```text
# Parallel mean-reversion strategy

if adx_h4(symbol, 14) < 18 and atr_percentile_h1(symbol, 20, lookback=20) in [20, 60]:
    if close_h1(symbol) < bb_lower(symbol, 20, 2.0) and rsi_h1(symbol, 2) <= 5:
        open_long_with_sl_tp()

    if close_h1(symbol) > bb_upper(symbol, 20, 2.0) and rsi_h1(symbol, 2) >= 95:
        open_short_with_sl_tp()
```

## 6. Implementation Notes

Architekturu rozdělte na tři vrstvy:

1. **Signal layer**  
   Vrací čistě deterministický objekt:  
   `{symbol, side, regime, entry_reason_codes, stop_distance, quality_score}`.  
   Sem patří EMA/RSI/ATR/ADX/news/spread filtry. AI může dodat jen doplňkové `ai_rank`, ne finální rozhodnutí.

2. **Risk layer**  
   Jediné místo, kde se počítá:  
   `risk_per_trade`, `lot`, `portfolio_heat`, `daily_stop`, `drawdown_kill_switch`, `max_trades_per_day`, `max_positions_per_symbol`.  
   Tato vrstva musí mít veto nad signal layer.

3. **Execution layer**  
   Zodpovědnost pouze za:  
   validaci symbolu,  
   odeslání orderu,  
   idempotentní retry,  
   SL/TP modify,  
   close by rule,  
   audit log.

Logging bych rozšířil takto:

- `decision_log.jsonl`: každý kandidát, regime label, reason codes, spread, ATR, ADX, stop distance, expected R
- `execution_log.jsonl`: request, fill price, slippage, reject reason, latency
- `position_log.jsonl`: MFE, MAE, R multiple, duration, exit_reason
- `risk_log.jsonl`: daily pnl, realized pnl, open risk, equity high-water mark, kill-switch state
- `ai_log.jsonl`: jen pokud AI ponecháte, tak ukládat prompt hash, candidate ranking a response hash; ne volný text jako primární rozhodovací audit

Prakticky bych prioritu nastavil takto:

1. doplnit hard SL a sizing podle `% risk`
2. odebrat Gemini z lot sizingu a TP
3. přidat ATR/ADX regime filter
4. zavést max daily loss a max portfolio heat
5. teprve potom řešit jemnější profit protection

Pokud chceš, navážu přímo návrhem konkrétní refaktorizace pro tento repozitář: které moduly přidat, co přesunout z `final_decision.py` a jak přepsat `trade_execution.py`, aby se to dalo rovnou implementovat.

## 7. Kroky Realizace

Níže je navržený postup realizace nad stávajícím kódem. Je postavený tak, aby nebylo nutné dělat kompletní rewrite a aby se změny daly zavádět inkrementálně.

### Cíl první iterace

V první iteraci se zavedou pouze tyto změny:

1. trend-following vstupní pravidla jako deterministický filtr
2. interní syntetický stop pouze pro výpočet velikosti pozice a interní risk metriku
3. TP nebude řízen Gemini, ale vlastní logikou systému
4. SL se nebude posílat do MT5
5. sizing bude řízen podle `% risk` nad syntetickým stopem
6. doplní se ATR a ADX regime filter
7. připraví se oddělení primární a alternativní strategie přes `strategy_id`, `magic` a `comment`
8. rozšíří se logging o rozhodnutí, risk a ownership obchodů
9. primární strategie bude umět reagovat i na ručně otevřené nebo historicky běžící pozice, minimálně přes take-profit / profit-management vrstvu
10. alternativní strategie bude mít vlastní aktivaci odvozenou od prahu primární strategie a konfigurovatelný limit maximálního počtu vlastních otevřených pozic

### Syntetický interní stop

Protože nechceme broker-side `Stop Loss`, použije se interní syntetický stop pouze pro výpočet lotu a řízení interní risk logiky.

Navržené pravidlo pro první iteraci:

- `synthetic_stop_distance = 1.5 * ATR(14) na H1`
- pro `BUY`: `synthetic_stop_price = entry_price - synthetic_stop_distance`
- pro `SELL`: `synthetic_stop_price = entry_price + synthetic_stop_distance`
- tento stop se nebude posílat do MT5 requestu
- bude uložen do logu a použit pouze pro výpočet:
    - `risk_usd`
    - `lot_size`
    - `R multiple`
    - budoucí cleanup / exit logiku

### Reakce na ručně otevřené a již běžící obchody

Primární strategie nebude v první iteraci řídit vstup ručně otevřených pozic, ale bude umět na ně reagovat v rámci výstupní vrstvy tam, kde to dává smysl.

Navržené pravidlo:

- pozice, které nejsou otevřené přímo automatizací, mohou být volitelně zařazené do `take profit` / `profit management` logiky primární strategie
- ownership vstupu a ownership výstupu se proto rozdělí na dvě věci:
    - `entry_owner_strategy_id`
    - `management_owner_strategy_id`
- pro ručně otevřené nebo již existující pozice bude možné nastavit, že:
    - vstup nepatří primární strategii
    - ale výstupní řízení přes TP / profit-management ano

Praktický důsledek:

- primární strategie může reagovat na ručně otevřené obchody přes TP logiku a případně budoucí cleanup logiku
- alternativní strategie ani jiné strategie nebudou tyto pozice řídit, pokud jim nebude explicitně přiřazen `management_owner_strategy_id`

### Konkrétní implementační kroky

#### Krok 1: Rozšíření market dat o EMA, ATR a ADX

Upravit sběr dat tak, aby kromě RSI a MA vracel i:

- `EMA20`, `EMA50`, `EMA200`
- `ATR14`
- `ADX14`
- aktuální spread snapshot

Pravděpodobné místo změn:

- `bots/analysis/ollama/market_data.py`

Výstup dat bude rozšířen tak, aby šel použít jak pro primární strategii, tak pro alternativní strategii.

#### Krok 2: Zavedení deterministického signal layeru pro primární strategii

Přidat nový modul, který nebude závislý na Gemini a vrátí čistý signál jen pokud jsou splněna tvrdá pravidla.

Navrhované nové moduly:

- `bots/analysis/ollama/signal_rules.py`
- `bots/analysis/ollama/market_regime.py`

První implementovaná pravidla:

- `D1 EMA200 slope > 0` pro long a `< 0` pro short
- `H4 EMA50 > H4 EMA200` pro long a opačně pro short
- `H1 close > EMA20` pro long a opačně pro short
- `RSI14` v definovaném pásmu
- `ADX(H4) >= 20`
- `ATR percentile` nebo jednodušší ATR volatility gate v první verzi
- spread pod limitem

Gemini pak nebude rozhodovat o lotu ani TP. V přechodové fázi může zůstat jen jako ranking nebo advisory vrstva nad kandidáty.

#### Krok 3: Zavedení risk layeru se syntetickým stopem

Přidat řízení velikosti pozice mimo Gemini.

Navrhované nové moduly:

- `bots/analysis/ollama/risk_engine.py`
- případně `bots/analysis/ollama/strategy_registry.py`

První verze risk výpočtu:

- `risk_per_trade_percent` bude čteno z `.env`
- `risk_usd = effective_balance * risk_per_trade_percent`
- `synthetic_stop_distance = 1.5 * ATR(H1)`
- `lot_size = risk_usd / hodnota_pohybu_mezi_entry_a_synthetic_stop`
- lot se následně upraví přes broker step/min/max validaci

Poznámka:

- portfolio gate zůstane nadále zachována přes stávající margin pravidlo `free margin > 20 %`
- nepřidává se nyní další hard drawdown kill-switch, pokud nebude výslovně požadován
- alternativní strategie dostane odvozený vlastní aktivační práh:
    - `primary_activation_margin_percent` například `20`
    - `parallel_activation_margin_delta_percent` například `5`
    - výsledný trigger alternativní strategie bude `20 - 5 = 15 %`

Tyto hodnoty budou čteny z `.env`.

#### Krok 4: Odebrání Gemini z lot sizingu a TP

Upravit finální rozhodovací flow tak, aby:

- Gemini nebyl zdrojem `lot_size`
- Gemini nebyl zdrojem `take_profit`
- finální lot byl vždy spočten risk vrstvou
- finální TP byl vždy spočten lokální logikou

Pravděpodobná místa změn:

- `bots/analysis/ollama/final_decision.py`
- `bots/analysis/ollama/gemini_decision.py`

Možnosti přechodového režimu:

1. Gemini úplně odstranit z finálního výběru obchodu
2. Gemini ponechat jen jako ranking kandidátů, ale obchod otevřít až po průchodu pravidly

Výchozí doporučení pro první iteraci:

- Gemini ponechat jako nepovinný ranking vstup
- exekuci podřídit pouze lokálním pravidlům a risk vrstvě

#### Krok 5: Zavedení lokální TP logiky bez SL do MT5

TP se bude nastavovat u všech nových obchodů, ale podle vlastní logiky.

První navržená logika:

- `take_profit_distance = 2.2 * synthetic_stop_distance`
- pro `BUY`: `tp = entry + take_profit_distance`
- pro `SELL`: `tp = entry - take_profit_distance`
- u CFD nebo crypto lze nadále zachovat konzervativní ochranné limity vzdálenosti TP

Pravděpodobné místo změn:

- `bots/analysis/ollama/final_decision.py`
- `bots/analysis/ollama/trade_execution.py`

#### Krok 6: Oddělení ownershipu strategií

Připravit systém tak, aby primární strategie a alternativní strategie měly oddělené vlastnictví obchodů.

To znamená:

- každá strategie dostane vlastní `strategy_id`
- každá strategie dostane vlastní `magic`
- každá strategie dostane vlastní `comment` marker
- každá strategie bude mít oddělené vlastnictví vstupu a oddělené vlastnictví managementu pozice

Požadované chování:

- risk, margin a diverzifikace budou vidět všechny otevřené pozice
- profit protection, cleanup a exit logika budou řídit pouze obchody patřící konkrétní strategii
- primární strategie bude moci převzít management vybraných ručně otevřených pozic
- alternativní strategie nebude řídit výstupy obchodů, které sama neotevřela, pokud jí to nebude explicitně přiřazeno

První navržené identity:

- primární strategie: `gemini_primary`, `magic=234000`
- alternativní strategie: `parallel_mean_reversion`, nové oddělené `magic`

#### Krok 7: Příprava alternativní strategie

V první fázi se připraví architektura a ownership, ale samotná alternativní strategie se může zapnout až po stabilizaci primární varianty.

Navrhované nové moduly:

- `bots/analysis/ollama/parallel_strategy_mean_reversion.py`
- případně sdílené helpery pro ownership a risk routing

Tím bude možné spustit druhou strategii paralelně, aniž by si strategie navzájem řídily výstupy cizích obchodů.

Navíc se hned v první iteraci připraví tyto limity a spouštěcí pravidla alternativní strategie:

- aktivace alternativní strategie při `primary_activation_margin_percent - parallel_activation_margin_delta_percent`
- konfigurovatelná hodnota v `.env`
- maximální počet současně otevřených pozic alternativní strategie, například `7`
- limit se bude počítat pouze nad pozicemi patřícími alternativní strategii

Navržené `.env` položky:

- `PRIMARY_STRATEGY_ACTIVATION_MARGIN_PERCENT=20`
- `PARALLEL_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT=5`
- `PARALLEL_STRATEGY_MAX_OPEN_POSITIONS=7`
- `PRIMARY_STRATEGY_MANAGE_MANUAL_POSITIONS=true`

Volitelně lze doplnit i:

- `PRIMARY_STRATEGY_MANUAL_POSITION_COMMENT_PATTERNS=`
- `PRIMARY_STRATEGY_MANUAL_POSITION_MAGIC_ALLOWLIST=`

To umožní přesně určit, které ruční nebo historické pozice smí primární strategie převzít do výstupního managementu.

#### Krok 8: Rozšíření logování

Přidat nové logy tak, aby byla dohledatelná rozhodnutí i ownership obchodu.

Navržené logy:

- `decision_log.jsonl`
- `execution_log.jsonl`
- `position_log.jsonl`
- `risk_log.jsonl`
- `strategy_ownership_log.jsonl`

Každý záznam by měl obsahovat alespoň:

- `strategy_id`
- `entry_owner_strategy_id`
- `management_owner_strategy_id`
- `magic`
- `symbol`
- `action`
- `entry_price`
- `synthetic_stop_price`
- `synthetic_stop_distance`
- `take_profit_price`
- `lot_size`
- `risk_usd`
- `signal_reason_codes`
- `regime_state`

### Doporučené pořadí implementace

1. rozšířit `market_data.py` o EMA, ATR, ADX a spread
2. přidat signal layer s trend-following pravidly
3. přidat risk layer se syntetickým stopem a sizingem podle `% risk`
4. odebrat Gemini z lot sizingu a TP
5. zavést vlastní TP logiku
6. oddělit ownership strategií včetně ručně otevřených a historických pozic
7. přidat `.env` konfiguraci pro aktivační prahy a limity alternativní strategie
8. rozšířit logging
9. připravit alternativní paralelní strategii

### Co se v této fázi zatím nebude dělat

- nebude se posílat broker-side `Stop Loss`
- nebude se přepisovat celý systém od nuly
- nebude se zatím zavádět plný drawdown kill-switch
- nebude se zatím měnit cleanup strategie víc, než je nezbytné pro ownership a budoucí kompatibilitu
- nebude se automaticky přebírat každá ručně otevřená pozice bez jasného pravidla nebo konfigurace ownershipu managementu

### Požadavek na potvrzení

Pokud tento plán sedí, další krok bude samotná implementace první iterace v kódu.

Před implementací prosím potvrď hlavně tyto body:

1. že sizing podle `% risk` má být řízen přes interní syntetický stop `1.5 * ATR(H1)`
2. že `take_profit` má být v první iteraci odvozen jako `2.2R` od syntetického stopu
3. že Gemini má být v první iteraci odebrán z `lot_size` a `take_profit`, ale může zůstat jako advisory / ranking vrstva
4. že primární strategie má smět převzít profit-management vybraných ručně otevřených nebo již běžících pozic
5. že alternativní strategie má mít aktivační práh `PRIMARY_STRATEGY_ACTIVATION_MARGIN_PERCENT - PARALLEL_STRATEGY_ACTIVATION_MARGIN_DELTA_PERCENT`
6. že alternativní strategie má mít vlastní limit otevřených pozic přes `.env`, výchozí návrh `7`
7. že alternativní strategie se má v této fázi připravit architektonicky a ownershipem, ale nemusí být hned plně zapnutá do produkčního flow
