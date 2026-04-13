"""Trading logic with Gemini AI predictions.

This module processes market data files and requests predictions from Gemini AI.
After getting predictions, it organizes files into timestamped folders.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import httpx

from account_state import get_account_balance_cap
from gemini_config import load_gemini_api_config
from gemini_decision import clean_gemini_response
from instrument_utils import get_symbol_prompt_guidance


def ask_gemini_prediction(symbol: str, data: Dict, api_key: str, api_url: str) -> Optional[str]:
	"""
	Ask Gemini AI for trading prediction based on market data.
	
	Args:
		symbol: Trading symbol (e.g., EURUSD_ecn or XAUUSD)
		data: Market data including candles, oscillators (RSI, MA)
		api_key: Gemini API key
		api_url: Gemini API URL
	
	Returns:
		Gemini's prediction text or None if failed
	"""
	# Build comprehensive prompt with RSI and MA context
	candles_summary = {}
	oscillators_summary = {}
	
	for timeframe in ["1h", "4h", "day", "week", "month"]:
		if timeframe in data.get("candles", {}):
			candles_count = len(data["candles"][timeframe])
			candles_summary[timeframe] = f"{candles_count} candles"
		
		if timeframe in data.get("oscillators", {}):
			oscs = data["oscillators"][timeframe]
			rsi_values = oscs.get("rsi", [])
			ma_values = oscs.get("ma", [])
			
			latest_rsi = rsi_values[-1]["value"] if rsi_values else None
			latest_ma = ma_values[-1]["value"] if ma_values else None
			
			oscillators_summary[timeframe] = {
				"rsi": latest_rsi,
				"ma": latest_ma
			}
	
	current_price = data.get("current_price")
	prompt_guidance = get_symbol_prompt_guidance(symbol)
	
	prompt = f"""Jsi finanční poradce a expert na technickou analýzu finančních instrumentů.

Posílám ti kompletní data pro instrument: {symbol}

Aktuální cena: {current_price}

Dostupné timeframes a data:
{json.dumps(candles_summary, indent=2)}

Aktuální technické indikátory:
{json.dumps(oscillators_summary, indent=2)}

Kompletní data včetně svíčkových formací, RSI a MA hodnot:
{json.dumps(data, indent=2)}

Úkol:
Na základě fundamentální analýzy, svíčkových formací, RSI, MA a širšího kontextu proveď rizikové hodnocení procentuálně (kde 100% = jistota, 0% = riziko) pro:
- BUY
- SELL  
- HOLD

{prompt_guidance}

Součet musí dát 100%.

Odpověď pošli POUZE v JSON formátu:
{{
  "symbol": "{symbol}",
  "BUY": <procenta>,
  "SELL": <procenta>,
  "HOLD": <procenta>,
  "reasoning": "<krátké zdůvodnění>"
}}
"""
	
	request_data = {
		"contents": [
			{
				"parts": [
					{"text": prompt}
				]
			}
		]
	}
	
	try:
		print(f"  📡 Dotazuji Gemini pro {symbol}...")
		
		with httpx.Client(timeout=60.0) as client:
			response = client.post(
				api_url,
				json=request_data,
				headers={
					"Content-Type": "application/json",
					"X-goog-api-key": api_key
				}
			)
		
		if response.status_code == 429:
			print(f"  ⚠️  Quota překročena pro {symbol}, čekám 60 sekund...")
			time.sleep(60)
			return None
		
		response.raise_for_status()
		
		response_data = response.json()
		text_response = response_data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
		
		if text_response:
			# Clean markdown formatting from response
			cleaned_response = clean_gemini_response(text_response)
			print(f"  ✅ Predikce získána pro {symbol}")
			# Delay to respect API limits
			time.sleep(5)
			return cleaned_response
		else:
			print(f"  ⚠️  Prázdná odpověď pro {symbol}")
			return None
			
	except Exception as exc:
		print(f"  ❌ Chyba při dotazu na Gemini pro {symbol}: {exc}")
		return None


def _parse_iso_datetime(value: str) -> Optional[datetime]:
	"""Parse ISO timestamp safely and normalize to timezone-aware UTC datetime."""
	try:
		parsed = datetime.fromisoformat(value)
		if parsed.tzinfo is None:
			return parsed.replace(tzinfo=timezone.utc)
		return parsed.astimezone(timezone.utc)
	except (TypeError, ValueError):
		return None


def _load_dotenv_value(key: str) -> Optional[str]:
	"""Load a specific value from .env files to allow runtime config changes."""
	base_dir = Path(__file__).resolve().parent
	env_paths = (
		base_dir / ".env",
		base_dir.parent / ".env",
		Path.cwd() / ".env",
	)

	for env_path in env_paths:
		if not env_path.exists():
			continue

		for raw_line in env_path.read_text(encoding="utf-8").splitlines():
			line = raw_line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			loaded_key, value = line.split("=", 1)
			loaded_key = loaded_key.strip()
			value = value.strip().strip('"').strip("'")
			if loaded_key == key:
				return value

	return None


def get_ollama_prediction_max_age() -> timedelta:
	"""Read Ollama prediction max age from .env on demand."""
	default_minutes = 120
	configured_value = _load_dotenv_value("OLLAMA_PREDICTION_MAX_AGE_MINUTES")
	if configured_value is None:
		return timedelta(minutes=default_minutes)

	try:
		minutes = int(configured_value)
		if minutes <= 0:
			raise ValueError
		return timedelta(minutes=minutes)
	except ValueError:
		print(
			f"⚠️  Nevalidni OLLAMA_PREDICTION_MAX_AGE_MINUTES='{configured_value}', "
			f"pouzivam {default_minutes} min"
		)
		return timedelta(minutes=default_minutes)


def load_recent_ollama_prediction(
	ollama_predictions_folder: Path,
	symbol: str,
	max_age: Optional[timedelta] = None,
) -> Optional[Dict[str, object]]:
	"""Load Ollama prediction for symbol when timestamp is not older than max_age."""
	if max_age is None:
		max_age = get_ollama_prediction_max_age()

	prediction, _ = load_recent_ollama_prediction_with_reason(
		ollama_predictions_folder=ollama_predictions_folder,
		symbol=symbol,
		max_age=max_age,
	)
	return prediction


def load_recent_ollama_prediction_with_reason(
	ollama_predictions_folder: Path,
	symbol: str,
	max_age: Optional[timedelta] = None,
) -> tuple[Optional[Dict[str, object]], str]:
	"""Load recent Ollama prediction and return decision reason for diagnostics."""
	if max_age is None:
		max_age = get_ollama_prediction_max_age()

	prediction_file = ollama_predictions_folder / f"{symbol}.json"
	if not prediction_file.exists():
		return None, "soubor neexistuje"

	try:
		prediction = json.loads(prediction_file.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, OSError):
		return None, "nevalidni JSON"

	timestamp_raw = prediction.get("timestamp")
	if not isinstance(timestamp_raw, str):
		return None, "chybi timestamp"

	parsed_ts = _parse_iso_datetime(timestamp_raw)
	if parsed_ts is None:
		return None, "timestamp neni validni ISO"

	now_utc = datetime.now(tz=timezone.utc)
	age = now_utc - parsed_ts
	if age < timedelta(0) or age > max_age:
		age_minutes = int(age.total_seconds() // 60)
		max_age_minutes = int(max_age.total_seconds() // 60)
		if age < timedelta(0):
			return None, f"timestamp je v budoucnosti ({age_minutes} min)"
		return None, f"timestamp je starsi nez limit {max_age_minutes} min ({age_minutes} min)"

	if not all(key in prediction for key in ("BUY", "SELL", "HOLD", "reasoning")):
		return None, "chybi BUY/SELL/HOLD/reasoning"

	payload_symbol = prediction.get("symbol")
	if isinstance(payload_symbol, str) and payload_symbol != symbol:
		return None, f"symbol mismatch ({payload_symbol} != {symbol})"

	# Ensure symbol in payload matches currently processed symbol.
	prediction["symbol"] = symbol
	return prediction, "validni Ollama predikce"


def filter_predictions(predictions_folder: Path) -> int:
	"""
	Filter out prediction files where both BUY and SELL are less than 35%.
	
	Args:
		predictions_folder: Folder with prediction JSON files
	
	Returns:
		Number of deleted files
	"""
	deleted_count = 0
	
	for pred_file in predictions_folder.glob("*.json"):
		try:
			with open(pred_file, "r", encoding="utf-8") as f:
				content = f.read()
			
			# Clean markdown formatting if present
			cleaned_content = clean_gemini_response(content)
			prediction = json.loads(cleaned_content)
			
			buy_pct = prediction.get("BUY", 0)
			sell_pct = prediction.get("SELL", 0)
			
			# Delete if both BUY and SELL are less than 35
			if buy_pct < 35 and sell_pct < 35:
				pred_file.unlink()
				print(f"  🗑️  Deleted: {pred_file.name} (BUY={buy_pct}%, SELL={sell_pct}%)")
				deleted_count += 1
		
		except Exception as exc:
			print(f"  ⚠️  Error filtering {pred_file.name}: {exc}")
	
	return deleted_count


def run_trading_logic(source_folder: Path) -> tuple[bool, Optional[Path]]:
	"""
	Main trading logic: process all market data files and get Gemini predictions.
	
	Args:
		source_folder: Folder with market data JSON files (SERVICE_DEST_FOLDER)
	
	Returns:
		Tuple of (success: bool, predictions_folder: Optional[Path])
	"""
	print("\n" + "="*60)
	print("🚀 Starting Trading Logic with Gemini AI")
	print("="*60)
	
	# Load Gemini configuration
	try:
		api_key, api_url = load_gemini_api_config()
		
		print(f"✅ Gemini config loaded")
		print(f"🛡️  Strategy balance cap: {get_account_balance_cap():.2f}")
	except Exception as exc:
		print(f"❌ Failed to load Gemini config: {exc}")
		return False, None
	
	# Create timestamp for this run
	timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
	
	# Create output directories
	source_archive_folder = source_folder / timestamp / "source"
	predictions_folder = source_folder / timestamp / "predikce"
	
	source_archive_folder.mkdir(parents=True, exist_ok=True)
	predictions_folder.mkdir(parents=True, exist_ok=True)
	
	print(f"📁 Output folders created:")
	print(f"   Source: {source_archive_folder}")
	print(f"   Predictions: {predictions_folder}")
	
	# Get all JSON files in source folder
	json_files = list(source_folder.glob("*.json"))
	ollama_predictions_folder = source_folder / "ollama" / "predikce"
	
	if not json_files:
		print(f"⚠️  No JSON files found in {source_folder}")
		return False, None
	
	print(f"\n📊 Found {len(json_files)} market data files to process")
	
	# Track processed files to avoid duplicates (in case files are created during processing)
	processed_symbols: Set[str] = set()
	
	success_count = 0
	error_count = 0
	ollama_reuse_count = 0
	gemini_count = 0

	if ollama_predictions_folder.exists():
		print(f"🤝 Ollama fallback aktivní: {ollama_predictions_folder}")
	else:
		print(f"ℹ️  Ollama predikce složka neexistuje, používám jen Gemini")
	
	for json_file in json_files:
		symbol = json_file.stem  # filename without .json
		
		# Skip if already processed in this cycle
		if symbol in processed_symbols:
			print(f"\n⏭️  Skipping {symbol} (already processed in this cycle)")
			continue
		
		try:
			print(f"\n📈 Processing {symbol}...")

			# Prefer prepared Ollama prediction when not older than configured limit.
			ollama_prediction_max_age = get_ollama_prediction_max_age()
			ollama_prediction_max_age_minutes = int(ollama_prediction_max_age.total_seconds() // 60)
			recent_ollama, ollama_reason = load_recent_ollama_prediction_with_reason(
				ollama_predictions_folder,
				symbol,
				max_age=ollama_prediction_max_age,
			)
			if recent_ollama is not None:
				prediction_file = predictions_folder / f"{symbol}.json"
				prediction_file.write_text(
					json.dumps(recent_ollama, ensure_ascii=False, indent=2),
					encoding="utf-8",
				)
				print(
					f"  ♻️  Použita Ollama predikce (<= {ollama_prediction_max_age_minutes} min): "
					f"{prediction_file.name}"
				)
				print(f"  ℹ️  Duvod: {ollama_reason}")

				archive_file = source_archive_folder / json_file.name
				shutil.move(str(json_file), str(archive_file))
				print(f"  📦 Source moved to archive: {archive_file.name}")

				processed_symbols.add(symbol)
				success_count += 1
				ollama_reuse_count += 1
				continue

			print(f"  🤖 Fallback na Gemini pro {symbol}: {ollama_reason}")

			# Load market data only when Ollama prediction is not usable.
			with open(json_file, "r", encoding="utf-8") as f:
				market_data = json.load(f)
			
			# Get prediction from Gemini (with retry logic)
			max_retries = 2
			prediction_text = None
			
			for attempt in range(max_retries):
				prediction_text = ask_gemini_prediction(symbol, market_data, api_key, api_url)
				
				if prediction_text:
					break
				else:
					if attempt < max_retries - 1:
						print(f"  🔄 Retry {attempt + 1}/{max_retries - 1} for {symbol}...")
					else:
						print(f"  ❌ Max retries reached for {symbol}, skipping")
			
			if prediction_text:
				# Save prediction
				prediction_file = predictions_folder / f"{symbol}.json"
				prediction_file.write_text(prediction_text, encoding="utf-8")
				print(f"  💾 Prediction saved: {prediction_file.name}")
				print(f"  🤖 Zdroj predikce: Gemini")
				
				# Move source file to archive
				archive_file = source_archive_folder / json_file.name
				shutil.move(str(json_file), str(archive_file))
				print(f"  📦 Source moved to archive: {archive_file.name}")
				
				# Mark as processed
				processed_symbols.add(symbol)
				success_count += 1
				gemini_count += 1
			else:
				print(f"  ⚠️  No prediction received for {symbol} after retries, skipping")
				error_count += 1
				
		except Exception as exc:
			print(f"  ❌ Error processing {symbol}: {exc}")
			error_count += 1
	
	print("\n" + "="*60)
	print(f"✅ Trading Logic Completed")
	print(f"   Success: {success_count}")
	print(f"   Reused from Ollama (<=1h): {ollama_reuse_count}")
	print(f"   Generated by Gemini: {gemini_count}")
	print(f"   Errors: {error_count}")
	print(f"   Total processed: {len(processed_symbols)}")
	print(f"📁 Results saved in: {source_folder / timestamp}")
	print("="*60)
	
	# Filter out weak predictions (both BUY and SELL < 35%)
	print(f"\n🔍 Filtering weak predictions from {predictions_folder}...")
	deleted_count = filter_predictions(predictions_folder)
	print(f"📊 Deleted {deleted_count} weak prediction(s)")
	
	return success_count > 0, predictions_folder if success_count > 0 else None


if __name__ == "__main__":
	# For testing standalone
	import sys
	
	# Load .env for standalone execution
	def _load_dotenv_standalone(dotenv_path: Path) -> None:
		if not dotenv_path.exists():
			return
		for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
			line = raw_line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			key, value = line.split("=", 1)
			key = key.strip()
			value = value.strip().strip('"').strip("'")
			if key and key not in os.environ:
				os.environ[key] = value
	
	# Load environment variables
	base_dir = Path(__file__).resolve().parent
	_load_dotenv_standalone(base_dir / ".env")
	_load_dotenv_standalone(base_dir.parent / ".env")
	_load_dotenv_standalone(Path.cwd() / ".env")
	
	if len(sys.argv) > 1:
		folder = Path(sys.argv[1])
	else:
		folder = Path(os.getenv("SERVICE_DEST_FOLDER", "../../analysis/python"))
	
	if not folder.exists():
		print(f"❌ Folder not found: {folder}")
		sys.exit(1)
	
	success = run_trading_logic(folder)
	sys.exit(0 if success else 1)
