require('dotenv').config();
const { ensurePredictJson } = require('./util.js');
const fs = require('fs/promises');
const fse = require('fs-extra');
const path = require('path');
const { GoogleGenerativeAI } = require('@google/generative-ai');

const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

async function copyFiles() {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const targetFolder = path.join(process.env.SERVICE_DEST_FOLDER, timestamp);
    await fs.mkdir(targetFolder, { recursive: true });

    const sourceFolder = process.env.MQL_SOURCE_FOLDER;
    const files = await fs.readdir(sourceFolder);
    const matchingFiles = files.filter(f => f.startsWith('tHistory') && f.endsWith('.json'));

    for (const file of matchingFiles) {
        const src = path.join(sourceFolder, file);
        const dest = path.join(targetFolder, file);
        await fs.copyFile(src, dest);
        await fs.unlink(src);
        console.log(`Zkopírován a odstraněn: ${file}`);
    }

    return targetFolder;
}

async function processFilesWithGemini(folder, proceedFolder) {
    const files = await fs.readdir(folder, { withFileTypes: true });

    if (files.length === 0) {
        console.log(`Složka "${folder}" je prázdná. Přeskakuji.`);
        return false;
    }

    const model = genAI.getGenerativeModel({ model: 'gemini-1.5-pro' });

    for (const file of files) {

        if (!file.isFile()) {
            continue;
        }

        const fileName = file.name;
        if (!fileName.endsWith('.json')) {
            continue;
        }

        const filePath = path.join(folder, fileName);
        const content = await fs.readFile(filePath, 'utf-8');
        const prompt = `jsi finanční poradce.
posílám historická data za měsíc měnových párů.
jak bys na základě fundamentální analýzy dostupné na webu, svíčkových formaci a daných dat procentuálně (kde 100% - jistota, 0 - rtiziko) provedl rizikové hodnocení
- BUY
- SELL
- HOLD
aby dohromady dály 100%
data jsou\n\`${content}\`\nodpověď prosím pošli stručně v JSON formátu, kde klíčem je měnový pár a tělem je hodnocení.
Ohodnoť všechny poslané páry`;

        try {
            const result = await model.generateContent(prompt);
            const response = await result.response.text();

            //const jsonMatch = response.match(/\{[\s\S]*?\}/);
            const jsonMatch = response;
            if (jsonMatch) {
                const outputPath = path.join(proceedFolder, `o${fileName}`);
                await fs.writeFile(outputPath, jsonMatch);
                console.log(`Výstup uložen: ${outputPath}`);
            } else {
                console.warn(`JSON nebyl nalezen v odpovědi pro: ${fileName}`);
            }

            await new Promise(resolve => setTimeout(resolve, 5000)); // zpomalení kvůli limitům

        } catch (err) {
            if (err.status === 429) {
                const retry = 36000; // default 36s
                console.warn(`Quota překročena, čekám ${retry / 1000} sekund...`);
                await new Promise(resolve => setTimeout(resolve, retry));
            } else {
                console.error(`Chyba při zpracování souboru ${fileName}:`, err);
            }
        }
    }
    return true;
}

async function emptyFolder(folderPath) {
    try {
        const files = await fs.readdir(folderPath);
        for (const file of files) {
            const filePath = path.join(folderPath, file);
            await fs.rm(filePath, { recursive: true, force: true });
        }
        console.log(`Složka ${folderPath} byla vyprázdněna.`);
    } catch (err) {
        console.error("Chyba při mazání obsahu složky:", err);
    }
}

function extractJsonFromText(text) {
    const start = text.indexOf('{');
    const end = text.lastIndexOf('}');

    if (start === -1 || end === -1 || end <= start) {
        throw new Error('JSON objekt nebyl nalezen.');
    }

    const jsonString = text.substring(start, end + 1);

    try {
        return JSON.parse(jsonString);
    } catch (err) {
        throw new Error('Chybný formát JSON: ' + err.message);
    }
}

async function mergeAndAnalyzeJson(targetFolder) {
    const files = await fs.readdir(targetFolder, { withFileTypes: true });

    if (files.length === 0) {
        console.log(`Složka "${targetFolder}" je prázdná. Přeskakuji.`);
        return;
    }

    const merged = {};

    for (const file of files) {
        if (!file.isFile() || !file.name.endsWith('.json') || !file.name.startsWith('o')) {
            continue;
        }
        const filePath = path.join(targetFolder, file.name);
        const content = await fs.readFile(filePath, 'utf-8');

        try {
            //const json = JSON.parse(content);
            const json = extractJsonFromText(content);
            for (const [symbol, data] of Object.entries(json)) {
                merged[symbol] = data;
            }
        } catch (err) {
            console.warn(`Soubor ${file.name} není validní JSON:`, err.message);
        }
    }

    // Vytvořit pole z objektu
    const array = Object.entries(merged).map(([symbol, data]) => ({
        symbol,
        ...data
    }));

    // Seřadit podle největší hodnoty BUY nebo SELL
    array.sort((a, b) => {
        const maxA = Math.max(a.BUY ?? 0, a.SELL ?? 0);
        const maxB = Math.max(b.BUY ?? 0, b.SELL ?? 0);
        return maxB - maxA;
    });

    // Uložit do aResult.json
    const outputPath = path.join(targetFolder, 'aResult.json');
    await fs.writeFile(outputPath, JSON.stringify(array, null, 2));

    const predictFolder = process.env.PREDICT_DEST_FOLDER;
    await fs.mkdir(predictFolder, { recursive: true });

    // Vymazání obsahu složky
    await emptyFolder(predictFolder);

    const aPredict = path.join(predictFolder, 'aPredict.json');
    await fse.copy(outputPath, aPredict);
    console.log(`Sloučený výstup uložen do: ${outputPath}`);
}

async function checkAnalyzeJsonExists(filePath) {
    try {
        await fs.access(filePath);
        return 1;
    } catch (err) {
        return 0;
    }
}

async function mainCycle() {
    const sourceFolder = process.env.MQL_SOURCE_FOLDER;
    const filePath = path.join(sourceFolder, 'analyze.json');

    const acc = await checkAnalyzeJsonExists(filePath);
    // const acc = 1; // for test
    if (acc === 1) {
        try {
            const folder = await copyFiles();

            const targetFolder = path.join(folder, "processed");
            await fs.mkdir(targetFolder, { recursive: true });

            const isExists = await processFilesWithGemini(folder, targetFolder);
            // const targetFolder = "C:\\Users\\Stepa\\GitHub\\mql5\\analysis\\2025-09-16T18-57-30-452Z\\processed";
            if (isExists) {
                await mergeAndAnalyzeJson(targetFolder);
            }
            ensurePredictJson();
            // odstranění analyze.json
            await fs.unlink(filePath);
            console.log(`Soubor "${filePath}" byl odstraněn po zpracování.`);
        } catch (err) {
            console.error('Chyba v cyklu:', err);
        }
    }
    else {
        console.log(`Soubor "${filePath}" neexistuje. Čekám na další cyklus.`);
    }

    console.log('Čekám 1 minutu na další cyklus...');
    setTimeout(mainCycle, 60 * 1000);
}

mainCycle();
