require('dotenv').config();
const fs = require('fs/promises');
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

async function processFilesWithGemini(folder) {
    const targetFolder = path.join(folder, "processed");
    await fs.mkdir(targetFolder, { recursive: true });

    const files = await fs.readdir(folder, { withFileTypes: true })
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
                const outputPath = path.join(targetFolder, `o${fileName}`);
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
}

async function mainCycle() {
    try {
        const folder = await copyFiles();
        await processFilesWithGemini(folder);
    } catch (err) {
        console.error('Chyba v cyklu:', err);
    }

    console.log('Čekám 1 minutu na další cyklus...');
    setTimeout(mainCycle, 60 * 1000);
}

mainCycle();
