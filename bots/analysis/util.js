// ensurePredictJson_async.js
const path = require('path');
const fs = require('fs/promises'); // asynchronní FS API
const { processTradeInfoWithGemini } = require('./gemini_v1.js');

const predictDestFolder = process.env.PREDICT_DEST_FOLDER;


/**
 * Projde text a vyextrahuje všechny validní JSON bloky.
 * - Podporuje fenced bloky: ```json ... ```, ``` ... ```
 * - Podporuje volné JSON úseky (začínající { nebo [) – balancuje závorky
 *   a ignoruje závorky uvnitř stringů.
 *
 * @param {string} input
 * @returns {Array<{raw: string, value: any, start: number, end: number, source: 'fence-json'|'fence'|'balanced'}>}
 */
function extractJsonSnippets(input) {
    const results = [];

    // 1) Nejprve zkusíme markdown fenced bloky s jazykem json: ```json ... ```
    const fenceJsonRegex = /```json\s*([\s\S]*?)\s*```/gi;
    let m;
    while ((m = fenceJsonRegex.exec(input)) !== null) {
        const raw = m[1].trim();
        try {
            const value = JSON.parse(raw);
            results.push({ raw, value, start: m.index, end: fenceJsonRegex.lastIndex, source: 'fence-json' });
        } catch {
            // necháme být – může to být pseudo-json; nenecháme to spadnout
        }
    }

    // 2) Obyčejné fenced bloky: ``` ... ```
    // Někdy lidi nepíšou ```json, jen ```
    const fenceAnyRegex = /```(?!json)([\s\S]*?)```/gi;
    while ((m = fenceAnyRegex.exec(input)) !== null) {
        const rawCand = m[1].trim();
        // Zkusíme z něj vytáhnout čistý JSON (může být s komentáři/šumem)
        const candidates = scanBalancedJson(rawCand);
        for (const c of candidates) {
            try {
                const value = JSON.parse(c.raw);
                results.push({
                    raw: c.raw,
                    value,
                    start: m.index + c.start,
                    end: m.index + c.end,
                    source: 'fence'
                });
            } catch { /* ignore */ }
        }
    }

    // 3) Mimo fenced bloky – globálně projít text a balancovat {…} a […]
    // Pozn.: dává smysl spustit jen pokud jsme nenašli nic, nebo klidně vždy (deduplikace dále)
    const globalCandidates = scanBalancedJson(input);
    for (const c of globalCandidates) {
        try {
            const value = JSON.parse(c.raw);
            results.push({ raw: c.raw, value, start: c.start, end: c.end, source: 'balanced' });
        } catch { /* ignore */ }
    }

    // 4) Odstraníme duplicity (když se překrývají stejné úseky)
    // priorita: fence-json > fence > balanced
    results.sort((a, b) => {
        const prio = (s) => s === 'fence-json' ? 0 : s === 'fence' ? 1 : 2;
        if (prio(a.source) !== prio(b.source)) return prio(a.source) - prio(b.source);
        if (a.start !== b.start) return a.start - b.start;
        return a.end - b.end;
    });

    const dedup = [];
    const seen = new Set();
    for (const r of results) {
        const key = `${r.start}:${r.end}`;
        if (!seen.has(key)) {
            seen.add(key);
            dedup.push(r);
        }
    }

    return dedup;
}

/**
 * Projde text a vrátí kandidáty na JSON bloky tak,
 * že balancuje {} a [] a ignoruje obsah ve stringu.
 *
 * @param {string} s
 * @returns {Array<{raw: string, start: number, end: number}>}
 */
function scanBalancedJson(s) {
    const res = [];
    const stack = [];
    const openers = new Set(['{', '[']);
    const closers = new Set(['}', ']']);
    const pairs = { '{': '}', '[': ']' };

    let inString = false;
    let stringQuote = null; // ' nebo "
    let escape = false;
    let startIndex = -1; // kde začal aktuální JSON blok
    let topOpener = null;

    for (let i = 0; i < s.length; i++) {
        const ch = s[i];

        if (inString) {
            if (escape) {
                escape = false;
            } else if (ch === '\\') {
                escape = true;
            } else if (ch === stringQuote) {
                inString = false;
                stringQuote = null;
            }
            continue; // všechno uvnitř stringu ignorujeme
        } else {
            // nejsme ve stringu – můžeme začínat string
            if (ch === '"' || ch === '\'') {
                inString = true;
                stringQuote = ch;
                escape = false;
                continue;
            }

            if (openers.has(ch)) {
                stack.push(ch);
                if (stack.length === 1) {
                    // začínáme nový kandidát
                    startIndex = i;
                    topOpener = ch;
                }
            } else if (closers.has(ch) && stack.length > 0) {
                const last = stack[stack.length - 1];
                if (pairs[last] === ch) {
                    stack.pop();
                    if (stack.length === 0) {
                        // máme vybalancovaný blok
                        const raw = s.slice(startIndex, i + 1);
                        res.push({ raw, start: startIndex, end: i + 1 });
                        startIndex = -1;
                        topOpener = null;
                    }
                } else {
                    // špatné párování – resetneme hledání aktuálního bloku
                    stack.length = 0;
                    startIndex = -1;
                    topOpener = null;
                }
            }
        }
    }

    return res;
}


async function fileExists(p) {
    try {
        await fs.access(p);
        return true;
    } catch {
        return false;
    }
}

async function ensurePredictJson(traderData, proceedFolder) {
    try {
        const mqlSourceFolder = process.env.MQL_SOURCE_FOLDER;

        if (!predictDestFolder || !mqlSourceFolder) {
            console.error('Chybí definice proměnných prostředí.');
            return;
        }

        const cPredictPath = path.join(predictDestFolder, 'cPredict.json');
        const aPredictPath = path.join(predictDestFolder, 'aPredict.json');
        const predictPath = path.join(predictDestFolder, 'predict.json');
        const mqlPredictPath = path.join(mqlSourceFolder, 'predict.json');

        // Výchozí prázdný výsledek
        let predictJson = {};

        // Pokud existuje aPredict.json, zkopíruj do cPredict.json (pokud cPredict.json neexistuje)
        if (await fileExists(aPredictPath)) {
            if (!(await fileExists(cPredictPath))) {
                await fs.copyFile(aPredictPath, cPredictPath);
            }

            if (await fileExists(cPredictPath)) {
                const raw = await fs.readFile(cPredictPath, 'utf-8');
                let data;
                try {
                    data = JSON.parse(raw);
                } catch (e) {
                    console.error('Soubor cPredict.json obsahuje neplatný JSON:', e.message);
                    data = [];
                }

                if (!Array.isArray(data) || data.length === 0) {
                    console.error('Soubor cPredict.json neobsahuje platné pole objektů.');
                } else {
                    if (data.length > 5) {
                        // vezmi prvních 5 a předdej Gemini
                        const firstFive = data.slice(0, 5);

                        // POZOR: processTradeInfoWithGemini musí být async, jinak zahoď "await"
                        const isExists = await processTradeInfoWithGemini(traderData, firstFive, proceedFolder);
                        if (isExists) {
                            const outputPath = path.join(proceedFolder, 'traderInfo.json');
                            // pokud soubor existuje
                            if (await fileExists(outputPath)) {
                                // načteme obsah souboru a měl by být JSON formátu
                                const traderInfoContent = await fs.readFile(outputPath, 'utf-8');
                                let traderInfo;
                                try {
                                    ///traderInfoContent.replace(/^```json\s*/, '').replace(/\s*```$/, '')
                                    const snippets = extractJsonSnippets(traderInfoContent);
                                    // vezmemem první hodnotu snippets
                                    const firstSnippet = snippets.length > 0 ? snippets[0] : null;
                                    predictJson = JSON.parse(firstSnippet.raw);

                                    // vezmeme první klíč a uvnítř objektu bude symbol
                                    const firstKey = Object.keys(predictJson)[0];
                                    // tento celý objekt můžeme poslat na SLACK ale někdy příště
                                    if (firstKey) {
                                        predictJson = {
                                            "symbol": firstKey,
                                            // vezmeme větší hodnotu mezi BUY a SELL
                                            "typ": predictJson[firstKey].BUY > predictJson[firstKey].SELL ? 'BUY' : 'SELL'
                                        }
                                        // predictJson = predictJson[firstKey];
                                    }

                                    console.log('Načtený objekt z traderInfo.json: ', predictJson);
                                } catch (e) {
                                    console.error('Soubor traderInfo.json obsahuje neplatný JSON:', e.message);
                                    traderInfo = null;
                                }
                            }
                            else
                                predictJson = {};
                        }
                        else
                            // v režimu >5 nebudu tvořit predictJson (předpokládám, že predikci dělá Gemini)
                            predictJson = {};
                    }
                    if (predictJson.length === 0 || !predictJson.symbol || !predictJson.typ) {
                        // jeden prvek → vytvoř predikci + posuň frontu
                        const first = data[0];

                        // Bezpečné přečtení BUY/SELL
                        const buy = Number(first?.BUY);
                        const sell = Number(first?.SELL);

                        // Pokud BUY/SELL nejsou číselné, nech prázdný typ
                        const typ =
                            Number.isFinite(buy) && Number.isFinite(sell)
                                ? (buy > sell ? 'BUY' : 'SELL')
                                : '';

                        predictJson = {
                            symbol: first?.symbol ?? '',
                            typ
                        };

                        console.log('Nový objekt:', predictJson);
                        // Odstranit první objekt (posun fronty)
                        const updatedData = data.slice(1);
                        await fs.writeFile(cPredictPath, JSON.stringify(updatedData, null, 2), 'utf-8');
                        console.log('Aktualizovaný soubor cPredict.json bez prvního objektu uložen.');
                    }
                }
            }
        }

        // Uložení do PREDICT_DEST_FOLDER
        await fs.writeFile(predictPath, JSON.stringify(predictJson, null, 2), 'utf-8');
        console.log('Vytvořen soubor predict.json ve složce PREDICT_DEST_FOLDER.');

        // Kopírování do MQL_SOURCE_FOLDER
        await fs.copyFile(predictPath, mqlPredictPath);
        console.log('Soubor predict.json zkopírován do složky MQL_SOURCE_FOLDER.');
    } catch (err) {
        console.error('Chyba v ensurePredictJson:', err);
    }
}

module.exports = {
    ensurePredictJson
};
