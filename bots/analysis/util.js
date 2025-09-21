const fs = require('fs');
const path = require('path');

function ensurePredictJson() {
    const predictDestFolder = process.env.PREDICT_DEST_FOLDER;
    const mqlSourceFolder = process.env.MQL_SOURCE_FOLDER;

    if (!predictDestFolder || !mqlSourceFolder) {
        console.error("Chybí definice proměnných prostředí.");
        return;
    }

    const cPredictPath = path.join(predictDestFolder, 'cPredict.json');
    const aPredictPath = path.join(predictDestFolder, 'aPredict.json');
    const predictPath = path.join(predictDestFolder, 'predict.json');
    const mqlPredictPath = path.join(mqlSourceFolder, 'predict.json');

    // Vytvoření prázdného JSON objektu
    let predictJson = {};
    if (fs.existsSync(aPredictPath)) {
        if (!fs.existsSync(cPredictPath)) {
            fs.copyFileSync(aPredictPath, cPredictPath);
        }
        if (fs.existsSync(cPredictPath)) {
            const data = JSON.parse(fs.readFileSync(cPredictPath, 'utf-8'));

            if (!Array.isArray(data) || data.length === 0) {
                console.error("Soubor cPredict.json neobsahuje platné pole objektů.");
            } else {

                const first = data[0];
                const typ = first.BUY > first.SELL ? 'BUY' : 'SELL';

                predictJson = {
                    symbol: first.symbol,
                    typ: typ
                };

                console.log("Nový objekt:", predictJson);

                // Odstranění prvního objektu z pole
                const updatedData = data.slice(1);

                // Uložení zpět do cPredict.json
                fs.writeFileSync(cPredictPath, JSON.stringify(updatedData, null, 2));
                console.log("Aktualizovaný soubor cPredict.json bez prvního objektu uložen.");

                // // Pokud chceš nový objekt uložit do souboru, můžeš přidat:
                // fs.writeFileSync(path.join(predictDestFolder, 'prediction.json'), JSON.stringify(newObject, null, 2));
            }
        }
    }

    // Uložení do složky PREDICT_DEST_FOLDER
    fs.writeFileSync(predictPath, JSON.stringify(predictJson, null, 2));
    console.log("Vytvořen soubor predict.json ve složce PREDICT_DEST_FOLDER.");

    // Kopírování do složky MQL_SOURCE_FOLDER
    fs.copyFileSync(predictPath, mqlPredictPath);
    console.log("Soubor predict.json zkopírován do složky MQL_SOURCE_FOLDER.");
}


module.exports = {
    ensurePredictJson
};
