"""Gemini-specific helpers for loading predictions and requesting a final trade decision."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx


_gemini_suspended_until: Optional[datetime] = None


def clean_gemini_response(text: str) -> str:
	"""Clean Gemini response text by removing markdown code fences."""
	text = text.strip()

	if text.startswith("```json"):
		text = text[7:]
	elif text.startswith("```"):
		text = text[3:]

	if text.endswith("```"):
		text = text[:-3]

	return text.strip()


def load_predictions(predictions_folder: Path) -> List[Dict]:
	"""Load remaining prediction files and keep only strong BUY/SELL candidates."""
	predictions = []

	for pred_file in predictions_folder.glob("*.json"):
		try:
			with open(pred_file, "r", encoding="utf-8") as f:
				content = f.read()

			prediction = json.loads(clean_gemini_response(content))
			buy_pct = prediction.get("BUY", 0)
			sell_pct = prediction.get("SELL", 0)

			if buy_pct >= 35 or sell_pct >= 35:
				predictions.append(prediction)
		except Exception as exc:
			print(f"  ⚠️  Error loading {pred_file.name}: {exc}")

	return predictions


def ask_gemini_final_decision(
	predictions: List[Dict],
	open_positions: List[Dict],
	account_state: Dict,
	api_key: str,
	api_url: str,
	excluded_symbols: List[str] = None,
	trade_number: Optional[int] = None,
	full_control_every_n: Optional[int] = None,
	gemini_full_control_mode: bool = False,
) -> Optional[str]:
	"""Ask Gemini AI for a final trading decision based on predictions and account state."""
	global _gemini_suspended_until

	if _gemini_suspended_until is not None:
		now = datetime.now(tz=timezone.utc)
		if now < _gemini_suspended_until:
			remaining = (_gemini_suspended_until - now).total_seconds() / 3600
			print(f"  ⏸️  Gemini suspended until {_gemini_suspended_until.isoformat()}")
			print(f"     Remaining: {remaining:.1f} hours")
			return None

		print("  ✅ Gemini suspension lifted")
		_gemini_suspended_until = None

	if excluded_symbols:
		filtered_predictions = [p for p in predictions if p.get("symbol") not in excluded_symbols]
		print(f"  🔍 Excluded {len(excluded_symbols)} symbols: {excluded_symbols}")
		print(f"     Remaining predictions: {len(filtered_predictions)}")

		if not filtered_predictions:
			print("  ⚠️  No predictions left after exclusions")
			return None

		predictions = filtered_predictions

	excluded_note = ""
	if excluded_symbols:
		excluded_note = f"\n\nVYLOUČENÉ SYMBOLY (nevybírej): {', '.join(excluded_symbols)}"

	mode_text = ""
	if trade_number is not None and full_control_every_n is not None:
		mode_label = "ANO" if gemini_full_control_mode else "NE"
		mode_text = (
			f"\n\nREŽIM AKTUÁLNÍHO OBCHODU:\n"
			f"- Pořadí obchodu: #{trade_number}\n"
			f"- Každý {full_control_every_n}. obchod je plně řízen Gemini (lot + take_profit): {mode_label}\n"
			f"- lot_size se ve finální exekuci vždy použije z této odpovědi.\n"
			f"- U ne-plně řízených obchodů se take_profit ve finální exekuci ignoruje."
		)

	prompt = f"""Jsi expert obchodní poradce. Musíš na základě analýzy učinit finální obchodní rozhodnutí.

DOSTUPNÉ INFORMACE:

1. Aktuální stav účtu:
{json.dumps(account_state, indent=2)}

2. Aktuálně otevřené pozice:
{json.dumps(open_positions, indent=2)}

3. Dostupné obchodní predikce (filtrované - pouze ty s BUY/SELL >= 35%):
{json.dumps(predictions, indent=2)}{excluded_note}{mode_text}

ÚKOL:
Na základě všech dostupných informací (predikce, otevřené pozice, stav účtu):

1. Vyber PRÁVĚ JEDEN měnový pár z dostupných predikcí
2. Rozhodni se pro BUY nebo SELL
3. Doporuč velikost lotu (berouc v úvahu aktuální marži a risk management)
4. Navrhni take_profit cenu pro swing obchod (pozice může být otevřená několik dní)
5. Zdůvodni rozhodnutí
6. DIVERZIFIKACE: Preferuj symboly, které ještě nemáš v otevřených pozicích. Pokud již existují otevřené pozice, posuzuj tu s open_price nejblíže aktuální tržní ceně a novou pozici na stejném symbolu otevři POUZE tehdy, když tato nejbližší pozice prodělává více než 15 % aktuální hodnoty účtu; jinak POVINNĚ vyber raději jiný kandidát z dostupných predikcí pro bezpečnou diverzifikaci portfolia.

DŮLEŽITÉ OBCHODNÍ NASTAVENÍ:
- Nejsem intradenní obchodník. Pozice držím často více dní (swing styl).
- Chci ale průběžně generovat zisky na denní bázi.
- Zohledni transakční náklad: za každých 0.01 lot je poplatek 0.10 USD.
- take_profit nastav realisticky tak, aby po odečtení poplatků dával obchod ekonomický smysl.

Odpověď prosím formátuj POUZE jako JSON bez dalšího textu, v tomto formátu:

{{
  "recommended_symbol": "EURUSD_ecn",
  "action": "BUY",
  "lot_size": 0.5,
	"take_profit": 1.105,
  "reasoning": "..."
}}

Kde lot_size je doporučená velikost pozice, take_profit je cílová cena TP a reasoning obsahuje stručné vysvětlení"""

	request_data = {"contents": [{"parts": [{"text": prompt}]}]}

	try:
		print("  📡 Dotazuji Gemini na finální rozhodnutí...")

		with httpx.Client(timeout=60.0) as client:
			response = client.post(
				api_url,
				json=request_data,
				headers={
					"Content-Type": "application/json",
					"X-goog-api-key": api_key,
				},
			)

		if response.status_code == 429:
			now = datetime.now(tz=timezone.utc)
			tomorrow = now + timedelta(days=1)
			midnight_tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
			_gemini_suspended_until = midnight_tomorrow

			hours_until = (midnight_tomorrow - now).total_seconds() / 3600
			print("  🚫 Quota překročena!")
			print(f"  ⏸️  Gemini suspended until {midnight_tomorrow.isoformat()}")
			print(f"     Suspension duration: {hours_until:.1f} hours")
			return None

		response.raise_for_status()

		response_data = response.json()
		text_response = response_data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")

		if text_response:
			print("  ✅ Finální rozhodnutí získáno")
			return clean_gemini_response(text_response)

		print("  ⚠️  Prázdná odpověď od Gemini")
		return None

	except httpx.HTTPError as exc:
		print(f"  ❌ HTTP chyba při dotazu na Gemini: {exc}")
		return None
	except Exception as exc:
		print(f"  ❌ Chyba při dotazu na Gemini: {exc}")
		return None