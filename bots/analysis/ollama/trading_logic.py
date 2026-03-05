"""Trading logic with Gemini AI predictions.

This module processes market data files and requests predictions from Gemini AI.
After getting predictions, it organizes files into timestamped folders.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import httpx


def _clean_gemini_response(text: str) -> str:
	"""
	Clean Gemini response by removing markdown code blocks.
	
	Args:
		text: Raw response from Gemini (may contain ```json ... ```)
	
	Returns:
		Clean JSON string
	"""
	text = text.strip()
	
	# Remove markdown code blocks
	if text.startswith("```json"):
		text = text[7:]  # Remove ```json
	elif text.startswith("```"):
		text = text[3:]  # Remove ```
	
	if text.endswith("```"):
		text = text[:-3]  # Remove trailing ```
	
	return text.strip()


def _load_gemini_config() -> Dict[str, str]:
	"""Load Gemini configuration from environment variables."""
	api_key = os.getenv("GEMINI_API_KEY")
	api_url = os.getenv("GEMINI_URL")
	
	if not api_key:
		raise ValueError("GEMINI_API_KEY not found in .env")
	if not api_url:
		raise ValueError("GEMINI_URL not found in .env")
	
	return {
		"GEMINI_API_KEY": api_key,
		"GEMINI_URL": api_url
	}


def ask_gemini_prediction(symbol: str, data: Dict, api_key: str, api_url: str) -> Optional[str]:
	"""
	Ask Gemini AI for trading prediction based on market data.
	
	Args:
		symbol: Trading symbol (e.g., EURUSD_ecn)
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
	
	prompt = f"""Jsi finanční poradce a expert na technickou analýzu forex trhů.

Posílám ti kompletní data pro měnový pár: {symbol}

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
			cleaned_response = _clean_gemini_response(text_response)
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
			cleaned_content = _clean_gemini_response(content)
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
		gemini_config = _load_gemini_config()
		api_key = gemini_config.get("GEMINI_API_KEY")
		api_url = gemini_config.get("GEMINI_URL")
		
		if not api_key or not api_url:
			raise ValueError("GEMINI_API_KEY or GEMINI_URL not found in gemini/.env")
		
		print(f"✅ Gemini config loaded")
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
	
	if not json_files:
		print(f"⚠️  No JSON files found in {source_folder}")
		return False, None
	
	print(f"\n📊 Found {len(json_files)} market data files to process")
	
	# Track processed files to avoid duplicates (in case files are created during processing)
	processed_symbols: Set[str] = set()
	failed_symbols: Dict[str, int] = {}  # Track retry attempts
	
	success_count = 0
	error_count = 0
	
	for json_file in json_files:
		symbol = json_file.stem  # filename without .json
		
		# Skip if already processed in this cycle
		if symbol in processed_symbols:
			print(f"\n⏭️  Skipping {symbol} (already processed in this cycle)")
			continue
		
		try:
			print(f"\n📈 Processing {symbol}...")
			
			# Load market data
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
				
				# Move source file to archive
				archive_file = source_archive_folder / json_file.name
				shutil.move(str(json_file), str(archive_file))
				print(f"  📦 Source moved to archive: {archive_file.name}")
				
				# Mark as processed
				processed_symbols.add(symbol)
				success_count += 1
			else:
				print(f"  ⚠️  No prediction received for {symbol} after retries, skipping")
				error_count += 1
				
		except Exception as exc:
			print(f"  ❌ Error processing {symbol}: {exc}")
			error_count += 1
	
	print("\n" + "="*60)
	print(f"✅ Trading Logic Completed")
	print(f"   Success: {success_count}")
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
