# Chyngelog

## 2026-03-03

### Oprava mapování výstupu predikce (`util.js`)

- Přidána normalizace výstupu z Gemini, aby se neprohazovaly hodnoty `symbol` a `typ`.
- Opraven scénář, kdy model vrátí například `symbol: "SELL"` a `typ: 70`.
- Nová logika umí převést různé tvary odpovědi na konzistentní objekt:
  - `{ symbol: "PAIR", typ: "BUY|SELL|HOLD" }`
  - `{ "PAIR": { ... } }`
- Pokud jsou dostupné jen skóre (`BUY`, `SELL`, `HOLD`), `typ` se odvodí z nejvyšší hodnoty.
- Opravena kontrola prázdného objektu:
  - původně: `predictJson.length === 0`
  - nově: `Object.keys(predictJson).length === 0`

### Přidané helpery v `util.js`

- `normalizeTyp(value)`
- `inferTypFromScores(obj)`
- `normalizePredictChoice(rawPredict)`

### Integrace do toku `ensurePredictJson`

- Nahrazeno původní ruční čtení `firstKey` za volání `normalizePredictChoice(...)`.
- Pokud normalizace selže, použije se fallback na predikci z `cPredict.json`.

### Kontrola po úpravě

- Ověřeno načtení modulu příkazem:
  - `node -e "require('./util.js'); console.log('util.js OK')"`
- Výsledek: `util.js OK`

### Oprava práce se vstupními soubory (`start.js`)

- Upravena funkce `copyFiles()`, aby vstupní soubory pouze kopírovala.
- Odstraněno mazání zdrojových souborů po kopii (`fs.unlink(src)`).
- Soubory `tHistory_*.json` a `4H-*.json` nyní zůstávají v `MQL5\Files` pro další cykly.
- Upraven log z `Zkopírován a odstraněn` na `Zkopírován`.

### Kontrola po úpravě

- Ověřen syntax check příkazem:
- `node --check start.js`
- Výsledek: bez chyby.
