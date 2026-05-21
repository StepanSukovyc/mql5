"""Trading logic with Gemini AI predictions.

This module processes market data files and requests predictions from Gemini AI.
After getting predictions, it organizes files into timestamped folders.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from account_state import get_account_balance_cap
from gemini_config import load_gemini_api_config
from gemini_decision import clean_gemini_response
from gemini_vertex import request_prediction_json
from instrument_utils import get_symbol_prompt_guidance
from market_regime import MarketRegimeContext, classify_market_regime
from news_filter import should_block_symbol_for_news
from strategy_profile import StrategyProfile, get_primary_strategy_profile, is_strategy_session_open


def _get_or_create_current_hour_run_folder(source_folder: Path) -> tuple[str, Path]:
	"""Reuse the latest run folder from the current UTC hour or create a new one."""
	now_utc = datetime.now(tz=timezone.utc)
	hour_pattern = now_utc.strftime("%Y%m%d_%H")
	existing_runs = [
		folder
		for folder in source_folder.iterdir()
		if folder.is_dir() and folder.name.startswith(hour_pattern) and len(folder.name) == 15 and folder.name[8] == "_"
	]
	if existing_runs:
		latest_run = max(existing_runs, key=lambda folder: folder.name)
		return latest_run.name, latest_run

	timestamp = now_utc.strftime("%Y%m%d_%H%M%S")
	return timestamp, source_folder / timestamp


def ask_gemini_prediction(
	symbol: str,
	data: Dict,
	gemini_config,
	*,
	market_context: Optional[MarketRegimeContext] = None,
	strategy_profile: Optional[StrategyProfile] = None,
) -> Optional[str]:
	"""
	Ask Gemini on Vertex AI for trading prediction based on market data.
	
	Args:
		symbol: Trading symbol (e.g., EURUSD_ecn or XAUUSD)
		data: Market data including candles, oscillators (RSI, MA)
		gemini_config: Vertex AI Gemini runtime configuration
	
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
	news_filter_note = ""
	news_filter = data.get("news_filter")
	if isinstance(news_filter, dict):
		news_filter_note = (
			"\nNews filter context:\n"
			+ json.dumps(news_filter, indent=2)
			+ "\nPokud je blocked=true, preferuj HOLD a nezakládej nový obchod."
		)
	market_context_note = ""
	if market_context is not None:
		market_context_note = (
			"\nProgramovy market context a vstupni guardraily:\n"
			+ json.dumps(market_context.to_dict(), indent=2)
			+ "\nPokud entry_allowed=false, preferuj HOLD. Pokud buy_setup=true a sell_setup=false, "
			+ "favorizuj BUY. Pokud sell_setup=true a buy_setup=false, favorizuj SELL. "
			+ "Nevstupuj proti zadanemu rezimu trhu."
		)

	strategy_note = ""
	if strategy_profile is not None:
		strategy_note = (
			f"\nProfil strategie: {strategy_profile.label}. "
			f"Max spread pro novy vstup: {strategy_profile.max_spread_points:.2f} bodu."
		)
	
	prompt = f"""Jsi finanční poradce a expert na technickou analýzu finančních instrumentů.

Posílám ti kompletní data pro instrument: {symbol}

Aktuální cena: {current_price}

Dostupné timeframes a data:
{json.dumps(candles_summary, indent=2)}

Aktuální technické indikátory:
{json.dumps(oscillators_summary, indent=2)}

{news_filter_note}

{market_context_note}

{strategy_note}

Kompletní data včetně svíčkových formací, RSI a MA hodnot:
{json.dumps(data, indent=2)}

Úkol:
Na základě fundamentální analýzy, svíčkových formací, RSI, MA a širšího kontextu proveď rizikové hodnocení procentuálně (kde 100% = jistota, 0% = riziko) pro:
- BUY
- SELL  
- HOLD

{prompt_guidance}

Součet musí dát 100%.
Vrať pouze strukturovaný JSON objekt dle předepsaného schématu. Bez markdownu, bez code fence, bez doprovodného textu.
"""
	
	try:
		print(f"  📡 Dotazuji Gemini pro {symbol}...")
		text_response = request_prediction_json(gemini_config, symbol, prompt)
		if text_response:
			print(f"  ✅ Predikce získána pro {symbol}")
			return text_response
		else:
			print(f"  ⚠️  Prázdná odpověď pro {symbol}")
			return None
			
	except Exception as exc:
		print(f"  ❌ Chyba při dotazu na Gemini pro {symbol}: {exc}")
		return None


def _serialize_prediction_payload(
	*,
	symbol: str,
	prediction_payload: Dict[str, object],
	market_context: MarketRegimeContext,
	strategy_profile: StrategyProfile,
) -> str:
	"""Return normalized prediction JSON with embedded market context."""
	payload = dict(prediction_payload)
	payload["symbol"] = symbol
	payload["market_context"] = market_context.to_dict()
	payload["strategy_id"] = strategy_profile.strategy_id
	payload["strategy_label"] = strategy_profile.label
	return json.dumps(payload, ensure_ascii=False, indent=2)


def _prediction_text_to_payload(prediction_text: str) -> Optional[Dict[str, object]]:
	try:
		return json.loads(clean_gemini_response(prediction_text))
	except (TypeError, ValueError, json.JSONDecodeError):
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


def is_economy_mode_enabled() -> bool:
	"""Return whether economy mode disables prepared Ollama predictions."""
	configured_value = _load_dotenv_value("ECONOMY_MODE_ENABLED")
	if configured_value is None:
		return True
	return configured_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def is_gemini_fallback_after_stale_ollama_enabled() -> bool:
	"""Return whether stale/missing Ollama predictions may fall back to Gemini."""
	configured_value = _load_dotenv_value("OLLAMA_FALLBACK_TO_GEMINI")
	if configured_value is None:
		return False
	return configured_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_ollama_gemini_fallback_limit() -> int:
	"""Return maximum number of symbols per cycle that may fall back to Gemini."""
	default_limit = 60
	configured_value = _load_dotenv_value("OLLAMA_GEMINI_FALLBACK_MAX_INSTRUMENTS")
	if configured_value is None:
		return default_limit

	try:
		limit = int(configured_value)
		if limit < 0:
			raise ValueError
		return limit
	except ValueError:
		print(
			f"⚠️  Nevalidni OLLAMA_GEMINI_FALLBACK_MAX_INSTRUMENTS='{configured_value}', "
			f"pouzivam {default_limit}"
		)
		return default_limit


def get_gemini_fallback_parallelism() -> int:
	"""Return maximum number of concurrent Gemini fallback requests."""
	default_parallelism = 3
	configured_value = _load_dotenv_value("GEMINI_FALLBACK_MAX_PARALLEL_REQUESTS")
	if configured_value is None:
		return default_parallelism

	try:
		parallelism = int(configured_value)
		if parallelism <= 0:
			raise ValueError
		return parallelism
	except ValueError:
		print(
			f"⚠️  Nevalidni GEMINI_FALLBACK_MAX_PARALLEL_REQUESTS='{configured_value}', "
			f"pouzivam {default_parallelism}"
		)
		return default_parallelism


def _request_gemini_prediction_with_retries(
	symbol: str,
	market_data: Dict,
	gemini_config,
	*,
	market_context: Optional[MarketRegimeContext] = None,
	strategy_profile: Optional[StrategyProfile] = None,
) -> Optional[str]:
	"""Request Gemini prediction with the existing retry policy."""
	max_retries = 2
	prediction_text = None

	for attempt in range(max_retries):
		prediction_text = ask_gemini_prediction(
			symbol,
			market_data,
			gemini_config,
			market_context=market_context,
			strategy_profile=strategy_profile,
		)
		if prediction_text:
			return prediction_text

		if attempt < max_retries - 1:
			print(f"  🔄 Retry {attempt + 1}/{max_retries - 1} for {symbol}...")
		else:
			print(f"  ❌ Max retries reached for {symbol}, skipping")

	return None


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


def run_trading_logic(
	source_folder: Path,
	*,
	strategy_profile: Optional[StrategyProfile] = None,
) -> tuple[bool, Optional[Path]]:
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
	profile = strategy_profile or get_primary_strategy_profile()
	print(f"🎯 Strategy profile: {profile.label} [{profile.strategy_id}]")
	if not is_strategy_session_open(profile):
		print("⏸️  Strategy session is closed for this profile, skipping prediction generation")
		return False, None
	
	# Load Gemini configuration
	try:
		gemini_config = load_gemini_api_config()
		
		print(f"✅ Gemini config loaded")
		print(f"   Project: {gemini_config.project}")
		print(f"   Region: {gemini_config.region}")
		print(f"   Model chain: {', '.join(gemini_config.fallback_models)}")
		print(f"🛡️  Strategy balance cap: {get_account_balance_cap():.2f}")
	except Exception as exc:
		print(f"❌ Failed to load Gemini config: {exc}")
		return False, None
	
	# Reuse the current-hour run folder to avoid creating a new timestamp folder on every retry.
	timestamp, run_folder = _get_or_create_current_hour_run_folder(source_folder)

	# Create output directories
	source_archive_folder = run_folder / "source"
	predictions_folder = run_folder / "predikce"
	
	source_archive_folder.mkdir(parents=True, exist_ok=True)
	predictions_folder.mkdir(parents=True, exist_ok=True)
	
	print(f"📁 Output folders ready:")
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
	ignored_count = 0
	economy_mode_enabled = is_economy_mode_enabled()
	gemini_fallback_enabled = economy_mode_enabled or is_gemini_fallback_after_stale_ollama_enabled()
	gemini_fallback_limit = get_ollama_gemini_fallback_limit()
	gemini_parallelism = get_gemini_fallback_parallelism()
	gemini_fallback_used = 0
	gemini_fallback_tasks: List[tuple[str, Path, Dict]] = []

	if economy_mode_enabled:
		print("🪫 Economy mode active - skipping prepared Ollama predictions and using Gemini at trade time")
	elif ollama_predictions_folder.exists():
		print(f"🤝 Ollama fallback aktivní: {ollama_predictions_folder}")
	else:
		print(f"ℹ️  Ollama predikce složka neexistuje, používám jen Gemini")

	print(
		"🔀 Režim fallbacku po staré/chybějící Ollama predikci: "
		+ ("Gemini povoleno" if gemini_fallback_enabled else "Gemini zakázáno, beru jen čerstvé Ollama predikce")
	)
	if gemini_fallback_enabled:
		print(f"🔢 Limit fallbacku do Gemini za cyklus: {gemini_fallback_limit}")
		print(f"🧵 Paralelni Gemini fallback dotazy: max {gemini_parallelism}")
	
	for json_file in json_files:
		symbol = json_file.stem  # filename without .json
		if not profile.allows_symbol(symbol):
			continue
		
		# Skip if already processed in this cycle
		if symbol in processed_symbols:
			print(f"\n⏭️  Skipping {symbol} (already processed in this cycle)")
			continue
		
		try:
			print(f"\n📈 Processing {symbol}...")

			with open(json_file, "r", encoding="utf-8") as f:
				market_data = json.load(f)

			news_decision = should_block_symbol_for_news(symbol)
			market_data["news_filter"] = news_decision.to_dict()
			if news_decision.blocked:
				print(
					f"  ⏭️  Ignoruji {symbol}: news filter blokuje vstup "
					f"({news_decision.reason})"
				)
				archive_file = source_archive_folder / json_file.name
				shutil.move(str(json_file), str(archive_file))
				print(f"  📦 Source moved to archive: {archive_file.name}")
				processed_symbols.add(symbol)
				ignored_count += 1
				continue

			market_context = classify_market_regime(market_data, max_spread_points=profile.max_spread_points)
			market_data["market_context"] = market_context.to_dict()
			if not market_context.entry_allowed:
				print(
					f"  ⏭️  Ignoruji {symbol}: market filter blokuje vstup "
					f"({market_context.regime}, {market_context.reason})"
				)
				archive_file = source_archive_folder / json_file.name
				shutil.move(str(json_file), str(archive_file))
				print(f"  📦 Source moved to archive: {archive_file.name}")
				processed_symbols.add(symbol)
				ignored_count += 1
				continue

			ollama_reason = "economy mode aktivni - Ollama reuse vypnut"
			if not economy_mode_enabled:
				# Prefer prepared Ollama prediction when not older than configured limit.
				ollama_prediction_max_age = get_ollama_prediction_max_age()
				ollama_prediction_max_age_minutes = int(ollama_prediction_max_age.total_seconds() // 60)
				recent_ollama, ollama_reason = load_recent_ollama_prediction_with_reason(
					ollama_predictions_folder,
					symbol,
					max_age=ollama_prediction_max_age,
				)
				if recent_ollama is not None:
					prediction_text = _serialize_prediction_payload(
						symbol=symbol,
						prediction_payload=recent_ollama,
						market_context=market_context,
						strategy_profile=profile,
					)
					prediction_file = predictions_folder / f"{symbol}.json"
					prediction_file.write_text(prediction_text, encoding="utf-8")
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

			if not gemini_fallback_enabled:
				print(f"  ⏭️  Ignoruji {symbol}: {ollama_reason}")
				archive_file = source_archive_folder / json_file.name
				shutil.move(str(json_file), str(archive_file))
				print(f"  📦 Source moved to archive: {archive_file.name}")
				processed_symbols.add(symbol)
				ignored_count += 1
				continue

			if gemini_fallback_used >= gemini_fallback_limit:
				print(
					f"  ⏭️  Ignoruji {symbol}: dosažen limit Gemini fallbacku "
					f"{gemini_fallback_used}/{gemini_fallback_limit}"
				)
				archive_file = source_archive_folder / json_file.name
				shutil.move(str(json_file), str(archive_file))
				print(f"  📦 Source moved to archive: {archive_file.name}")
				processed_symbols.add(symbol)
				ignored_count += 1
				continue

			print(f"  🤖 Fallback na Gemini pro {symbol}: {ollama_reason}")
			gemini_fallback_used += 1
			print(f"  🔢 Gemini fallback zařazen {gemini_fallback_used}/{gemini_fallback_limit}")

			gemini_fallback_tasks.append((symbol, json_file, market_data, market_context))
				
		except Exception as exc:
			print(f"  ❌ Error processing {symbol}: {exc}")
			error_count += 1

	if gemini_fallback_tasks:
		print(f"\n🤖 Spoustim paralelni Gemini fallback pro {len(gemini_fallback_tasks)} instrumentu...")
		with ThreadPoolExecutor(max_workers=gemini_parallelism) as executor:
			future_to_task = {
				executor.submit(
					_request_gemini_prediction_with_retries,
					symbol,
					market_data,
					gemini_config,
					market_context=market_context,
					strategy_profile=profile,
				): (symbol, json_file)
				for symbol, json_file, market_data, market_context in gemini_fallback_tasks
			}

			for future in as_completed(future_to_task):
				symbol, json_file = future_to_task[future]
				try:
					prediction_text = future.result()
				except Exception as exc:
					print(f"  ❌ Gemini fallback worker failed for {symbol}: {exc}")
					error_count += 1
					continue

				if prediction_text:
					payload = _prediction_text_to_payload(prediction_text)
					if payload is None:
						print(f"  ⚠️  Prediction for {symbol} was not valid JSON after cleanup")
						error_count += 1
						continue

					market_context = next(
						context
						for task_symbol, _task_file, _task_data, context in gemini_fallback_tasks
						if task_symbol == symbol
					)
					prediction_text = _serialize_prediction_payload(
						symbol=symbol,
						prediction_payload=payload,
						market_context=market_context,
						strategy_profile=profile,
					)
					prediction_file = predictions_folder / f"{symbol}.json"
					prediction_file.write_text(prediction_text, encoding="utf-8")
					print(f"  💾 Prediction saved: {prediction_file.name}")
					print(f"  🤖 Zdroj predikce: Gemini")

					archive_file = source_archive_folder / json_file.name
					shutil.move(str(json_file), str(archive_file))
					print(f"  📦 Source moved to archive: {archive_file.name}")

					processed_symbols.add(symbol)
					success_count += 1
					gemini_count += 1
				else:
					print(f"  ⚠️  No prediction received for {symbol} after retries, skipping")
					error_count += 1
	
	print("\n" + "="*60)
	print(f"✅ Trading Logic Completed")
	print(f"   Success: {success_count}")
	print(f"   Reused from Ollama (<=1h): {ollama_reuse_count}")
	print(f"   Generated by Gemini: {gemini_count}")
	print(f"   Gemini fallback used: {gemini_fallback_used}/{gemini_fallback_limit}")
	print(f"   Ignored without usable fallback: {ignored_count}")
	print(f"   Errors: {error_count}")
	print(f"   Total processed: {len(processed_symbols)}")
	print(f"📁 Results saved in: {run_folder}")
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
