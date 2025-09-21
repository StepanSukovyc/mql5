require('dotenv').config();

const fs = require('fs/promises');
const path = require('path');
const { GoogleGenerativeAI } = require('@google/generative-ai');
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

async function processDaysWithGemini(folder, proceedFolder) {
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

        await proceedResponse(proceedFolder, 'o', fileName, model, prompt)
    }
    return true;
}

async function proceedResponse(proceedFolder, prefix, fileName, model, prompt) {
    try {
        const result = await model.generateContent(prompt);
        const response = await result.response.text();

        //const jsonMatch = response.match(/\{[\s\S]*?\}/);
        const jsonMatch = response;
        if (jsonMatch) {
            const outputPath = path.join(proceedFolder, `${prefix}${fileName}`);
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

async function process4HWithGemini(folder, proceedFolder) {
    const inputPath = path.join(proceedFolder, 'aDaysResult.json');
    const model = genAI.getGenerativeModel({ model: 'gemini-1.5-pro' });

    try {
        // Zkontroluj existenci souboru
        await fs.access(inputPath);

        // Načti obsah souboru
        const data = await fs.readFile(inputPath, 'utf-8');
        const pairs = JSON.parse(data);

        // Projdi prvních 10 objektů
        for (let i = 0; i < Math.min(10, pairs.length); i++) {
            const { symbol } = pairs[i];
            const fileName = `4H-${symbol}.json`;
            const filePath = path.join(folder, fileName);

            try {
                // Zkontroluj existenci souboru
                await fs.access(filePath);

                // Načti obsah souboru
                const content = await fs.readFile(filePath, 'utf-8');

                // Vytvoř prompt
                const prompt = `jsi finanční poradce.posílám historická data periody 4H za měsíc měnového páru.
jak bys na základě fundamentální analýzy dostupné na webu, svíčkových formaci a daných dat procentuálně (kde 100% - jistota, 0 - rtiziko) provedl rizikové hodnocení
- BUY
- SELL
- HOLD
aby dohromady dály 100%
data jsou\n\`${content}\`\nodpověď prosím pošli stručně v JSON formátu, kde klíčem je měnový pár a tělem je hodnocení.`;

                // Zavolej funkci proceedResponse
                await proceedResponse(proceedFolder, 'o4H-', fileName, model, prompt);
            } catch (err) {
                // Soubor neexistuje nebo nelze načíst – přeskoč
                console.warn(`Soubor ${fileName} nelze načíst:`, err.message);
            }
        }
    } catch (err) {
        console.error('Soubor aDaysResult.json neexistuje nebo nelze načíst:', err.message);
        return false;
    }
    return true;
}

module.exports = {
    processDaysWithGemini, process4HWithGemini
}