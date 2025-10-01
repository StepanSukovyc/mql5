require('dotenv').config();

const axios = require('axios');
const fs = require('fs/promises');
const path = require('path');

const url = process.env.GEMINI_URL;
const API_KEY = process.env.GEMINI_API_KEY;

async function proceedResponse(proceedFolder, prefix, fileName, prompt) {
    const requestData = {
        contents: [
            {
                parts: [
                    { text: prompt }
                ]
            }
        ]
    };

    try {
        const response = await axios.post(url, requestData, {
            headers: {
                'Content-Type': 'application/json',
                'X-goog-api-key': API_KEY
            }
        });

        const textResponse = response.data?.candidates?.[0]?.content?.parts?.[0]?.text || '';
        const outputPath = path.join(proceedFolder, `${prefix}${fileName}`);

        if (textResponse) {
            await fs.writeFile(outputPath, textResponse);
            console.log(`✅ Výstup uložen: ${outputPath}`);
        } else {
            console.warn(`⚠️ JSON nebyl nalezen v odpovědi pro: ${fileName}`);
        }

        await new Promise(resolve => setTimeout(resolve, 5000)); // zpomalení kvůli limitům

    } catch (err) {
        if (err.response?.status === 429) {
            const retry = 36000;
            console.warn(`⚠️ Quota překročena, čekám ${retry / 1000} sekund...`);
            await new Promise(resolve => setTimeout(resolve, retry));
        } else {
            console.error(`❌ Chyba při zpracování souboru ${fileName}:`, err.response?.data || err.message);
        }
    }
}

async function processDaysWithGemini(folder, proceedFolder) {
    const files = await fs.readdir(folder, { withFileTypes: true });

    if (files.length === 0) {
        console.log(`Složka "${folder}" je prázdná. Přeskakuji.`);
        return false;
    }

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

        // můžeš prompt doplnit o obsah souboru, pokud chceš:
        const fullPrompt = `jsi finanční poradce.
posílám historická data za měsíc měnových párů.
jak bys na základě fundamentální analýzy dostupné na webu, svíčkových formaci a daných dat procentuálně (kde 100% - jistota, 0 - rtiziko) provedl rizikové hodnocení
- BUY
- SELL
- HOLD
aby dohromady dály 100%
data jsou\n\`${content}\`\nodpověď prosím pošli stručně v JSON formátu, kde klíčem je měnový pár a tělem je hodnocení.
Ohodnoť všechny poslané páry`;

        await proceedResponse(proceedFolder, 'o', fileName, fullPrompt);
    }

    return true;
}

async function process4HWithGemini(folder, proceedFolder) {
    const inputPath = path.join(proceedFolder, 'aDaysResult.json');

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

                // Zavolej funkci proceedResponse (axios verze)
                await proceedResponse(proceedFolder, 'o4H-', fileName, prompt);

            } catch (err) {
                console.warn(`⚠️ Soubor ${fileName} nelze načíst:`, err.message);
            }
        }
    } catch (err) {
        console.error('❌ Soubor aDaysResult.json neexistuje nebo nelze načíst:', err.message);
        return false;
    }

    return true;
}

async function testGemini() {
    const requestData = {
        contents: [
            {
                parts: [
                    {
                        text: "Explain how AI works in a few words"
                    }
                ]
            }
        ]
    };
    try {
        const response = await axios.post(url, requestData, {
            headers: {
                'Content-Type': 'application/json',
                'X-goog-api-key': process.env.GEMINI_API_KEY
            }
        });

        console.log("Odpověď Gemini:");
        console.log(response.data);
    } catch (error) {
        console.error("Chyba při volání Gemini:");
        console.error(error.response?.data || error.message);
    }
}

module.exports = {
    processDaysWithGemini, process4HWithGemini, testGemini
}