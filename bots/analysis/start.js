require('dotenv').config();

const { ensurePredictJson } = require('./util.js');
const fs = require('fs/promises');
const path = require('path');
const { processDaysWithGemini, process4HWithGemini } = require('./gemini_v1.js');
const { analyzeDaysData, analyze4HData } = require('./analysis.js');

async function copyFiles() {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const targetFolder = path.join(process.env.SERVICE_DEST_FOLDER, timestamp);
    await fs.mkdir(targetFolder, { recursive: true });

    const sourceFolder = process.env.MQL_SOURCE_FOLDER;
    const files = await fs.readdir(sourceFolder);
    const matchingFiles = files.filter(f => (f.startsWith('tHistory') || f.startsWith('4H-')) && f.endsWith('.json'));

    for (const file of matchingFiles) {
        const src = path.join(sourceFolder, file);
        const dest = path.join(targetFolder, file);
        await fs.copyFile(src, dest);
        await fs.unlink(src);
        console.log(`Zkopírován a odstraněn: ${file}`);
    }

    return targetFolder;
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
    if (acc === 1) {
        try {
            // analyze.json obshauje aktuální data obchodní platformy
            // přečteme soubor, a data pošleme k analýze
            const traderData = await fs.readFile(filePath, 'utf-8');
            console.log("Obsah analyze.json:", traderData);

            const folder = await copyFiles();
            const targetFolder = path.join(folder, "processed");
            await fs.mkdir(targetFolder, { recursive: true });

            let isExists = await processDaysWithGemini(folder, targetFolder);
            if (isExists) {
                await analyzeDaysData(targetFolder);
                let isExists = await process4HWithGemini(folder, targetFolder);
                if (isExists)
                    await analyze4HData(targetFolder);
            }
            ensurePredictJson(traderData, targetFolder);
            // odstranění analyze.json
            await fs.unlink(filePath);
            console.log(`Soubor "${filePath}" byl odstraněn po zpracování.`);
        } catch (err) {
            console.error('Chyba v cyklu:', err);
        }
    }
    else {
        console.log(`Soubor "${filePath}" neexistuje.`);
    }

    console.log('Čekám 1 minutu na další cyklus...');
    setTimeout(mainCycle, 60 * 1000);
}

mainCycle();
