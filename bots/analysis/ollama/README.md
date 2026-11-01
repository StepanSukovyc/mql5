# Economic calendar

The optional Economic Calendar layer protects **new entries only**. Existing positions continue to be managed by the profit cleanup and swap rollover cleanup. The deterministic event-risk filter always takes precedence over AI context; Ollama and Gemini never send orders.

The installed Python `MetaTrader5` package does not expose Economic Calendar functions, so compile [mql/experts/mt5_economic_calendar_exporter.mq5](../../../mql/experts/mt5_economic_calendar_exporter.mq5) in MetaEditor and attach it to any chart. It is read-only and exports `economic_calendar.json` to MT5 Common Files every `RefreshSeconds`. Set `ECONOMIC_CALENDAR_EXPORT_PATH` to that file's absolute path.

The exporter retains server timestamp and writes UTC only when its server-to-UTC offset can be measured. Events with untrusted time never block a trade. Python reads its JSON atomically, normalizes all comparisons to aware UTC datetimes, and writes snapshots to `SERVICE_DEST_FOLDER/economic_calendar/` plus `trade_logs/economic_calendar_filter.csv` and `economic_calendar_events.jsonl`.

Enable gradually: first `ECONOMIC_CALENDAR_ENABLED=false`; then enable it with `ECONOMIC_FILTER_DRY_RUN=true`; inspect audits; only then set dry-run to `false`. `ECONOMIC_FAIL_MODE=OPEN` (the default) preserves prior behavior on unavailable, invalid, or stale data; `CLOSED` rejects only new entries. `AVAILABLE`, `PARTIAL`, `NO_DATA`, `STALE`, and `INVALID` are explicit states: `NO_DATA` is not a neutral score.

Forex mappings strip broker suffixes such as `EURUSD_ecn`; major pairs, XAU/XAG and BTC/ETH USD exposure are supported. Crypto calendar context represents only its fiat side. Unknown CFD/index symbols remain `UNMAPPED` unless explicitly supplied through `ECONOMIC_SYMBOL_EXPOSURE_MAP`; they are not blocked by guesswork.

Run offline calendar tests with:

```powershell
cd bots/analysis/ollama
..\..\..\.venv\Scripts\python.exe -m pytest tests/test_economic_calendar.py -q
```

# MT5 Hourly Collector (Python)

Automatický obchodní systém s AI rozhodováním. Skript běží jako **nekonečný automat**: kontroluje marži, stahuje data, filtruje signály, vytváří finální obchodní doporučení pomocí Gemini na **Vertex AI v Google Cloud**, **automaticky provádí obchody** a **opakuje celý cyklus**.

**Nově přidáno**: Paralelní **Ollama Service** - nezávislá smyčka generující predikce pomocí lokálního AI modelu.

## Co skript dělá

**Hlavní cyklus (Gemini AI):**

1. Načte konfiguraci z `.env`.
2. Připojí se k MetaTrader 5 (`MetaTrader5` Python package).
3. Spustí monitoring volné marže.
4. Jakmile marže > 20%:
   - Zkontroluje, zda existují předpovědi z **aktuální hodiny**
   - **Pokud ano**: používá je (bez stahování nových dat)
   - **Pokud ne**: stáhne data a získá nové předpovědi od Gemini AI
  - Při tvorbě predikce pro každý symbol nejdřív zkontroluje `SERVICE_DEST_FOLDER/ollama/predikce/{symbol}.json`
  - Pokud je `timestamp` v souboru čerstvý (max 1 hodina), reuse-ne Ollama predikci místo volání Gemini
  - Pokud Ollama predikce neexistuje / je nevalidní / je starší než 1h, pak:
    - při `OLLAMA_FALLBACK_TO_GEMINI=true` použije fallback `ask_gemini_prediction`, ale jen pro prvních `OLLAMA_GEMINI_FALLBACK_MAX_INSTRUMENTS` instrumentů v daném cyklu
    - při `OLLAMA_FALLBACK_TO_GEMINI=false` instrument ignoruje
  - Gemini fallback dotazy mohou běžet paralelně, omezené přes `GEMINI_FALLBACK_MAX_PARALLEL_REQUESTS`
  - Po vyčerpání limitu Gemini fallbacku se další instrumenty bez čerstvé Ollama predikce přeskočí
  - Pokud po tomto filtrování nezůstane žádný instrument s použitelnou predikcí, celý cyklus se přeskočí bez nákupu
5. Filtruje slabé předpovědi (BUY < 35% AND SELL < 35% → smaže)
6. Dělá **finální rozhodnutí**:
   - Kombinuje zbývající predikce se stavem účtu a otevřenými pozicemi
  - Cloud Ollama poskytuje advisory ranking instrumentu a směru; finální vstup schvalují lokální pravidla
  - V promptu zohledňuje swing styl (nejde o intraday), denní cíl ziskovosti a poplatek `0.10 USD` za každých `0.01` lotu
7. **Lokální signal/risk exekuce**:
  - `lot_size` a `take_profit` se počítají lokálně v `risk_engine.py`
  - Pořadí obchodu nemění sizing, TP ani advisory režim
8. **Profit cleanup** (`PROFIT_CLEANUP_STRATEGY_ENABLED`, default `true`):
  - Account monitor kontroluje pozice každou minutu; vyhodnocení se přeskočí během swap blokovacího okna
  - Minutový scénář uzavírá pozice při překročení dynamického cíle `PCZ = max((0.01 × position_volume / reference_volume) × balance, 0.005 USD)`; `reference_volume = (int(balance / 500) + 1) × 0.01`
  - Denní scénář uzavírá pozice v okně 21:50–22:00 `Europe/Prague`, pokud čistý zisk `profit + swap + commission + fee` dosáhne alespoň 0,50 USD
  - Oba scénáře používají skutečnou `commission` a `fee` z MT5 historie dealů podle ID pozice; nedostupná historie se nepovažuje za nulové náklady
  - Přepínač `PROFIT_CLEANUP_STRATEGY_DRY_RUN` (default `true`) pouze vypíše kandidáty a zapíše audit bez skutečného zavření pozic; live uzavírání povolte nastavením na `false`
9. **Swap rollover cleanup** (`SWAP_ROLLOVER_CLEANUP_STRATEGY_ENABLED`, default `true`):
  - Běží v account monitoru každou minutu, ale pouze uvnitř swap blokovacího okna
  - Swap blokovací okno se nyní bere vždy z pevného ručního intervalu z `.env` přes `SWAP_BLOCK_START_*` a `SWAP_BLOCK_END_*`
  - Aktuální interval je `22:30-23:30` v čase `Europe/Prague` a používá se stejně pro lock i rollover cleanup
  - Projde otevřené pozice mimo správu profit protection a scalping
  - Spočítá čistý zisk `ZISK = profit + swap + commission + fee` pomocí skutečných nákladů konkrétní pozice z MT5 historie dealů
  - Pokud je čistý zisk alespoň `0.10 USD`, pozice je vhodná k uzavření kvůli vyhnutí se swapu
  - Audit log zapisuje i skip/no-candidate průchody, takže je vidět, zda strategie byla mimo okno nebo uvnitř okna nic nenašla
  - Přepínač `SWAP_ROLLOVER_CLEANUP_STRATEGY_DRY_RUN` (default `true`) pouze vypíše kandidáty a zapíše audit bez skutečného zavření pozic
10. **Provede obchod** na MT5 podle aktivního režimu
11. Uloží rozhodnutí do `geminipredictions/PREDIKCE_<timestamp>.json`
12. **Vrátí se na krok 3** (restart monitoring)

**Automatické pozastavení v swap blokovacím okně:**
- Blokace se řídí pevným ručním intervalem z `.env`
  - Systém se zastaví v intervalu `SWAP_BLOCK_START_*` až `SWAP_BLOCK_END_*`
  - Jakákoli připravená rozhodnutí se v tomto okně zahodí
  - Aktuální konfigurace je `22:30-23:30` v čase `Europe/Prague`

**Večerní zákaz nových vstupů:**
- Od `21:50` do `00:00` `Europe/Prague` se neotevírají nové pozice.
- Zákaz je ověřen v plánovači i znovu těsně před odesláním vstupního příkazu do MT5.
- Platí pro všechny vstupní strategie; uzavírání a správa stávajících pozic pokračují beze změny.
- Praha se převádí přes časovou zónu IANA, takže přepnutí letního/zimního času je zohledněno.

**Ukončení:** Ctrl+C

## Struktura vystupu

Kazdy soubor ma tvar:

```json
{
  "symbol": "EURUSD_ecn",
  "generated_at": "2026-03-04T10:00:00+00:00",
  "lookback_periods": 30,
  "current_price": 1.08654,
  "candles": {
    "1h": [
      {
        "time": "...",
        "open": 1.082,
        "high": 1.085,
        "low": 1.081,
        "close": 1.084,
        "tick_volume": 1234,
        "spread": 12,
        "real_volume": 0
      }
    ],
    "4h": [...],
    "day": [...],
    "week": [...],
    "month": [...]
  },
  "oscillators": {
    "1h": {
      "rsi": [{ "time": "...", "value": 51.23 }],
      "ma": [{ "time": "...", "value": 1.08654 }]
    },
    "4h": {...},
    "day": {...},
    "week": {...},
    "month": {...}
  }
}
```

## Instalace

Vytvorte virtualni prostredi a nainstalujte zavislosti:

```bash
pip install -r requirements.txt
```

## Konfigurace `.env`

Zkopirujte `bots/analysis/ollama/.env.example` na `bots/analysis/ollama/.env` a upravte hodnoty:

```env
SERVICE_DEST_FOLDER=C:/path/to/output/folder

# Optionalni (defaulty jsou uvedene v zavorce)
MT5_SYMBOL_SUFFIX=_ecn
# Prazdna hodnota vypne suffix filtr a zpracuje vsechny MT5 symboly:
# MT5_SYMBOL_SUFFIX=
MT5_SYMBOL_BLACKLIST=
# Carkou oddelene symboly nebo masky, ktere se maji preskocit, napr. BTCUSD,ETHUSD,X*_ecn,__CFD__
# Pri dosazeni limitu se preskoci cely rozhodovaci cyklus pro nove vstupy
MT5_MAX_OPEN_POSITIONS=17
MT5_CRYPTO_SYMBOL_PATTERNS=BTCUSD*,ETHUSD*,LTCUSD*,BCHUSD*
MT5_MIN_SIGNAL_PERCENT=35
MT5_CRYPTO_MIN_SIGNAL_PERCENT=65
MT5_CRYPTO_LOT_MULTIPLIER=0.25
MT5_CRYPTO_MAX_OPEN_POSITIONS=1
MT5_CRYPTO_ALLOW_FULL_TP_MODE=true
MT5_CRYPTO_TP_DISTANCE_PERCENT=2.0
LOOKBACK_PERIODS=30
ECONOMY_MODE_ENABLED=true
ECONOMY_MODE_INTERVAL_SECONDS=300
RUN_INTERVAL_SECONDS=3600
RSI_PERIOD=14
MA_PERIOD=20
PRETTY_JSON=true

# Optionalni login (kdyz MT5 session neni dostupna automaticky)
# MT5_LOGIN=12345678
# MT5_PASSWORD=your_password
# MT5_SERVER=YourBroker-Server

# Gemini Vertex AI konfigurace pro trading logic
# Používej service account JSON z Google Cloud, ne API key flow.
GOOGLE_APPLICATION_CREDENTIALS=C:/secure/google-cloud/service-account.json
VERTEX_AI_PROJECT_ID=your-gcp-project-id
VERTEX_AI_REGION=eu
VERTEX_AI_MODEL=gemini-4-argon

# Argon: MEDIUM thinking bez thinking_budget, limit alespoň 8192 output tokenů.
# Vyžaduje google-genai>=1.73.1.
# Volitelný API-key fallback na Gemini 2.5 Flash nezaručuje EU zpracování:
# GEMINI_API_KEY=your_api_key
# GEMINI_URL=https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent

PROFIT_CLEANUP_STRATEGY_ENABLED=true
PROFIT_CLEANUP_STRATEGY_DRY_RUN=true
SWAP_ROLLOVER_CLEANUP_STRATEGY_ENABLED=true
SWAP_ROLLOVER_CLEANUP_STRATEGY_DRY_RUN=true

Speciální hodnota `__CFD__` v `MT5_SYMBOL_BLACKLIST` vyřadí všechny instrumenty rozpoznané jako CFD.

# Manualni fallback block window, kdyz MT5 historie neposkytne pouzitelny rollover cas
SWAP_BLOCK_START_HOUR=22
SWAP_BLOCK_START_MINUTE=30
SWAP_BLOCK_END_HOUR=23
SWAP_BLOCK_END_MINUTE=30
# SWAP_ROLLOVER_LOOKBACK_DAYS=14
# SWAP_BLOCK_HALF_WINDOW_MINUTES=30

# Ollama service konfigurace (nezávislé predikce)
OLLAMA_ENABLED=true
OLLAMA_URL=http://localhost:11434/api/generate
OLLAMA_MODEL=deepseek-coder-v2
OLLAMA_PREDICTION_MAX_AGE_MINUTES=120
OLLAMA_FALLBACK_TO_GEMINI=false
OLLAMA_GEMINI_FALLBACK_MAX_INSTRUMENTS=60
GEMINI_FALLBACK_MAX_PARALLEL_REQUESTS=3
OLLAMA_MAX_PARALLEL_REQUESTS=2
OLLAMA_NUM_CTX=16384
OLLAMA_TIMEOUT_SECONDS=600
OLLAMA_REQUEST_DELAY_SECONDS=0.0
OLLAMA_COMPACT_PROMPT=true
```

`GOOGLE_APPLICATION_CREDENTIALS` musí ukazovat na service-account JSON s oprávněním volat Vertex AI. Soubor drž mimo repository a neposílej ho do gitu.

### Automatický balance cap

Strategie automaticky používá aktuální balance zaokrouhlený dolů na celé tisíce USD. `TRADING_ACCOUNT_BALANCE_CAP` se již nenastavuje v `.env`.

- `11125` znamená cap `11000` a bezpečnostní rezervu `125`.
- `9999` znamená cap `9000` a bezpečnostní rezervu `999`.

Rezerva se odečítá také z volné marže před kontrolou objednávky. Tím strategie nevyužije prostředky nad aktuální celou tisícovku.

`VERTEX_AI_PROJECT_ID`, `VERTEX_AI_REGION` a `VERTEX_AI_MODEL` určují, do jakého Google Cloud projektu a regionu se bude volat. Výchozí model chain je postaven tak, aby při výpadku nebo nedostupnosti konkrétního flash modelu uměl přejít na rozumný fallback ve stejné třídě modelů, ale tento chain se používá jen tehdy, když není aktivní legacy API-key fallback.

Gemini requesty teď běží primárně přes Python SDK `google-genai` v režimu `vertexai=True`, používají stabilní API `v1`, mají vypnuté thinking, vynucený structured JSON response schema a logují token usage, response id, finish reason a fallback model.

Pokud běžíte v režimu pouze s Vertex konfigurací a Python SDK selže na transportní chybě bez HTTP statusu, typicky na `httpx.ReadTimeout` při čtení odpovědi z Vertex AI, helper automaticky zkusí stejný `generateContent` request přes přímý Vertex REST call s bearer tokenem získaným ze stejného service-account JSON. V logu se takový úspěšný bypass objeví jako `transport="vertex-rest-fallback"`.

Pokud jsou v `.env` nastavené `GEMINI_API_KEY` a `GEMINI_URL`, chování je nově agresivnější: Vertex se zkusí pouze jednou jako primární volba a při první chybě se request okamžitě přepne na starší Gemini API flow přes Generative Language endpoint. V tomto režimu se už nespouští další Vertex retry, další Vertex fallback modely ani `vertex-rest-fallback`. V logu se přepnutí zapisuje jako `transport="legacy-gemini-api"`.

Pokud `GEMINI_API_KEY` nebo `GEMINI_URL` nejsou nastavené, helper zůstává čistě ve Vertex režimu a použije původní Vertex retry/fallback logiku.

Pro tento fallback je teď `google-auth` explicitní součástí `requirements.txt`; bot se už nespoléhá na to, že bude auth vrstva k dispozici jen nepřímo přes jinou knihovnu.

`OLLAMA_FALLBACK_TO_GEMINI=false` znamená, že hlavní trading logika bere pro analýzu pouze instrumenty s čerstvou Ollama predikcí. Instrument bez použitelné Ollama predikce se v daném běhu přeskočí a když takto odpadnou všechny symboly, finální decision fáze se nespustí.

`OLLAMA_FALLBACK_TO_GEMINI=true` znamená, že hlavní trading logika může použít Gemini jako náhradní zdroj predikce, ale pouze do limitu `OLLAMA_GEMINI_FALLBACK_MAX_INSTRUMENTS` instrumentů za jeden cyklus. Další symboly bez čerstvé Ollama predikce se už v tom běhu přeskočí.

`ECONOMY_MODE_ENABLED=true` zapíná úsporný režim. V tomto režimu se nespouští průběžná Ollama předanalýza, ale bot si pouze v pozadí stahuje čerstvá tržní data z MT5 a při obchodním triggeru jde rovnou přes Gemini. Výchozí stav je zapnuto.

`ECONOMY_MODE_INTERVAL_SECONDS` určuje, jak často se v úsporném režimu obnovují tržní data na pozadí. Výchozí hodnota `300` znamená refresh každých 5 minut.

`GEMINI_FALLBACK_MAX_PARALLEL_REQUESTS` omezuje, kolik Gemini fallback dotazů může běžet současně. Vyšší hodnota může zrychlit cyklus, ale zvyšuje riziko quota limitů nebo špičkové latence.

`OLLAMA_MAX_PARALLEL_REQUESTS` omezuje, kolik Ollama requestů může běžet současně během nezávislého Ollama cyklu. `OLLAMA_REQUEST_DELAY_SECONDS` pak umožňuje jemně rozložit start jednotlivých requestů, pokud by při plně paralelním startu docházelo ke špičkám nebo nestabilitě.

`OLLAMA_NUM_CTX` nastavuje velikost context window posílanou v každém requestu do Ollamy přes `options.num_ctx`. Hodnota `16384` je rozumnější provozní kompromis pro CPU běh, když chcete držet latenci pod kontrolou a přitom posílat víc dat než do výchozích 4096 tokenů. Vyšší hodnota obvykle znamená vyšší spotřebu RAM/VRAM a delší inferenci.

`OLLAMA_TIMEOUT_SECONDS` určuje, jak dlouho Python klient čeká na odpověď z Ollamy. Po navýšení `OLLAMA_NUM_CTX` a při běhu na CPU je často potřeba jít výrazně nad původní 3 minuty; `600` sekund je bezpečnější výchozí hodnota.

`OLLAMA_COMPACT_PROMPT` řídí velikost promptu pro Ollamu. Při `true` se použije kompaktní shrnutí vždy, což je vhodnější volba pro CPU běh nebo při problémech s latencí. Při `false` se služba pokusí použít plný prompt a porovná jeho velikost s odhadovaným rozpočtem odvozeným z `OLLAMA_NUM_CTX`; pokud by byl stále příliš velký, automaticky spadne zpět do kompaktního režimu.

Rucni blokovaci okno `SWAP_BLOCK_START_*` az `SWAP_BLOCK_END_*` je interpretovano v case `Europe/Prague`. Audit a trade logy zustavaji ulozene v UTC.

**LOOKBACK_PERIODS** - Počet posledních period, které se mají stahovat pro každý timeframe. Výchozí 30.

## Spusteni

**Příprava (pokud chcete používat průběžnou Ollama předanalýzu):**
```bash
# Ujistěte se, že Ollama server běží
ollama serve

# Stáhněte model deepseek-coder-v2
ollama pull deepseek-coder-v2
```

**Spuštění aplikace:**
```bash
python logika.py
```

Pro rychlé ověření výpočtu profit cleanup bez MT5 můžete zadat hodnoty balance/volume/profit/swap/skutečné commission+fee:

```bash
python verify_profit_cleanup_strategy.py
```

Automatické testy výpočtu spustíte takto:

```bash
python -m unittest test_profit_cleanup_strategy.py

Pokud ponecháte `ECONOMY_MODE_ENABLED=true`, Ollama není pro běžný provoz potřeba. Pokud chcete obnovit původní režim s průběžným lokálním předpočítáváním predikcí, nastavte `ECONOMY_MODE_ENABLED=false`.
```

Skript běží jako **nekonečný obchodní automat**:
1. Spustí **Ollama Service** v samostatném threadu
2. Spustí monitoring volné marže (hlavní logika)
3. Když marže > 20%:
   - Zkontroluje existující predikce z aktuální hodiny
  - Používá je, nebo stáhne nová data a získá nové predikce
  - Pro každý symbol preferuje čerstvou Ollama predikci (<= `OLLAMA_PREDICTION_MAX_AGE_MINUTES`)
  - Když čerstvá Ollama predikce chybí, buď volá Gemini (`OLLAMA_FALLBACK_TO_GEMINI=true`) do limitu `OLLAMA_GEMINI_FALLBACK_MAX_INSTRUMENTS`, nebo symbol přeskočí
  - Gemini fallback větev může posílat více dotazů paralelně, ale maximálně `GEMINI_FALLBACK_MAX_PARALLEL_REQUESTS` současně
   - Filtruje slabé signály
   - Provede obchod
4. **Restart cyklu** (vrací se na krok 2)
5. **Ukončení:** Ctrl+C (zastaví obě smyčky korektně)

**Poznámka:** Ollama Service běží paralelně celou dobu a generuje vlastní predikce nezávisle. **Stahuje data přímo z MT5** (vlastní připojení) - není závislý na hlavní logice.

## Smoke test Gemini 4 Argon (PowerShell)

Tento test ověřuje skutečné API volání a validaci JSON odpovědi Argonu, nikoli kvalitu obchodní predikce. Používá syntetická data, neotevírá obchod, nespouští trading smyčku a neukládá obchodní predikci. API požadavek může být zpoplatněný.

Spusť celý následující blok v PowerShellu **ze složky `bots\analysis\ollama`**, kde jsou `logika.py` a `.env`. Nevkládej před něj další `Push-Location`, pokud už v této složce jsi. Python launcher `py -3.14` musí používat interpreter s nainstalovanými závislostmi z `requirements.txt`; dostupné interpretery zjistíš pomocí `py --list-paths`.

```powershell
@'
import os
from pathlib import Path
from logika import _load_dotenv
from gemini_config import load_gemini_api_config
from gemini_vertex import request_prediction_json

_load_dotenv(Path(".env"))
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("GEMINI_URL", None)

config = load_gemini_api_config()
assert config.model == "gemini-4-argon", config.model
assert config.region == "eu", config.region

prompt = """
Connectivity test with synthetic data.
Return only JSON with symbol, BUY, SELL, HOLD, reasoning.
symbol must be TEST_SYMBOL.
BUY=40, SELL=20, HOLD=40.
reasoning must be a short connectivity-test explanation.
"""

print(request_prediction_json(config, "TEST_SYMBOL", prompt))
print("SMOKE TEST OK: Argon EU returned a validated response.")
'@ | py -3.14 -X utf8 -

if ($LASTEXITCODE -ne 0) {
    throw "Smoke test failed. See the error above."
}
```

Pokud používáš jiný interpreter, nahraď `py -3.14` jeho příkazem nebo absolutní cestou, například `& 'C:\path\to\python.exe'`. Ukončovací `'@` musí začínat na začátku řádku. Pokud po neúplném vložení zůstane terminál na promptu `>>`, stiskni Ctrl+C a vlož blok znovu celý.

### Vyhodnocení

- Úspěch vyžaduje závěrečné `SMOKE TEST OK`, validovaný JSON s `symbol`, `BUY`, `SELL`, `HOLD`, `reasoning` a log `event="success"`, `model="gemini-4-argon"`, `region="eu"`.
- `transport="google-genai-sdk"` znamená úspěch SDK cesty. `transport="vertex-rest-fallback"` znamená úspěch záložního Vertex REST požadavku na stejný model; předchozí SDK chyba je vidět v logu.
- Test záměrně vypne API-key fallback pouze ve svém procesu, i kdyby byly klíč a URL zděděné z prostředí. Nepřejde na Gemini 2.5 Flash a nemění `.env` ani prostředí běžící trading aplikace.
- `ModuleNotFoundError` znamená chybějící závislost ve zvoleném interpreteru. Chyby autentizace, oprávnění, modelu, lokace nebo kvóty se projeví chybovým výpisem a nenulovým návratovým kódem.
- Loader `.env` nepřepisuje již nastavené proměnné procesu. Pokud selže kontrola modelu nebo regionu, zkontroluj také `$env:VERTEX_AI_MODEL` a `$env:VERTEX_AI_REGION`.

Test byl živě ověřen 1. 11. 2026: `google-genai-sdk`, `gemini-4-argon`, `eu`, MEDIUM thinking, `fallback_model=false`, ukončení `STOP` a validní JSON. To potvrzuje přístup k modelu a funkčnost této cesty v okamžiku testu, nikoli budoucí dostupnost služby nebo výkonnost strategie.

## Shadow benchmark Ollama Cloud: GPT versus Qwen

Ze složky `bots\analysis\ollama` lze spustit samostatné porovnání per-symbol predikcí:

```powershell
py -3.14 -X utf8 benchmark_ollama.py --source '..\..\..\analysis\python\ollama\source' --limit 10
```

Pokud zdrojová data leží jinde, zadej skutečnou složku market-data JSON souborů obsahujících `candles` a `oscillators`, nikoli složku výsledných predikcí. Test používá klíč a cloudový endpoint z `.env`; klíč nevkládej do příkazu ani nesdílej. Ověř, že je dovoleno odesílat vybraná data poskytovateli.

- Výchozí modely jsou `gpt-oss:120b` a `qwen3.8:27b`. Dostupnost tohoto Qwen tagu na účtu není předem garantovaná; při HTTP 401/403/404/429 test zastaví další požadavky a uloží chybu do `responses.jsonl`.
- První kontrolu dostupnosti proveď s `--limit 1` (dva API požadavky). Výchozí `--limit 10` znamená 20 požadavků; `--repeats 3` znamená 60. Test spotřebuje API allowance.
- Oba modely dostávají totožný zmrazený kompaktní produkční predikční prompt. Fundamental context je explicitně `NO_DATA`, aby živý kalendář nevnesl rozdíly nebo budoucí informace. API parametry odpovídají predikční větvi: temperature 0.7, top_p 0.9, cloud num_ctx a výchozí thinking modelu.
- Test nevolá MT5 exekuci, nemění produkční model ani predikce. Vlastní vstupy, prompty, raw odpovědi, SHA-256 promptu, latency a chyby ukládá do nové složky `benchmarks\<UTC timestamp>`. Tyto soubory mohou obsahovat citlivá vstupní data; nesdílej je ani necommituj bez kontroly.
- `summary.json` porovnává validitu odpovědí, počty BUY/SELL/HOLD/TIE a shodu dominantního směru. Nejde o skutečné runtime NO_TRADE: lokální signal/risk filtry nejsou součástí testu. Při chybě končí nenulovým návratovým kódem.
- Jde pouze o test použitelnosti predikcí, nikoli advisory rankingu, P/L, profit factoru nebo drawdownu. Opakování umožní následnou kontrolu stability v uložených odpovědích. Ziskovost vyžaduje samostatný časově korektní backtest se simulací nákladů a výstupů.

## Automatické spuštění

Protože skript běží jako nekonečný automat, stačí ho spustit **jednou** při startu systému:

### Linux (systemd service)
Vytvoř service file `/etc/systemd/system/mt5-trading.service`:

```ini
[Unit]
Description=MT5 Trading Automat
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/bots/analysis/ollama
ExecStart=/usr/bin/python3 logika.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Spuštění:
```bash
sudo systemctl enable mt5-trading
sudo systemctl start mt5-trading
```

### Windows (Task Scheduler)
Vytvoř task, který spustí `python logika.py` při startu systému.

## Moduly Systému

- **logika.py** - Hlavní orchestrace nekonečného cyklu (monitoring → predikce → finální rozhodnutí → obchod → restart)
- **account_monitor.py** - Monitoruje volnou marži a signalizuje překročení 20% prahu (single-line output)
- **profit_cleanup_strategy.py** - Minutové a večerní uzavírání pozic podle čistého zisku po skutečné commission a fee z MT5
- **profit_protection_strategy.py** - Ochrana zisku a uzavření spravovaných pozic po výchozích 5 dnech, pokud `profit + swap + skutečně zaúčtované náklady` dosáhne alespoň `0.50 USD`
- **verify_profit_cleanup_strategy.py** - Lokální validační skript pro čistý zisk po zadaných skutečných nákladech a dynamický limit `PCZ`
- **trading_logic.py** - Stahuje data z MT5, preferuje čerstvé Ollama predikce, fallbackuje na Gemini a filtruje slabé signály
- **final_decision.py** - Kombinuje predikce se stavem účtu, dělá finální rozhodnutí a provádí obchod
- **ollama_service.py** - Paralelní služba generující predikce pomocí lokálního Ollama AI (běží v samostatném threadu)
- **market_data.py** - Sdílené utility pro sběr dat z MT5 (RSI, MA, svíčky) - používají logika.py i ollama_service.py

## Výstupní Soubory

- `<SERVICE_DEST_FOLDER>/<timestamp>/predikce/*.json` - Filtrované predikce
- `<SERVICE_DEST_FOLDER>/geminipredictions/PREDIKCE_<timestamp>.json` - Finální rozhodnutí
- `<SERVICE_DEST_FOLDER>/ollama/predikce/{symbol}.json` - Předchystané Ollama predikce (použitelné v hlavní logice při stáří <= 1h)
- `<SERVICE_DEST_FOLDER>/trade_logs/profit_cleanup.csv` - Audit minutového i večerního profit cleanupu včetně `B`, referenčního `VOLUME`, `ZISK`, použitého cíle a výsledku close pokusu
- Dokud testujete, nechte `PROFIT_CLEANUP_STRATEGY_DRY_RUN=true`; po ověření změňte na `false`

## Poznamky

- MetaTrader 5 terminal musí běžet lokálně ve stejném uživatelském kontextu
- Pokud některý symbol selže, skript pokračuje na další symbol
- Monitorování probíhá v **background threadu**, nezablokuje tedy ostatní procesy
- **Ollama Service**: Stahuje data **přímo z MT5** (vlastní připojení), není závislý na hlavní logice
- Optimalizace: Pokud je k dispozici čerstvá Ollama predikce symbolu (max 1h), použije se místo volání Gemini
- Hardening: Kontrola "už zpracováno v této hodině" porovnává UTC datum+hodinu (YYYYMMDDHH), ne pouze hodinu
- Hardening: Pokud existuje více predikčních složek v aktuální hodině, systém vybere nejnovější timestamp
- Hardening: Pokud filtr smaže všechny existující predikce, obchodní krok se bezpečně přeskočí
- Hardening: Reuse Ollama predikce ověřuje konzistenci symbolu, aby nedošlo ke křížení párů
- Finální rozhodnutí se dělá na **právě jednom měnovém páru** s vypočtenou velikostí lotu
- Velikost lotu a take profit určuje lokální risk engine, nikoli pořadí obchodu nebo AI
- **Nekonečný loop:** Skript běží dokola, dokud není ručně zastaven
- Ukončení skriptu: `Ctrl+C`
