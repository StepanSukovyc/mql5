Analyzuj a rozšiř existující Python projekt automatického obchodního systému umístěný v:

C:\Users\Stepa\Programovani\GitHub\StepanSukovyc\mql5\bots\analysis\ollama

Neprováděj slepé přepsání projektu. Nejdříve projdi existující zdrojové soubory, README.md, .env.example, requirements.txt, testy a aktuální datový tok.

Existující systém:

- běží v Pythonu jako nekonečný obchodní automat,
- připojuje se k lokálnímu MetaTrader 5 terminálu přes Python balíček MetaTrader5,
- používá market_data.py pro získávání tržních dat,
- používá ollama_service.py pro lokální predikce,
- používá Gemini na Vertex AI pro finální rozhodování,
- používá trading_logic.py pro přípravu a filtrování predikcí,
- používá final_decision.py pro finální výběr instrumentu a provedení obchodu,
- používá logika.py jako hlavní orchestraci,
- ukládá auditní a predikční výstupy do SERVICE_DEST_FOLDER,
- má vlastní profit cleanup, swap rollover cleanup a hybrid loss exit logiku.

CÍL

Přidej do projektu ekonomický kalendář MetaTrader 5 ve dvou současně implementovaných vrstvách:

1. Deterministický bezpečnostní filtr:
   blokování nových obchodů v okolí významných makroekonomických událostí.

2. Fundamentální analýza zveřejněných výsledků:
   porovnání actual, forecast a previous hodnoty a její vyhodnocení pomocí Ollamy.

Fundamentální data nesmí nahradit technickou strategii ani přímo provádět obchod.

Musí fungovat pouze jako:

- ochrana před rizikovým načasováním,
- doplňkový kontext pro Ollamu a Gemini,
- doplňkový sentiment pro obchodovaný instrument.

Existující technická obchodní logika musí zůstat funkční i tehdy, když ekonomický kalendář nebo Ollama nejsou dostupné.

============================================================
1. NEJDŘÍVE ANALYZUJ PROJEKT
============================================================

Před implementací:

1. Projdi všechny relevantní Python moduly.
2. Najdi přesná místa, kde:
   - se načítají symboly,
   - se generují Ollama predikce,
   - se vytváří Gemini prompt,
   - se filtrují kandidáti,
   - se vybírá finální instrument,
   - se odesílá nový obchodní pokyn.
3. Ověř aktuální formát Ollama predikčních JSON souborů.
4. Ověř současný způsob práce s časem a timezone.
5. Ověř, zda projekt používá symboly se suffixem, například EURUSD_ecn.
6. Ověř, zda instalovaný Python balíček MetaTrader5 přímo zpřístupňuje ekonomický kalendář MT5.

Nevymýšlej názvy existujících funkcí. Použij skutečnou strukturu nalezenou v projektu.

Před změnou kódu vytvoř stručný implementační plán obsahující:

- seznam souborů, které budou změněny,
- seznam nových souborů,
- popis datového toku,
- integrační body,
- bezpečnostní fallbacky,
- plán testů.

Potom implementaci proveď.

============================================================
2. ZDROJ EKONOMICKÉHO KALENDÁŘE
============================================================

Primární zdroj musí být vestavěný Economic Calendar MetaTrader 5.

Kalendář musí poskytovat minimálně:

- event_id,
- value_id,
- country_id,
- country_code,
- currency,
- event_name,
- importance,
- event_time,
- actual,
- forecast,
- previous,
- revised,
- source,
- retrieved_at.

Důležité:

Nativní funkce Economic Calendar jsou dostupné na straně MQL5 terminálu. Nejprve ověř, zda je lze korektně volat prostřednictvím aktuálně používaného Python balíčku MetaTrader5.

Pokud Python API kalendář neposkytuje, implementuj bezpečný lokální bridge:

- vytvoř minimální MQL5 Expert Advisor nebo Script,
- použij nativní MQL5 calendar funkce,
- získávej události pomocí CalendarValueHistory(),
- metadata události načítej pomocí CalendarEventById(),
- podle potřeby načítej informace o zemi pomocí CalendarCountryById(),
- pravidelně exportuj normalizovaný JSON soubor,
- Python bude tento JSON soubor bezpečně načítat.

Navrhovaný soubor MQL5 bridge:

mt5_economic_calendar_exporter.mq5

Navrhovaný výstup:

MT5 Common Files nebo jiný bezpečně sdílený lokální adresář.

Přesnou výslednou cestu udělej konfigurovatelnou.

Nepoužívej scraping webových stránek.

Nepřidávej placené externí API.

MQL5 bridge nesmí otevírat, měnit ani zavírat obchody. Smí pouze číst kalendář a exportovat data.

============================================================
3. PRÁCE S ČASEM
============================================================

Ekonomický kalendář MT5 pracuje s časem obchodního serveru.

Implementace musí:

- explicitně evidovat původní serverový čas,
- převést čas do UTC,
- pro interní porovnávání používat timezone-aware UTC datetime,
- nikdy neporovnávat naive datetime s timezone-aware datetime,
- nepředpokládat, že čas MT5 serveru je Europe/Prague,
- uložit do auditu původní čas i normalizovaný UTC čas,
- bezpečně řešit přechod letního a zimního času.

Pokud nelze offset serverového času spolehlivě určit, událost nesmí být použita k automatickému blokování obchodu. V takovém případě ji označ jako časově nedůvěryhodnou a zapiš důvod do auditu.

============================================================
4. NOVÉ PYTHON MODULY
============================================================

Vytvoř samostatnou vrstvu, která nebude těsně svázána s trading_logic.py.

Preferovaná struktura:

economic_calendar/
    __init__.py
    models.py
    provider.py
    mt5_file_provider.py
    symbol_mapping.py
    risk_filter.py
    fundamental_analyzer.py
    storage.py

Pokud je s ohledem na současnou strukturu projektu vhodnější jednodušší uspořádání, můžeš jej upravit. Zachovej ale oddělení těchto odpovědností:

- získání kalendářních dat,
- validace a normalizace,
- mapování symbolů na měny a ekonomiky,
- výpočet časového rizika,
- Ollama fundamentální analýza,
- agregace sentimentu,
- ukládání a audit,
- integrace do obchodního procesu.

Použij dataclasses nebo Pydantic modely podle stylu existujícího projektu.

============================================================
5. NORMALIZOVANÝ MODEL UDÁLOSTI
============================================================

Použij interní model podobný tomuto:

{
  "event_id": "string",
  "value_id": "string",
  "country_code": "US",
  "currency": "USD",
  "event_name": "Consumer Price Index",
  "importance": "NONE|LOW|MODERATE|HIGH",
  "event_time_server": "ISO-8601",
  "event_time_utc": "ISO-8601|null",
  "time_is_trusted": true,
  "actual": null,
  "forecast": 2.8,
  "previous": 2.7,
  "revised": null,
  "status": "SCHEDULED|RELEASED|REVISED",
  "source": "MT5_ECONOMIC_CALENDAR",
  "retrieved_at_utc": "ISO-8601"
}

Rozlišuj:

- chybějící hodnotu,
- skutečnou nulu,
- ještě nezveřejněnou actual hodnotu,
- revidovanou hodnotu,
- nevalidní nebo zastaralý záznam.

Nepřeváděj chybějící hodnotu na nulu.

============================================================
6. MAPOVÁNÍ SYMBOLŮ
============================================================

Implementuj:

get_symbol_exposure(symbol) -> SymbolExposure

Nejdříve bezpečně odstraň broker suffix, například:

EURUSD_ecn -> EURUSD

Pro hlavní forex páry:

EURUSD -> EUR, USD
GBPJPY -> GBP, JPY
AUDCAD -> AUD, CAD

Pro drahé kovy:

XAUUSD -> USD a konfigurovatelná expozice GOLD
XAGUSD -> USD a konfigurovatelná expozice SILVER

Pro kryptoměny:

BTCUSD -> USD a CRYPTO
ETHUSD -> USD a CRYPTO

Klasický ekonomický kalendář používej u kryptoměn pouze pro fiat část, například USD. Netvrď, že USD makrodata představují kompletní fundament BTC nebo ETH.

Pro indexy a CFD nevytvářej neověřené mapování automaticky. Použij konfigurovatelnou mapu v .env nebo samostatném JSON souboru.

Příklad konfigurace:

ECONOMIC_SYMBOL_EXPOSURE_MAP={
  "XAUUSD":["USD","GOLD"],
  "XAGUSD":["USD","SILVER"],
  "BTCUSD":["USD","CRYPTO"]
}

Pokud symbol nelze namapovat:

- vrať stav UNMAPPED,
- nepoužívej fundamentální sentiment,
- neblokuj obchod pouze na základě dohadu,
- zapiš informaci do auditu,
- pokračuj původní technickou logikou.

============================================================
7. BEZPEČNOSTNÍ EVENT RISK FILTER
============================================================

Implementuj deterministickou funkci:

evaluate_event_risk(symbol, now_utc, events) -> EventRiskResult

Výsledek musí obsahovat:

{
  "symbol": "EURUSD_ecn",
  "risk_level": "NONE|LOW|MEDIUM|HIGH|UNKNOWN",
  "allow_new_trade": true,
  "relevant_currencies": ["EUR", "USD"],
  "blocking_events": [],
  "warning_events": [],
  "reason_codes": [],
  "calendar_status": "AVAILABLE|STALE|UNAVAILABLE|INVALID"
}

Výchozí pravidla:

1. HIGH událost:
   - blokuj nový obchod X minut před událostí,
   - blokuj nový obchod Y minut po události.

2. MODERATE událost:
   - standardně pouze warning,
   - volitelně může být blokující podle konfigurace.

3. LOW a NONE:
   - standardně neblokují.

4. Událost musí být relevantní alespoň pro jednu expozici symbolu.

5. Filtr se vztahuje pouze na nové vstupy.

6. Filtr nesmí:
   - zavírat existující pozice,
   - měnit stop-loss,
   - měnit take-profit,
   - zasahovat do profit cleanup,
   - zasahovat do swap rollover cleanup,
   - zasahovat do hybrid loss exit.

7. Je-li najednou relevantních více událostí, použij nejvyšší riziko.

8. Blokování musí být deterministické. LLM nesmí rozhodovat, zda je časové okno blokující.

Výchozí konfigurace:

ECONOMIC_CALENDAR_ENABLED=true
ECONOMIC_CALENDAR_PROVIDER=mt5
ECONOMIC_CALENDAR_REFRESH_SECONDS=300
ECONOMIC_CALENDAR_LOOKAHEAD_HOURS=24
ECONOMIC_CALENDAR_MAX_DATA_AGE_MINUTES=15

ECONOMIC_HIGH_BLOCK_BEFORE_MINUTES=30
ECONOMIC_HIGH_BLOCK_AFTER_MINUTES=30

ECONOMIC_MODERATE_BLOCK_ENABLED=false
ECONOMIC_MODERATE_BLOCK_BEFORE_MINUTES=15
ECONOMIC_MODERATE_BLOCK_AFTER_MINUTES=15

ECONOMIC_FAIL_MODE=OPEN

Význam ECONOMIC_FAIL_MODE:

OPEN:
- při nedostupném nebo nevalidním kalendáři pokračuj původní technickou logikou,
- zapiš warning a audit.

CLOSED:
- při nedostupném nebo nevalidním kalendáři neotevírej nové obchody,
- existující pozice zůstanou nedotčené.

Výchozí hodnota musí být OPEN, aby nová funkce po nasazení nezastavila celý systém.

============================================================
8. OLLAMA FUNDAMENTÁLNÍ ANALÝZA
============================================================

Ollamu používej pouze pro události, které již byly zveřejněny a obsahují použitelné hodnoty.

Ollama nemá rozhodovat o tom, zda se má provést obchod.

Ollama má pouze vyhodnotit pravděpodobný dopad jedné zveřejněné ekonomické události na její měnu.

Analyzuj například:

- actual versus forecast,
- actual versus previous,
- případnou revised hodnotu,
- směr dopadu na příslušnou měnu,
- sílu a důvěryhodnost hodnocení.

Prompt musí být stručný, deterministický a vyžadovat pouze validní JSON.

Příklad významu promptu:

You are evaluating one published macroeconomic event.

Do not recommend a trade.
Do not evaluate a currency pair.
Do not invent missing values.
Use only the supplied event data.

Currency: USD
Event: Consumer Price Index
Importance: HIGH
Actual: 3.4
Forecast: 2.8
Previous: 2.7
Revised: null

Return only JSON matching the required schema.

Požadované schéma odpovědi:

{
  "currency": "USD",
  "event_id": "string",
  "direction": "BULLISH|BEARISH|NEUTRAL|UNKNOWN",
  "sentiment_score": 0.0,
  "confidence": 0,
  "surprise_direction": "ABOVE|BELOW|INLINE|UNKNOWN",
  "reason": "short factual explanation",
  "data_quality": "COMPLETE|PARTIAL|INSUFFICIENT"
}

Pravidla:

- sentiment_score musí být v rozsahu -1.0 až 1.0,
- confidence musí být v rozsahu 0 až 100,
- reason musí být krátký,
- odpověď musí projít striktní validací,
- nevalidní JSON jednou oprav nebo opakuj request,
- po druhém selhání vrať UNKNOWN,
- žádnou nevalidní odpověď nepoužívej v obchodním rozhodování,
- actual=null znamená, že událost ještě není analyzovatelná,
- Ollama nesmí doplňovat chybějící čísla,
- ukládej název modelu a čas analýzy,
- zabraň opakované analýze stejného value_id.

Před produkčním použitím zvaž, zda současný model OLLAMA_MODEL=deepseek-coder-v2 je vhodný pro makroekonomickou interpretaci. Neměň model bez konfigurace. Umožni nastavit samostatný model:

ECONOMIC_OLLAMA_MODEL=

Pokud není nastaven, použij stávající OLLAMA_MODEL.

============================================================
9. AGREGACE FUNDAMENTÁLNÍHO SENTIMENTU
============================================================

Implementuj agregaci po měnách:

USD
EUR
GBP
JPY
CHF
AUD
CAD
NZD

Agregace musí:

- používat pouze validní analyzované události,
- zohlednit importance,
- zohlednit confidence,
- zohlednit stáří události,
- umožnit postupné časové oslabování vlivu,
- nechat staré události expirovat,
- zachovat možnost dohledat, které události výsledek vytvořily.

Nevytvářej sentiment, pokud nejsou dostatečná data.

Místo hodnoty 0 použij stav NO_DATA, pokud žádný fundament není dostupný.

Výstup měny:

{
  "currency": "USD",
  "status": "AVAILABLE|NO_DATA|STALE|INVALID",
  "sentiment_score": 0.42,
  "confidence": 76,
  "contributing_events": ["event/value identifiers"],
  "generated_at_utc": "ISO-8601"
}

Výchozí návrh váhy:

- HIGH: 1.0
- MODERATE: 0.5
- LOW: 0.2
- NONE: 0.0

Skutečný vzorec implementuj v čisté, testovatelné funkci a popiš jej v dokumentaci.

Pro forex pár vypočítej sentiment pouze tehdy, když jsou dostupné obě měny:

pair_sentiment = base_currency_sentiment - quote_currency_sentiment

Výsledek omez do rozsahu -1.0 až 1.0.

Pokud je dostupná jen jedna strana páru:

- nepočítej falešně plný pair sentiment,
- vrať stav PARTIAL,
- předej modelu dostupnou měnu samostatně,
- sniž confidence.

Pokud nejsou dostupná žádná data:

- vrať NO_DATA,
- technická strategie pokračuje beze změny.

============================================================
10. INTEGRACE DO OLLAMA PREDIKCE
============================================================

Rozšiř existující prompt v ollama_service.py o strukturovanou sekci:

fundamental_context

Obsah:

{
  "calendar_status": "AVAILABLE|STALE|UNAVAILABLE|INVALID",
  "symbol_mapping_status": "MAPPED|PARTIAL|UNMAPPED",
  "risk_level": "NONE|LOW|MEDIUM|HIGH|UNKNOWN",
  "new_trade_blocked": false,
  "upcoming_relevant_events": [],
  "recent_published_events": [],
  "base_currency_sentiment": {},
  "quote_currency_sentiment": {},
  "pair_sentiment": {},
  "data_generated_at_utc": "ISO-8601"
}

Nevkládej do promptu celý kalendář.

Vlož pouze:

- události relevantní pro aktuální symbol,
- omezený počet nejbližších budoucích událostí,
- omezený počet nedávných zveřejněných událostí,
- agregované skóre,
- jednoznačnou informaci o dostupnosti a stáří dat.

Ollama predikce musí ve výstupu zachovat původní pole a navíc obsahovat:

{
  "fundamental_context_used": true,
  "fundamental_data_status": "AVAILABLE|PARTIAL|NO_DATA|STALE|INVALID",
  "event_risk_level": "NONE|LOW|MEDIUM|HIGH|UNKNOWN",
  "pair_fundamental_sentiment": null
}

Zachovej zpětnou kompatibilitu existujících JSON predikcí.

============================================================
11. INTEGRACE DO GEMINI FINÁLNÍHO ROZHODNUTÍ
============================================================

Rozšiř finální Gemini kontext o stejné normalizované fundamentální informace.

Gemini může fundamentální kontext zohlednit při:

- výběru mezi technicky podobnými kandidáty,
- snížení confidence,
- rozhodnutí SKIP,
- vysvětlení rizika.

Gemini nesmí obejít deterministický HIGH event block.

Pokud je instrument blokován risk filtrem, musí být odstraněn ze seznamu kandidátů před finálním výběrem.

Nespoléhej na to, že Gemini zákaz pochopí pouze z promptu.

Do finálního auditu přidej:

- zda byl fundament dostupný,
- zda byl použit,
- jaký byl pair sentiment,
- zda byl symbol blokován,
- která událost blokování způsobila,
- reason codes.

============================================================
12. POŘADÍ V OBCHODNÍM TOKU
============================================================

Požadované pořadí pro nové vstupy:

1. Načti technická data.
2. Načti validní snapshot ekonomického kalendáře.
3. Urči ekonomické expozice symbolu.
4. Vyhodnoť deterministické event risk window.
5. Pokud je nový vstup blokován:
   - nevytvářej nový obchod,
   - zapiš audit,
   - pokračuj dalším symbolem.
6. Pokud není blokován:
   - načti dostupný fundamentální sentiment,
   - přidej jej do Ollama kontextu,
   - proveď stávající technickou predikci.
7. Zachovej stávající filtry minimální síly signálu.
8. Před finálním Gemini rozhodnutím znovu ověř, že event risk snapshot není zastaralý.
9. Proveď finální rozhodnutí podle existující logiky.
10. Bezprostředně před order_send proveď poslední rychlou revalidaci event risk filtru.
11. Pokud mezitím vstoupila relevantní událost do blokovacího okna:
    - obchod neodesílej,
    - zapiš reason code ECONOMIC_EVENT_BLOCKED_AT_EXECUTION.

Tato poslední kontrola je nutná kvůli možné prodlevě mezi vytvořením predikce a odesláním obchodu.

============================================================
13. UKLÁDÁNÍ A CACHE
============================================================

Ukládej normalizované výstupy do:

SERVICE_DEST_FOLDER/economic_calendar/upcoming_events.json

SERVICE_DEST_FOLDER/economic_calendar/released_events.json

SERVICE_DEST_FOLDER/economic_calendar/currency_sentiment.json

SERVICE_DEST_FOLDER/economic_calendar/pair_sentiment/{symbol}.json

Používej atomický zápis souborů:

- zápis nejprve do dočasného souboru,
- flush,
- bezpečné nahrazení cílového souboru.

Zabraň čtení částečně zapsaného JSON souboru.

Každý snapshot musí obsahovat:

- schema_version,
- generated_at_utc,
- source,
- source_status,
- data_age_seconds.

Implementuj deduplikaci pomocí event_id a value_id.

============================================================
14. AUDIT
============================================================

Vytvoř:

SERVICE_DEST_FOLDER/trade_logs/economic_calendar_filter.csv

Minimální sloupce:

timestamp_utc
symbol
mapping_status
calendar_status
risk_level
allow_new_trade
event_id
value_id
event_name
currency
importance
event_time_utc
minutes_to_event
decision
reason_code

Vytvoř také strukturovaný audit:

SERVICE_DEST_FOLDER/trade_logs/economic_calendar_events.jsonl

Do JSONL zapisuj:

- přijetí a normalizaci události,
- změnu scheduled na released,
- výsledek Ollama analýzy,
- validační chybu,
- stale data,
- blokování nového obchodu,
- fail-open nebo fail-closed rozhodnutí.

Neloguj:

- heslo k MT5,
- service account JSON,
- API klíče,
- celé citlivé .env hodnoty.

============================================================
15. KONFIGURACE
============================================================

Rozšiř .env.example minimálně o:

ECONOMIC_CALENDAR_ENABLED=true
ECONOMIC_CALENDAR_PROVIDER=mt5
ECONOMIC_CALENDAR_REFRESH_SECONDS=300
ECONOMIC_CALENDAR_LOOKAHEAD_HOURS=24
ECONOMIC_CALENDAR_MAX_DATA_AGE_MINUTES=15

ECONOMIC_HIGH_BLOCK_BEFORE_MINUTES=30
ECONOMIC_HIGH_BLOCK_AFTER_MINUTES=30

ECONOMIC_MODERATE_BLOCK_ENABLED=false
ECONOMIC_MODERATE_BLOCK_BEFORE_MINUTES=15
ECONOMIC_MODERATE_BLOCK_AFTER_MINUTES=15

ECONOMIC_FAIL_MODE=OPEN

ECONOMIC_FUNDAMENTAL_ANALYSIS_ENABLED=true
ECONOMIC_OLLAMA_MODEL=
ECONOMIC_OLLAMA_TIMEOUT_SECONDS=120
ECONOMIC_OLLAMA_MAX_RETRIES=1

ECONOMIC_SENTIMENT_MAX_AGE_HOURS=24
ECONOMIC_SENTIMENT_DECAY_HOURS=12

ECONOMIC_CALENDAR_EXPORT_PATH=
ECONOMIC_SYMBOL_EXPOSURE_MAP=

Načítání konfigurace implementuj stejným způsobem jako ve zbytku projektu.

Validuj:

- záporné časové intervaly,
- nepodporovaný fail mode,
- nevalidní JSON mapování,
- neexistující exportní cestu,
- nevalidní model configuration.

============================================================
16. CHOVÁNÍ PŘI CHYBÁCH
============================================================

Při chybě kalendáře:

- nesmí spadnout hlavní trading loop,
- nesmí spadnout account monitor,
- nesmí být ovlivněny existující pozice,
- zapiš warning a strukturovaný audit,
- použij ECONOMIC_FAIL_MODE.

Při chybě Ollamy:

- deterministický event risk filter musí dál fungovat,
- fundamentální sentiment nastav na INVALID nebo NO_DATA,
- pokračuj podle technické strategie,
- nevytvářej náhradní sentiment.

Při poškozeném JSON z MQL5 bridge:

- soubor ignoruj,
- zachovej poslední validní snapshot pouze do maximální povolené doby,
- po překročení maximálního stáří označ kalendář jako STALE,
- použij ECONOMIC_FAIL_MODE.

============================================================
17. TESTY
============================================================

Vytvoř unit testy bez potřeby živého MT5 terminálu a bez volání skutečné Ollamy.

Použij fixtures a mocky.

Otestuj minimálně:

1. EURUSD a budoucí HIGH USD událost.
2. EURUSD a budoucí HIGH EUR událost.
3. GBPJPY a MODERATE JPY událost.
4. XAUUSD a HIGH USD událost.
5. BTCUSD a HIGH USD událost.
6. Symbol se suffixem EURUSD_ecn.
7. Neznámý symbol.
8. Událost mimo blokovací okno.
9. Událost přesně na hranici blokovacího okna.
10. Blokování po zveřejnění události.
11. Více souběžných událostí.
12. Chybějící actual.
13. Actual rovno nule.
14. Revised hodnota.
15. Nevalidní Ollama JSON.
16. Ollama timeout.
17. Nedostupný kalendář ve fail-open režimu.
18. Nedostupný kalendář ve fail-closed režimu.
19. Stale snapshot.
20. Nevalidní timezone.
21. Atomické ukládání JSON.
22. Deduplikace stejného value_id.
23. Jen jedna měna páru má sentiment.
24. Žádná měna páru nemá sentiment.
25. High-impact událost vstoupí do blokovacího okna mezi predikcí a order_send.
26. Risk filtr neovlivní uzavírání existujících pozic.
27. Profit cleanup zůstane funkční.
28. Swap rollover cleanup zůstane funkční.
29. Hybrid loss exit zůstane funkční.
30. Vypnutý ECONOMIC_CALENDAR_ENABLED zachová původní chování.

Přidej integrační test s fixture JSON souborem simulujícím výstup MQL5 bridge.

============================================================
18. DRY-RUN A POSTUPNÉ NASAZENÍ
============================================================

Přidej konfiguraci:

ECONOMIC_FILTER_DRY_RUN=true

V dry-run režimu:

- vypočítej, zda by byl obchod blokován,
- zapiš rozhodnutí do auditu,
- skutečný obchod neblokuj,
- přidej pole would_block=true/false.

Výchozí hodnota musí být true.

Přidej statistický výstup umožňující pozdější vyhodnocení:

- kolik obchodů by filtr povolil,
- kolik by zablokoval,
- podle jaké měny,
- podle jaké importance,
- zda byl obchod podle pozdějšího výsledku ziskový nebo ztrátový, pokud to lze napojit na existující trade log bez změny jeho významu.

Nevytvářej tvrzení, že filtr zvyšuje ziskovost. Pouze sbírej auditní data pro pozdější vyhodnocení.

============================================================
19. DOKUMENTACE
============================================================

Aktualizuj README.md a popiš:

- architekturu ekonomického kalendáře,
- proč je případně nutný MQL5 bridge,
- jak nasadit a zkompilovat mt5_economic_calendar_exporter.mq5,
- kam bridge zapisuje JSON,
- jak nastavit .env,
- rozdíl mezi risk filtrem a Ollama sentimentem,
- význam fail-open a fail-closed,
- význam dry-run režimu,
- práci se serverovým časem MT5 a UTC,
- význam AVAILABLE, PARTIAL, NO_DATA, STALE a INVALID,
- podporované symboly,
- postup přidání mapování dalších CFD a indexů,
- příklady auditních záznamů,
- způsob spuštění testů.

README musí výslovně uvést:

- ekonomický filtr blokuje pouze nové vstupy,
- existující pozice spravují stávající exit strategie,
- chybějící fundament neznamená neutrální sentiment,
- NO_DATA není stejné jako sentiment_score=0,
- LLM neposílá obchodní příkazy,
- deterministický risk filtr má přednost před AI doporučením.

============================================================
20. AKCEPTAČNÍ KRITÉRIA
============================================================

Implementace je hotová pouze tehdy, když:

1. Projekt lze spustit s ECONOMIC_CALENDAR_ENABLED=false a chová se stejně jako před změnou.
2. Ekonomický kalendář lze zapnout bez narušení hlavního trading loopu.
3. HIGH relevantní událost je správně rozpoznána pro obě měny forex páru.
4. Risk filtr pracuje bez Ollamy.
5. Ollama analyzuje pouze zveřejněné události s použitelnými hodnotami.
6. Neúplná fundamentální data nezastaví technickou strategii.
7. Neznámý symbol není automaticky blokován na základě dohadu.
8. Existující pozice nejsou novým modulem zavírány ani měněny.
9. Před order_send proběhne poslední revalidace blokovacího okna.
10. Všechny nové testy projdou.
11. Existující testy zůstanou funkční.
12. JSON výstupy jsou zapisovány atomicky.
13. Audit dokáže vysvětlit každé blokování nebo selhání.
14. Ve výchozím nastavení je ECONOMIC_FILTER_DRY_RUN=true.
15. README.md a .env.example odpovídají skutečné implementaci.

============================================================
21. OMEZENÍ
============================================================

Nedělej následující:

- nepoužívej web scraping,
- nepřidávej placené externí API,
- nedovol LLM přímo otevírat nebo zavírat obchody,
- nedovol LLM obcházet event risk filter,
- neměň význam stávajících cleanup strategií,
- nepovažuj chybějící data za neutrální sentiment,
- nevytvářej falešné fundamentální mapování neznámých CFD,
- nevkládej všechny události do každého promptu,
- nezapisuj secrets do logu,
- neměň stávající produkční konfiguraci automaticky,
- nezapínej skutečné blokování bez předchozího dry-run režimu,
- nevytvářej druhý samostatný obchodní systém.

Na konci práce:

1. Vypiš změněné a vytvořené soubory.
2. Popiš výsledný datový tok.
3. Uveď všechny nové .env proměnné.
4. Uveď příkazy pro spuštění testů.
5. Uveď postup kompilace a nasazení MQL5 bridge, pokud byl potřeba.
6. Uveď známá omezení.
7. Uveď bezpečný postup přechodu:
   - calendar disabled,
   - calendar enabled + dry-run,
   - vyhodnocení auditů,
   - produkční blokování.
