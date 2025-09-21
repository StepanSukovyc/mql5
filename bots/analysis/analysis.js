require('dotenv').config();

const fs = require('fs/promises');
const fse = require('fs-extra');
const path = require('path');

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

/**
 * Výsledkem metody je seřazení jednotlivých výsledků do jednoho souboru. 
 * Řazení probíhá na základě nejvyšší hodnoty `SELL` nebo `BUY`.
 * Výsledný soubor je názvu `aDaysResult.json`.
 * Následně stejný soubor je uložen do složky `predict` jako `aPredict.json`. 
 * @param {*} targetFolder Složka obsahující soubory s již analyzovanými daty GEMINI\
 * , které začínají na `o` a mají příponu `json`
 * @returns 
 */
async function analyzeDaysData(targetFolder) {
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

    // Uložit do aDaysResult.json
    const outputPath = path.join(targetFolder, 'aDaysResult.json');
    await fs.writeFile(outputPath, JSON.stringify(array, null, 2));

    const predictFolder = process.env.PREDICT_DEST_FOLDER;
    await fs.mkdir(predictFolder, { recursive: true });

    // Vymazání obsahu složky
    await emptyFolder(predictFolder);

    const aPredict = path.join(predictFolder, 'aPredict.json');
    await fse.copy(outputPath, aPredict);
    console.log(`Sloučený výstup uložen do: ${outputPath}`);
}

async function analyze4HData(targetFolder) {
    const files = await fs.readdir(targetFolder, { withFileTypes: true });

    if (files.length === 0) {
        console.log(`4H Složka "${targetFolder}" je prázdná. Přeskakuji.`);
        return;
    }

    const merged = {};

    for (const file of files) {
        if (!file.isFile() || !file.name.endsWith('.json') || !file.name.startsWith('o4H')) {
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
            console.warn(`4H Soubor ${file.name} není validní JSON:`, err.message);
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

    // Uložit do aDaysResult.json
    const outputPath = path.join(targetFolder, 'a4HResult.json');
    await fs.writeFile(outputPath, JSON.stringify(array, null, 2));

    const predictFolder = process.env.PREDICT_DEST_FOLDER;
    await fs.mkdir(predictFolder, { recursive: true });

    // TODO Až bude nějaký obsah tak provedeme porovnání...
    // prozatím necháme jak je pro další analýzu...

    // // Vymazání obsahu složky
    // await emptyFolder(predictFolder);

    // const aPredict = path.join(predictFolder, 'aPredict.json');
    // await fse.copy(outputPath, aPredict);
    // console.log(`Sloučený výstup uložen do: ${outputPath}`);    
}


module.exports = {
    analyzeDaysData,
    analyze4HData
}