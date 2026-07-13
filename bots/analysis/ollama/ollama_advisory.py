"""Ollama-based advisory for final trade decision (cloud Ollama variant).

This module provides ask_ollama_final_decision() as a drop-in replacement for
ask_gemini_final_decision() used by the cloud Ollama strategy. The response
format is identical so _extract_ranked_candidates_from_decision_payload() works
without modifications.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

import httpx

from instrument_utils import is_crypto_symbol
from ollama_service import (
    _extract_json_from_text,
    get_ollama_cloud_api_key,
    get_ollama_cloud_model,
    get_ollama_cloud_num_ctx,
    get_ollama_cloud_timeout_seconds,
    get_ollama_cloud_url,
)


def ask_ollama_final_decision(
    predictions: List[Dict],
    open_positions: List[Dict],
    account_state: Dict,
) -> Optional[str]:
    """Ask cloud Ollama for a final trading decision.

    Returns a JSON string in the same format as ask_gemini_final_decision()
    (keys: recommended_symbol, action, reasoning, candidates), or None on failure.
    """
    if not predictions:
        return None

    crypto_symbols = [
        str(p.get("symbol"))
        for p in predictions
        if is_crypto_symbol(str(p.get("symbol", "")))
    ]
    crypto_note = ""
    if crypto_symbols:
        crypto_note = (
            "\n\nPRAVIDLA PRO CRYPTO INSTRUMENTY:\n"
            f"- Crypto kandidati v tomto vyberu: {', '.join(crypto_symbols)}\n"
            "- Crypto je povoleno, ale pouze pri vyznamne presvedcivem signalu.\n"
            "- U crypto preferuj mensi lot a konzervativnejsi risk management."
        )

    response_example = json.dumps(
        {
            "recommended_symbol": "SYMBOL_NAME",
            "action": "BUY",
            "reasoning": "Silny trend a cisty BUY bias.",
            "candidates": [
                {
                    "symbol": "SYMBOL_NAME",
                    "action": "BUY",
                    "reasoning": "Nejsilnejsi kandidat.",
                },
                {
                    "symbol": "ALTERNATIVE_SYMBOL",
                    "action": "SELL",
                    "reasoning": "Alternativa pro fallback.",
                },
            ],
        },
        indent=2,
        ensure_ascii=False,
    )

    prompt = f"""Jsi expert obchodni poradce. Na zaklade analyzy ucin finalni obchodni rozhodnutí.

STAV UCTU:
{json.dumps(account_state, indent=2, ensure_ascii=False)}

OTEVRENE POZICE:
{json.dumps(open_positions, indent=2, ensure_ascii=False)}

DOSTUPNE PREDIKCE (filtrovane - pouze BUY/SELL >= 35%):
{json.dumps(predictions, indent=2, ensure_ascii=False)}{crypto_note}

UKOL:
1. Vyber hlavniho kandidata z dostupnych predikcí.
2. Rozhodni se pro BUY nebo SELL.
3. Strucne zduvodni rozhodnutí (max 2 vety, do 180 znaku).
4. Pokud davaji smysl alternativy, vrat i 2 az 3 serazene kandidaty pro fallback.
5. DIVERZIFIKACE: Preferuj symboly bez otevrenych pozic.
6. Nerikis lot_size ani take_profit - pouze instrument a smer.

Odpovez POUZE jako JSON bez dalsiho textu:

{response_example}"""

    ollama_url = get_ollama_cloud_url()
    ollama_model = get_ollama_cloud_model()
    api_key = get_ollama_cloud_api_key()
    num_ctx = get_ollama_cloud_num_ctx()
    timeout = get_ollama_cloud_timeout_seconds()

    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request_data = {
        "model": ollama_model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": num_ctx,
            "temperature": 0.3,
            "top_p": 0.9,
        },
    }

    try:
        print("  📡 Dotazuji cloud Ollamu na finalni rozhodnutí...")
        with httpx.Client(timeout=timeout) as client:
            response = client.post(ollama_url, json=request_data, headers=headers)
            response.raise_for_status()

        result = response.json()
        text = result.get("response", "")
        if not text:
            print("  ⚠️  Prazdna odpoved od cloud Ollamy")
            return None

        parsed = _extract_json_from_text(text)
        if not parsed:
            print("  ⚠️  Nelze parsovat JSON z odpovedi cloud Ollamy")
            return None

        if not parsed.get("recommended_symbol") or not parsed.get("action"):
            print("  ⚠️  Cloud Ollama neposkytla povinne pole recommended_symbol/action")
            return None

        print("  ✅ Finalni rozhodnutí od cloud Ollamy ziskano")
        return json.dumps(parsed, ensure_ascii=False)

    except httpx.ConnectError:
        print(f"  ❌ Nelze se pripojit ke cloud Ollama API na {ollama_url}")
        return None
    except httpx.TimeoutException:
        print("  ⏱️  Timeout pri cekani na cloud Ollama advisory")
        return None
    except Exception as exc:
        print(f"  ❌ Chyba pri volani cloud Ollama advisory: {exc}")
        return None
