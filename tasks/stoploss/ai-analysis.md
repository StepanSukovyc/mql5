# Analýza hybridního řízení ztrátových pozic

Datum analýzy: 4. 8. 2026

## Závěr

Hybrid z `tasks/stoploss/popis.md` je pro tento systém vhodnější než veřejný stop-loss, pokud bude zaveden jako samostatná, konzervativní vrstva řízení životního cyklu pozice. Nejde o garantovaný stop-loss: bot při splnění pravidla odešle opačný market pokyn. Výstup proto závisí na tom, že proces a MT5 připojení běží, a výsledná cena může být horší kvůli spreadu nebo skluzu.

Hlavní pravidlo nasazení musí být neměnné:

> Hybrid smí rozhodovat pouze o pozicích otevřených od `2026-08-01 00:00:00 Europe/Prague` včetně. Pozice otevřené dříve jsou pouze započteny do celkového počtu a účetních metrik; hybrid je nesmí vybrat ani uzavřít.

To odpovídá požadavku na čisté oddělení nového experimentu od desítek existujících starších pozic. V srpnu je tato hranice v UTC `2026-07-31T22:00:00+00:00`; implementace však nemá používat tuto konstantu, ale vždy převést konfigurované datum z `Europe/Prague` na UTC. Eliminuje to chybu při změně letního času.

## Co systém dělá nyní

Pozice spravuje smyčka `run_position_management_monitor()` v [bots/analysis/ollama/account_monitor.py](bots/analysis/ollama/account_monitor.py). Běží standardně každou minutu a postupně spouští profit protection, swap rollover cleanup, denní loss cleanup, měsíční advisory a týdenní surplus cleanup.

Existující výstupy:

- [bots/analysis/ollama/profit_protection_strategy.py](bots/analysis/ollama/profit_protection_strategy.py) se týká jen ziskových pozic patřících primary/index strategii. Zavře profit při retracementu, po 12 hodinách nad cílem nebo po maximální době držení. Ztrátovou pozici neřeší.
- [bots/analysis/ollama/loss_cleanup_strategy.py](bots/analysis/ollama/loss_cleanup_strategy.py) jednou denně vybírá jednu ztrátovou pozici starší sedmi dnů, pokud ji kryje předchozí realizovaný zisk a 2% balance buffer. Kandidáta vybírá podle největší bezpečné ztráty, nikoliv podle struktury trhu.
- [bots/analysis/ollama/monthly_loss_cleanup_strategy.py](bots/analysis/ollama/monthly_loss_cleanup_strategy.py) vytváří pouze doporučení pro ztrátové pozice starší 30 dnů; sám pokyn neodesílá.
- [bots/analysis/ollama/weekly_surplus_cleanup_strategy.py](bots/analysis/ollama/weekly_surplus_cleanup_strategy.py) může jednou týdně uzavřít více starých ztrátových pozic, když je kryje týdenní přebytek. Řadí je od nejstarší, poté podle nejmenší ztráty.
- [bots/analysis/ollama/swap_rollover_cleanup_strategy.py](bots/analysis/ollama/swap_rollover_cleanup_strategy.py) zavírá pouze profitní pozice v rollover okně.

Uzavření se provádí přes `close_position_by_ticket()` v [bots/analysis/ollama/trade_execution.py](bots/analysis/ollama/trade_execution.py): opačný market pokyn s konkrétním ticketem. To je správný technický základ pro interní exit bez brokerem uloženého SL.

## Rozhodnutí: úplně odstranit staré loss-cleanupy

Nový hybrid bude jediný mechanismus, který smí automaticky vyhodnocovat a zavírat ztrátové pozice. Denní, týdenní i měsíční loss-cleanup se nemají jen vypnout přes `.env`; mají být z kódu a konfigurace úplně odstraněny.

Odstranit tyto moduly:

- `loss_cleanup_strategy.py` včetně denního triggeru, rozpočtu Z, state souboru a logů `loss_cleanup.csv` / `loss_cleanup_daily_deals.csv`;
- `weekly_surplus_cleanup_strategy.py` včetně týdenního triggeru, surplus budgetu, state souboru a logu `weekly_surplus_cleanup.csv`;
- `monthly_loss_cleanup_strategy.py` včetně advisory výpočtu, state souboru a `monthly_loss_cleanup_recommendations.json`.

Z [bots/analysis/ollama/account_monitor.py](bots/analysis/ollama/account_monitor.py) se odstraní jejich importy a volání. Odstraní se také jejich jednotkové testy a všechny konfigurační klíče `LOSS_CLEANUP_*`, `WEEKLY_CLEANUP_*` a `MONTHLY_LOSS_CLEANUP_*` z `.env`, `.env.example`, README a `TRADING_LOGIC.md`. Historické soubory v `trade_logs` lze ponechat jako neměnný audit, ale běžící systém je už nesmí číst ani do nich zapisovat.

Tím odpadá konflikt více nezávislých strategií nad stejným ticketem a současně je zajištěno, že žádný starý loss-cleanup nemůže omylem uzavřít pozici otevřenou před 1. 8. 2026.

## Doporučená podoba hybridu

Nový modul například `hybrid_loss_exit_strategy.py` má být jediným vlastníkem ztrátového exitu pro pozice v experimentálním rozsahu. Vyhodnocuje se nejvýše jednou za 15 minut, mimo existující swap blokovací okno.

### 1. Povinná způsobilost

Pozice je kandidát jen tehdy, když platí vše:

- `position.time` je od konfigurovaného počátečního data včetně;
- pozice patří povolené strategii podle `magic`/komentáře přes `position_belongs_to_strategy()` v [bots/analysis/ollama/strategy_context.py](bots/analysis/ollama/strategy_context.py);
- čistý výsledek `profit + swap - fee` je záporný;
- není v rollover blokovacím okně;
- ticket nebyl právě obsloužen jiným exit modulem.

Nedoporučuji implicitně zahrnout manuální pozice (`magic == 0`). Pokud mají být zahrnuty, musí k tomu existovat explicitní konfigurace `HYBRID_EXIT_MANAGE_MANUAL_POSITIONS=false` a auditní záznam o tomto rozhodnutí.

### 2. Věkové vrstvy

| Stáří pozice | Režim | Akce |
| --- | --- | --- |
| `< 24 h` | mladá | Nezavírat kvůli hybridu. Jen logovat stav. |
| `24-72 h` | střední | Nezavírat pouze pro čas. Kontrolovat, zda se nepotvrdilo pokračování pohybu proti pozici. |
| `>= 72 h` | stará | Zavřít, pokud je stále ve ztrátě, není poblíž break-even a zároveň selže trendová/momentum validace. |

Hranice mají být konfigurovatelné v hodinách, ale počáteční konzervativní hodnoty `24` a `72` odpovídají návrhu. Nezavírat starou pozici jen proto, že je stará: u mean-reversion přístupu by to systematicky realizovalo ztrátu během přechodného rozšíření trhu.

### 3. Momentum validace

Střední a stará pozice mají být posuzovány z uzavřených svíček, ne z aktuálního ticku.

- Long: negativní validace nastane při novém H1 low pod low vstupní svíčky nebo posledního potvrzeného swing low a současně `H1 close < EMA20`, případně při H4 downtrend potvrzeném dvěma uzavřenými svíčkami.
- Short: zrcadlově nové H1 high a `H1 close > EMA20`, případně potvrzený H4 uptrend.
- Jediný impuls nestačí. Vyžadovat alespoň dva nezávislé důvody, například nové low/high a uzavření na špatné straně EMA. Tím se omezuje uzavírání při lokálním šumu.

Pro první iteraci má modul nejdřív jen logovat tyto signály. Před zapnutím live režimu je nutné z historických logů ověřit, zda pravidlo nevyhazuje převážně pozice, které se brzy vracejí k break-even.

### 4. Break-even a emergency limit

"Blízko break-even" musí být číselně definováno. Doporučené pravidlo je `net_profit >= -max(2 * fee, HYBRID_EXIT_BREAK_EVEN_BUFFER_USD)`. Pozice s malou technickou ztrátou se ve stáří 72 h nezavírá jen kvůli času.

Vedle hybridu může existovat poslední interní emergency limit. Nemá být veřejný SL a musí být oddělen od momentum pravidel:

- limit počítat z aktuální equity, například `HYBRID_EXIT_EMERGENCY_ACCOUNT_LOSS_PERCENT`;
- pro jednu pozici doporučuji začít podstatně níže než 3-5 % účtu, protože několik souběžných pozic by jinak mohlo vytvořit neakceptovatelný portfolio drawdown;
- emergency exit se spustí bez čekání na věk, ale stále mimo swap blokovací okno jen pokud nejde o skutečný account-protection stav;
- musí být omezen `HYBRID_EXIT_MAX_CLOSES_PER_CYCLE=1`, aby jedna chybná konfigurace nezavřela hromadně účet.

## Praktické provedení

### Jak bude probíhat sledování trhu

Sledování trhu znamená deterministické načtení dat z MT5 a přepočet několika předem daných podmínek. Není to nepřetržité sledování každého ticku ani opakované volání AI.

1. `run_position_management_monitor()` běží dál každou minutu, ale hybrid se spustí nejvýše jednou za `HYBRID_EXIT_CHECK_INTERVAL_MINUTES`.
2. Modul nejdřív načte otevřené pozice a okamžitě vyřadí legacy pozice, ziskové pozice, nepatřící magic/komentáře a pozice v rollover okně.
3. Pro každý zbývající symbol načte přes existující [bots/analysis/ollama/market_data.py](bots/analysis/ollama/market_data.py) přibližně 260 H1 a H4 svíček. To poskytne stabilní výpočet EMA20 a posledních swing high/low.
4. Rozhodnutí se smí změnit jen po vzniku nové **uzavřené** H1 svíčky. Běžící H1 svíčka se nesmí použít, protože její close, high a low se ještě mění. Implementace proto musí explicitně vyřadit aktuální neuzavřený bar z dat vrácených MT5.
5. Z uzavřené svíčky se odvodí reason codes: například pro long `h1_new_low`, `h1_close_below_ema20` a `h4_downtrend`; pro short jejich zrcadla. Každý použitý vstup i výsledek se zapíše do diagnostického JSONL.

Při výpadku MT5 dat, nedostatku svíček nebo chybě indikátoru je správné rozhodnutí `hold_missing_market_data`, nikdy automatický close. Emergency limit založený na aktuálním P/L je samostatná větev a může být vyhodnocen i bez candle dat.

### Jak přesně určit "blízko break-even"

Primární rozhodnutí se musí opírat o skutečný peněžní výsledek pozice, protože zahrnuje rozdílnou hodnotu bodu, objem, swap a spread každého instrumentu:

```text
net_profit = position.profit + position.swap - modeled_fee
break_even_threshold = -max(2 * modeled_fee, HYBRID_EXIT_BREAK_EVEN_BUFFER_USD)
near_break_even = net_profit >= break_even_threshold
```

Příklad: pro `0.01` lotu s modelem poplatku `0.10 USD` a bufferem `0.20 USD` je práh `-0.20 USD`. Starší pozice s `net_profit=-0.15 USD` je blízko break-even a hybrid ji neuzavře jen kvůli věku a negativnímu momentu. Pozice s `net_profit=-4.23 USD` blízko break-even není.

Do diagnostiky se navíc uloží cena otevření (`position.price_open`), aktuální likvidační strana kotace (pro BUY `bid`, pro SELL `ask`) a vzdálenost od break-even v ceně i v ATR. Tyto pomocné metriky slouží k analýze, ale finální pravidlo musí zůstat založené na `net_profit`, aby se nekombinovaly neporovnatelné pipy mezi různými symboly.

### Kdy sledování vede k uzavření

Většina průchodů pouze sbírá data a zapisuje `hold` rozhodnutí. Odeslání `close_position_by_ticket()` je povoleno jen v těchto případech:

| Stav | Market data | Akce |
| --- | --- | --- |
| Legacy nebo mladá pozice | Nevyhodnocují se pro exit | Nikdy nezavírat hybridem. |
| Střední pozice 24-72 h | Negativní signály se logují | Nezavírat; čekat na stáří 72 h. |
| Stará pozice, blízko break-even | Libovolný | Nezavírat jen kvůli hybridu. |
| Stará pozice, mimo break-even | Aspoň dva negativní reason codes z nové uzavřené H1/H4 svíčky | Kandidát `exit_momentum`; v `DRY_RUN` jen zapsat, v live režimu poslat jeden close pokyn. |
| Libovolná způsobilá pozice | Emergency limit překročen | Kandidát `exit_emergency`; zavřít podle samostatných guardů. |

Před živým close se znovu načte konkrétní ticket a aktuální tick. Pokud pozice mezitím zmizela, změnila objem nebo přestala být ztrátová, close se zruší a zaloguje jako `revalidation_failed`. To brání uzavření na zastaralém snapshotu.

### Role AI

AI nemá rozhodovat o automatickém uzavření. Pro tento exit je nevhodná jako poslední autorita: výsledek není deterministický, hůře se testuje, může být nedostupná a odpověď přichází příliš pozdě vzhledem k běžícímu trhu.

AI lze později použít jen v poradním režimu `HYBRID_EXIT_AI_ADVISORY_ENABLED=false`:

- dostane již zalogovaný strukturovaný snapshot kandidáta;
- vrátí pouze `hold` nebo `exit` a krátký důvod;
- výstup se zapíše do `hybrid_loss_exit_events.jsonl` jako `ai_advisory`;
- výstup AI nesmí změnit `decision`, volat `close_position_by_ticket()` ani obcházet deterministické guardy.

Po nasbírání dostatečných dat lze porovnat doporučení AI s outcome logem. Teprve pokud prokazatelně přidává hodnotu, lze jej zvážit jako další **potvrzující** reason code. I v takovém režimu musí zůstat nutná časová, P/L, rollover a limitní ochrana a finální close pravidlo musí být reprodukovatelné bez AI.

## Konfigurace

```env
# Zapne hybrid jako jediný automatický mechanismus pro ztrátové pozice.
HYBRID_EXIT_ENABLED=true
# true pouze loguje rozhodnutí; false smí po splnění všech guardů odeslat market close.
HYBRID_EXIT_DRY_RUN=true
# Lokální datum od kterého jsou pozice způsobilé pro hybrid; starší ticket se nikdy neuzavře.
HYBRID_EXIT_ANALYSIS_START_DATE=2026-08-01
# IANA časové pásmo pro převod počátečního data a určení obchodního víkendu.
HYBRID_EXIT_TIMEZONE=Europe/Prague
# Minimální rozestup kontrol; rozhodnutí se navíc mění jen po nové uzavřené H1 svíčce.
HYBRID_EXIT_CHECK_INTERVAL_MINUTES=15
# Aktivní obchodní hodiny, po kterých přestává být pozice mladá; víkend se nezapočítává.
HYBRID_EXIT_YOUNG_HOURS=24
# Aktivní obchodní hodiny, od kterých je pozice stará a může splnit momentum exit.
HYBRID_EXIT_OLD_HOURS=72
# Minimální USD buffer pod nulou, ve kterém je stará pozice stále považována za blízkou break-even.
HYBRID_EXIT_BREAK_EVEN_BUFFER_USD=0.20
# Maximální čistá ztráta jedné způsobilé pozice jako procento equity pro interní emergency exit.
HYBRID_EXIT_EMERGENCY_ACCOUNT_LOSS_PERCENT=1.0
# Nejvyšší počet pozic, které hybrid smí uzavřít při jednom vyhodnocení.
HYBRID_EXIT_MAX_CLOSES_PER_CYCLE=1
# true zahrne manuální pozice s magic=0; doporučené false je ponechá mimo hybrid.
HYBRID_EXIT_MANAGE_MANUAL_POSITIONS=false
# Rozsah diagnostiky: actions = jen akce, eligible = způsobilé pozice, all = všechny pozice.
HYBRID_EXIT_DIAGNOSTICS_LEVEL=eligible
# Hodiny po rozhodnutí, kdy se uloží následný výsledek pro vyhodnocení kvality pravidel.
HYBRID_EXIT_OUTCOME_HORIZONS_HOURS=24,48
# Pouze poradní AI zápis do diagnostiky; nesmí změnit pravidlo ani vyvolat close.
HYBRID_EXIT_AI_ADVISORY_ENABLED=false
# Začátek uzavření trhu v lokálním čase; od této hranice se stáří pozice nezvyšuje.
HYBRID_EXIT_MARKET_CLOSE_WEEKDAY=4
# Hodina lokálního pátečního uzavření (0=pondělí ... 4=pátek).
HYBRID_EXIT_MARKET_CLOSE_HOUR=23
# Minuta lokálního pátečního uzavření.
HYBRID_EXIT_MARKET_CLOSE_MINUTE=0
# Konec uzavření trhu v lokálním čase; od této hranice se stáří opět zvyšuje.
HYBRID_EXIT_MARKET_OPEN_WEEKDAY=6
# Hodina lokálního nedělního otevření (6=neděle).
HYBRID_EXIT_MARKET_OPEN_HOUR=23
# Minuta lokálního nedělního otevření.
HYBRID_EXIT_MARKET_OPEN_MINUTE=0
```

Datum validovat striktně ve formátu `YYYY-MM-DD`. Nevalidní nebo chybějící datum je bezpečnostní chyba: modul se nesmí spustit, ne použít tichý default. Při startu má vypsat lokální i UTC hranici a počet nalezených pozic v kategoriích `legacy`, `young`, `middle`, `old`.

### Víkend a stáří pozice

Stáří pro hybrid není obyčejný rozdíl kalendářních hodin. Je to **aktivní obchodní stáří**: interval, kdy je daný trh podle nakonfigurovaného týdenního okna otevřený. Čas mezi `HYBRID_EXIT_MARKET_CLOSE_*` a `HYBRID_EXIT_MARKET_OPEN_*` se do `age_hours` nezapočítává.

Příklad při konfiguraci pátek 23:00 až neděle 23:00 v `Europe/Prague`:

- pozice otevřená v pátek ve 22:00 má při uzavření trhu aktivní stáří 1 hodinu;
- v pondělí ve 22:00 má aktivní stáří 24 hodin, ne kalendářních 72 hodin;
- do hranice 72 aktivních hodin se dostane až po dalších 48 otevřených hodinách trhu, přibližně ve středu ve 22:00.

Pro nástroje s odlišným session kalendářem, například krypto nebo CFD, nesmí být tento FX víkendový kalendář použit naslepo. První verze hybridu má proto zpracovávat jen instrumenty se stejným konfigurovaným obchodním oknem; jiný typ instrumentu musí skončit jako `hold_unsupported_market_session`, dokud nedostane vlastní session konfiguraci.

Při výpočtu se nesmí spoléhat na to, že je bot přes víkend spuštěný. Funkce `calculate_active_market_age(opened_at, now, session_calendar)` musí odečíst celé uzavřené víkendové intervaly mezi časem otevření a aktuálním časem. Při každém průchodu se do diagnostiky uloží `calendar_age_hours`, `market_closed_hours` a `active_market_age_hours`, aby bylo vidět přesný důvod zařazení do věkové vrstvy.

## Diagnostický audit

Samotný `hybrid_loss_exit.csv` nestačí. Umí ukázat, co bot udělal, ale ne proč konkrétně pozici vybral, jaký byl stav trhu ani zda by odložený nebo provedený exit dopadl lépe. Hybrid má proto zapisovat tři vzájemně propojené vrstvy. Každý průchod dostane `evaluation_id` typu `YYYYMMDDTHHMMSSZ-uuid` a všechny záznamy nad stejným ticketem sdílejí `position_key = ticket`.

### 1. Přehled akcí: `trade_logs/hybrid_loss_exit.csv`

Jeden řádek pouze pro kandidáta, pokus o uzavření nebo provedené uzavření. CSV slouží pro rychlé otevření v Excelu a kontrolu výsledků.

Povinná pole: `timestamp_utc`, `evaluation_id`, `ticket`, `symbol`, `magic`, `opened_at_utc`, `opened_at_prague`, `age_hours`, `age_band`, `net_profit`, `legacy`, `eligible`, `decision`, `close_reason`, `dry_run`, `close_attempted`, `closed`, `mt5_retcode`, `mt5_message`.

`decision` musí být jedna z hodnot `legacy_excluded`, `young_hold`, `middle_hold`, `old_hold_near_break_even`, `old_hold_momentum_ok`, `exit_momentum`, `exit_emergency`, `skipped_rollover`, `skipped_cycle_limit` nebo `close_failed`. Pole `close_reason` obsahuje strojově čitelné reason codes oddělené čárkou, ne jen volný text.

### 2. Rozhodovací události: `trade_logs/hybrid_loss_exit_events.jsonl`

JSONL bude hlavní forenzní zdroj. Zapisuje se pro každou způsobilou pozici v každém vyhodnocení; při `HYBRID_EXIT_DIAGNOSTICS_LEVEL=all` také pro legacy a mladé pozice. Jeden event obsahuje celý vstup rozhodovacího stromu:

```json
{
	"timestamp_utc": "2026-08-04T12:00:00+00:00",
	"evaluation_id": "20260804T120000Z-example",
	"ticket": 12345678,
	"symbol": "EURUSD_ecn",
	"position": {
		"side": "BUY",
		"magic": 234200,
		"opened_at_utc": "2026-08-01T22:30:00+00:00",
		"age_hours": 61.5,
		"volume": 0.01,
		"profit": -4.10,
		"swap": -0.03,
		"modeled_fee": 0.10,
		"net_profit": -4.23
	},
	"eligibility": {
		"analysis_start_utc": "2026-07-31T22:00:00+00:00",
		"legacy": false,
		"strategy_owned": true,
		"in_swap_block": false,
		"age_band": "middle"
	},
	"market": {
		"h1_close": 1.0812,
		"h1_ema20": 1.0831,
		"h1_previous_swing_low": 1.0820,
		"h1_new_adverse_extreme": true,
		"h4_close": 1.0812,
		"h4_ema20": 1.0850,
		"h4_adverse_trend_confirmed": true,
		"source_bar_times_utc": {"h1": "2026-08-04T11:00:00+00:00", "h4": "2026-08-04T08:00:00+00:00"}
	},
	"rules": {
		"break_even_threshold": -0.20,
		"near_break_even": false,
		"momentum_reason_codes": ["h1_new_low", "h1_below_ema20", "h4_downtrend"],
		"required_momentum_signals": 2,
		"matched_momentum_signals": 3,
		"emergency_triggered": false
	},
	"decision": {"value": "exit_momentum", "would_close": true, "dry_run": true}
}
```

Ukládat pouze poslední uzavřené H1 a H4 svíčky a odvozené indikátory použité pravidlem. Neukládat celý historický candle dataset při každém průchodu; výrazně by rostl objem logů, aniž by to zlepšilo audit rozhodnutí.

### 3. Následný výsledek rozhodnutí: `trade_logs/hybrid_loss_exit_outcomes.jsonl`

Nejdůležitější vrstva pro ladění pravidel. Když hybrid vytvoří `exit_momentum`, `old_hold_momentum_ok` nebo `old_hold_near_break_even`, uloží si lehký stav do `hybrid_loss_exit_outcome_state.json`. Po horizontech z `HYBRID_EXIT_OUTCOME_HORIZONS_HOURS` zapíše, co se s rozhodnutím stalo.

Záznam obsahuje původní `evaluation_id`, ticket, rozhodnutí, cenu a net P/L při rozhodnutí, stav po 24 h a 48 h, zda pozice zůstala otevřená, případně skutečný čas/cenu/důvod uzavření z historie MT5. Pro hypotetický exit dopočítá také změnu ceny a P/L proti okamžiku rozhodnutí.

Tento log odpoví na otázky, které běžný audit neumí:

- Kolik signálů `exit_momentum` by se do 24/48 h vrátilo k break-even, tedy byly falešné exity?
- Jak velká dodatečná ztráta vznikla tím, že `old_hold_momentum_ok` zůstal otevřený?
- Liší se výsledky podle symbolu, směru, věkové vrstvy nebo strategie/magic?

Pokud pozice skončí dříve jiným legitimním mechanismem, outcome se nesmí tvářit jako tržní výsledek hybridu. Musí uvést `outcome_status=position_closed_elsewhere` a skutečný close reason, pokud jej lze z MT5 historie zjistit.

### Provozní souhrn

Na konci každého průchodu zapsat jeden `hybrid_loss_exit_cycle` event do `position_management_monitor.jsonl`: počet všech pozic, legacy, nezpůsobilých, mladých, středních, starých, kandidátů, hypotetických exitů, pokusů o close, úspěšných close a nejčastější reason codes. To dovolí sledovat, zda modul skutečně běží, aniž by bylo nutné otevírat detailní JSONL soubor.

`HYBRID_EXIT_DIAGNOSTICS_LEVEL` má podporovat hodnoty `actions`, `eligible` a `all`; doporučený start je `eligible`. Pro `all` nastavit retenční limit, například 90 dnů nebo 100 MB na soubor, a při překročení provést rotaci. CSV se nesmí používat jako jediný zdroj pravdy, protože neumí bezpečně nést strukturovaná pole ani historii rozhodnutí.

## Ověření

Povinné testy před live režimem:

1. Pozice z `2026-07-31 21:59 UTC` se v srpnu označí jako `legacy` a nikdy se nedostane mezi kandidáty.
2. Pozice z `2026-07-31 22:00 UTC` se označí jako způsobilá; ověřuje se hranice pražské půlnoci.
3. Mladá ztrátová pozice se nezavře ani při negativním momentu, pokud není emergency.
4. Střední pozice se bez dvou potvrzujících signálů nezavře.
5. Stará ztrátová pozice se zavře jen při negativním momentu a mimo break-even buffer.
6. Starý ticket zůstane nedotčen i v live režimu.
7. `DRY_RUN` nesmí volat `close_position_by_ticket()`.
8. Rollover blokace a limit jednoho uzavření za cyklus mají přednost před výběrem kandidáta.
9. Každá akce nebo kandidát má shodné `evaluation_id` v CSV i JSONL eventu a obsahuje všechny reason codes.
10. Outcome záznam po 24/48 h správně rozliší otevřenou pozici, pozici uzavřenou hybridem a pozici uzavřenou jiným mechanismem.
11. Neuzavřená H1 svíčka nesmí vyvolat `exit_momentum`; stejná uzavřená svíčka musí dát shodný výsledek při opakovaném vyhodnocení.
12. Pokud se ticket nebo P/L mezi vyhodnocením a close změní, hybrid nesmí odeslat pokyn bez nové validace.
13. Zapnutý AI advisory nesmí ovlivnit deterministické `decision` ani vyvolat MT5 close pokyn.

## Postup nasazení

1. Odstranit denní, týdenní a měsíční loss-cleanup moduly, jejich importy/volání v `account_monitor.py`, testy, konfigurační klíče a dokumentaci.
2. Přidat hybridní modul, config parser a testy; zatím bez zásahu do vstupní logiky.
3. Napojit jej do `account_monitor.py` v `DRY_RUN=true` jako jediný loss-exit.
4. Nechat sbírat audit nejméně několik obchodních cyklů. Porovnat u označených kandidátů cenu při signálu s cenou po 24/48 hodinách a podíl falešných exitů.
5. Teprve potom zapnout `HYBRID_EXIT_DRY_RUN=false`, stále s maximem jednoho uzavření za cyklus.

Tento návrh řeší stárnutí nových pozic bez veřejného SL a zároveň chrání současný inventář pozic. Neřeší však riziko výpadku bota ani gap trhu; pro účet s vysokou pákou proto zůstává vhodné mít zvlášť definovaný portfolio kill-switch pro nové vstupy.