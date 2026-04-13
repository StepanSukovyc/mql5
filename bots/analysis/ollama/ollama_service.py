"""Ollama service for independent market predictions.

This module runs in a separate thread and continuously generates predictions
using Ollama AI (deepseek-coder-v2) based on market data.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Set

import httpx
import re
import MetaTrader5 as mt5

from account_state import get_account_login
from instrument_utils import get_symbol_prompt_guidance
from mt5_connection import initialize_mt5, shutdown_mt5
from market_data import (
    collect_symbol_payload,
    get_symbols,
)


def _load_dotenv_value(key: str) -> Optional[str]:
    """
    Load a specific value from .env file.
    
    This allows reading dynamic values that can be changed during runtime.
    """
    base_dir = Path(__file__).resolve().parent
    env_path = base_dir / ".env"
    
    if not env_path.exists():
        return None
    
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k == key:
            return v
    
    return None


def is_ollama_enabled() -> bool:
    """
    Check if Ollama functionality is enabled in .env file.
    
    Reads directly from file to allow runtime changes.
    """
    value = _load_dotenv_value("OLLAMA_ENABLED")
    if value is None:
        return False
    return value.lower() in {"true", "1", "yes", "y", "on"}


def get_current_hour() -> int:
    """Get current hour in UTC."""
    return datetime.now(tz=timezone.utc).hour


def get_current_hour_key() -> str:
    """Get current UTC year-month-day-hour key for robust hourly comparison."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d%H")


def was_processed_this_hour(symbol: str, predictions_folder: Path) -> bool:
    """
    Check if prediction for symbol was already created this hour.
    
    Args:
        symbol: Trading symbol (e.g., EURUSD_ecn)
        predictions_folder: Path to ollama/predikce folder
    
    Returns:
        True if prediction exists from current hour
    """
    if not predictions_folder.exists():
        return False
    
    prediction_file = predictions_folder / f"{symbol}.json"
    
    if not prediction_file.exists():
        return False
    
    # Check file modification time
    try:
        file_mtime = prediction_file.stat().st_mtime
        file_datetime = datetime.fromtimestamp(file_mtime, tz=timezone.utc)
        file_hour_key = file_datetime.strftime("%Y%m%d%H")
        current_hour_key = get_current_hour_key()

        return file_hour_key == current_hour_key
    except Exception:
        return False


def collect_market_data_from_mt5(ollama_source_folder: Path, suffix: str, symbol_blacklist: List[str], lookback_periods: int, rsi_period: int, ma_period: int, pretty_json: bool) -> int:
    """
    Collect market data directly from MT5 and save to ollama/source folder.
    
    Args:
        ollama_source_folder: Target ollama/source folder
        suffix: Symbol suffix (e.g., "_ecn"); empty string means all MT5 symbols
        symbol_blacklist: Symbols or wildcard patterns to skip, e.g. ["BTCUSD", "X*"]
        lookback_periods: Number of periods to fetch
        rsi_period: RSI calculation period
        ma_period: MA calculation period
        pretty_json: Whether to format JSON with indentation
    
    Returns:
        Number of symbols processed
    """
    # Create target folder
    ollama_source_folder.mkdir(parents=True, exist_ok=True)
    
    # Clear old files from ollama/source
    for old_file in ollama_source_folder.glob("*.json"):
        old_file.unlink()
    
    # Get symbols from MT5
    try:
        symbols = get_symbols(suffix, blacklist=symbol_blacklist)
    except Exception as e:
        print(f"❌ Chyba při získávání symbolů z MT5: {e}")
        return 0

    scope_label = f"s příponou '{suffix}'" if suffix else "bez filtru přípony"
    if symbol_blacklist:
        scope_label += f" a blacklistem ({', '.join(symbol_blacklist)})"
    
    if not symbols:
        print(f"⚠️  Žádné symboly {scope_label} nenalezeny")
        return 0
    
    print(f"📊 Nalezeno {len(symbols)} symbolů {scope_label}")
    
    # Process each symbol
    ok_count = 0
    err_count = 0
    
    for symbol in symbols:
        try:
            payload = collect_symbol_payload(symbol, lookback_periods, rsi_period, ma_period)
            
            # Write to ollama/source folder
            out_path = ollama_source_folder / f"{symbol}.json"
            if pretty_json:
                out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            
            ok_count += 1
            print(f"✅ {symbol}: Data stažena z MT5")
            
        except Exception as exc:
            err_count += 1
            print(f"❌ {symbol}: Chyba - {exc}")
    
    print(f"\n📈 Celkem: {ok_count} úspěšných, {err_count} chyb z {len(symbols)} symbolů")
    return ok_count


def _extract_json_from_text(text: str) -> Optional[Dict]:
    """
    Extract JSON object from Ollama response text.
    
    Handles markdown code blocks and tries to find valid JSON.
    """
    if not text:
        return None
    
    # Remove markdown code blocks
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    
    if text.endswith("```"):
        text = text[:-3]
    
    text = text.strip()
    
    # Try to find JSON object
    try:
        # First try direct parse
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON from text
        match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    
    return None


def ask_ollama_prediction(symbol: str, data: Dict, ollama_url: str, ollama_model: str) -> Optional[Dict]:
    """
    Ask Ollama AI for trading prediction.
    
    Args:
        symbol: Trading symbol
        data: Market data with candles and oscillators
        ollama_url: Ollama API URL
        ollama_model: Ollama model name
    
    Returns:
        Dict with BUY/SELL/HOLD values or None if failed
    """
    # Build summary of market data
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
    
    # Create prompt similar to Gemini
    prompt = f"""Jsi finanční poradce a expert na technickou analýzu finančních instrumentů.

Posílám ti kompletní data pro instrument: {symbol}

Aktuální cena: {current_price}

Dostupná data:
- Časové rámce: 1h, 4h, day, week, month
- Pro každý timeframe máš: svíčkové formace, RSI, MA
- RSI hodnoty za jednotlivé timeframes: {json.dumps(oscillators_summary, indent=2)}

Kompletní data:
```json
{json.dumps(data, indent=2, ensure_ascii=False)}
```

Úkol:
Na základě fundamentální analýzy, svíčkových formací, RSI a MA hodnot proveď rizikové hodnocení:
- BUY (pravděpodobnost růstu)
- SELL (pravděpodobnost poklesu)
- HOLD (nejistota, doporučení držet)

{prompt_guidance}

Hodnocení je procentuální (0-100%), součet musí být 100%.

Odpověď ve formátu JSON:
{{
    "{symbol}": {{
        "BUY": <číslo 0-100>,
        "SELL": <číslo 0-100>,
        "HOLD": <číslo 0-100>,
        "reasoning": "<krátké zdůvodnění>"
    }}
}}

Odpověz pouze JSON, bez dalšího textu."""

    try:
        call_time = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        print(f"🤖 [{call_time}] Volám Ollama API ({ollama_model}) pro {symbol}...")
        
        request_data = {
            "model": ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.7,
                "top_p": 0.9
            }
        }
        
        with httpx.Client(timeout=180.0) as client:
            response = client.post(ollama_url, json=request_data)
            response.raise_for_status()
            
            result = response.json()
            prediction_text = result.get("response", "")
            
            if not prediction_text:
                print(f"⚠️  Prázdná odpověď od Ollama pro {symbol}")
                return None
            
            # Extract JSON from response
            parsed = _extract_json_from_text(prediction_text)
            
            if not parsed:
                print(f"⚠️  Nepodařilo se parsovat JSON z odpovědi pro {symbol}")
                return None
            
            # Extract values from parsed JSON
            # Response might be {"SYMBOL": {"BUY": ..., "SELL": ...}} or directly {"BUY": ..., "SELL": ...}
            prediction_data = None
            
            # Check if it's wrapped with symbol key
            if symbol in parsed:
                prediction_data = parsed[symbol]
            else:
                # Try to find any nested object with BUY/SELL/HOLD
                for key, value in parsed.items():
                    if isinstance(value, dict) and "BUY" in value:
                        prediction_data = value
                        break
                
                # If not found, assume the parsed JSON itself contains the values
                if not prediction_data and "BUY" in parsed:
                    prediction_data = parsed
            
            if not prediction_data:
                print(f"⚠️  Nenalezeny BUY/SELL/HOLD hodnoty v odpovědi pro {symbol}")
                return None
            
            # Validate and extract required fields
            try:
                result_dict = {
                    "BUY": float(prediction_data.get("BUY", 0)),
                    "SELL": float(prediction_data.get("SELL", 0)),
                    "HOLD": float(prediction_data.get("HOLD", 0)),
                    "reasoning": prediction_data.get("reasoning", "")
                }
                
                print(f"✅ Predikce pro {symbol} získána (BUY:{result_dict['BUY']}, SELL:{result_dict['SELL']}, HOLD:{result_dict['HOLD']})")
                return result_dict
                
            except (ValueError, TypeError) as e:
                print(f"⚠️  Chyba při extrakci hodnot pro {symbol}: {e}")
                return None
                
    except httpx.ConnectError:
        print(f"❌ Nelze se připojit k Ollama API na {ollama_url}")
        print("💡 Ujistěte se, že Ollama server běží: ollama serve")
        return None
    except httpx.TimeoutException:
        print(f"⏱️  Timeout při čekání na odpověď od Ollama pro {symbol}")
        return None
    except Exception as e:
        print(f"❌ Chyba při volání Ollama API pro {symbol}: {e}")
        return None


def process_symbol_with_ollama(
    symbol_file: Path,
    ollama_predictions_folder: Path,
    ollama_url: str,
    ollama_model: str
) -> bool:
    """
    Process one symbol and generate prediction using Ollama.
    
    Args:
        symbol_file: Path to symbol JSON file
        ollama_predictions_folder: Target folder for predictions
        ollama_url: Ollama API URL
        ollama_model: Ollama model name
    
    Returns:
        True if successful
    """
    try:
        # Read symbol data
        symbol = symbol_file.stem  # e.g., EURUSD_ecn
        data = json.loads(symbol_file.read_text(encoding="utf-8"))
        
        # Check if already processed this hour
        if was_processed_this_hour(symbol, ollama_predictions_folder):
            print(f"⏭️  Přeskakuji {symbol} - již byl zpracován v aktuální hodině")
            return False
        
        # Get prediction from Ollama
        prediction_dict = ask_ollama_prediction(symbol, data, ollama_url, ollama_model)
        
        if prediction_dict:
            # Create final output with required structure
            output_data = {
                "symbol": symbol,
                "BUY": prediction_dict["BUY"],
                "SELL": prediction_dict["SELL"],
                "HOLD": prediction_dict["HOLD"],
                "reasoning": prediction_dict["reasoning"],
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "model": ollama_model
            }
            
            # Save prediction to file (only symbol name)
            output_filename = f"{symbol}.json"
            output_path = ollama_predictions_folder / output_filename
            
            output_path.write_text(json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8")
            save_time = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
            print(f"💾 [{save_time}] Predikce uložena: {output_filename}")
            return True
        
        return False
        
    except Exception as e:
        print(f"❌ Chyba při zpracování {symbol_file.name}: {e}")
        return False


def ollama_service_loop(service_dest_folder: Path, stop_event) -> None:
    """
    Main Ollama service loop - runs independently from main logic.
    
    Args:
        service_dest_folder: SERVICE_DEST_FOLDER path
        stop_event: Threading event to signal shutdown
    """
    print("\n" + "="*60)
    print("🤖 Ollama Service Loop spuštěn")
    print("="*60)
    print("Funkcionalita: Nezávislé predikce pomocí Ollama AI")
    print("Model: deepseek-coder-v2")
    print("Ovládání: Změňte OLLAMA_ENABLED v .env za běhu")
    print("="*60 + "\n")
    
    # Load Ollama configuration
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
    ollama_model = os.getenv("OLLAMA_MODEL", "deepseek-coder-v2")
    
    # Load MT5 configuration
    mt5_login_raw = os.getenv("MT5_LOGIN")
    mt5_login = int(mt5_login_raw) if mt5_login_raw else None
    mt5_password = os.getenv("MT5_PASSWORD")
    mt5_server = os.getenv("MT5_SERVER")
    symbol_suffix = os.getenv("MT5_SYMBOL_SUFFIX", "_ecn").strip()
    symbol_blacklist = [item.strip() for item in os.getenv("MT5_SYMBOL_BLACKLIST", "").split(",") if item.strip()]
    lookback_periods = int(os.getenv("LOOKBACK_PERIODS", "30"))
    rsi_period = int(os.getenv("RSI_PERIOD", "14"))
    ma_period = int(os.getenv("MA_PERIOD", "20"))
    pretty_json = os.getenv("PRETTY_JSON", "true").lower() in {"true", "1", "yes", "y", "on"}
    
    # Initialize MT5 connection
    print("🔌 Připojuji se k MetaTrader 5...")
    try:
        initialize_mt5(login=mt5_login, password=mt5_password, server=mt5_server)
    except RuntimeError as exc:
        print(f"❌ MT5 inicializace selhala: {exc}")
        print("⚠️  Ollama Service nemůže pokračovat bez MT5 připojení")
        return
    
    print(f"✅ MT5 připojen - účet: {get_account_login()}")
    
    cycle_count = 0
    
    while not stop_event.is_set():
        try:
            cycle_count += 1
            
            # Check if Ollama is enabled
            enabled = is_ollama_enabled()
            
            if not enabled:
                print(f"\n⏸️  [Ollama Cyklus #{cycle_count}] Funkcionalita není aktivní")
                print("   Čekám 5 minut... (Změňte OLLAMA_ENABLED=true v .env pro aktivaci)")
                
                # Sleep in small intervals to allow graceful shutdown
                for _ in range(300):  # 5 minutes = 300 seconds
                    if stop_event.is_set():
                        shutdown_mt5()
                        return
                    time.sleep(1)
                continue
            
            print(f"\n{'='*60}")
            print(f"▶️  Ollama Cyklus #{cycle_count}")
            print(f"{'='*60}")
            
            # Create ollama folders
            ollama_base = service_dest_folder / "ollama"
            ollama_source = ollama_base / "source"
            ollama_predictions = ollama_base / "predikce"
            
            ollama_source.mkdir(parents=True, exist_ok=True)
            ollama_predictions.mkdir(parents=True, exist_ok=True)
            
            # Collect market data from MT5
            print("\n📥 Stahuji aktuální tržní data z MT5...")
            symbols_collected = collect_market_data_from_mt5(
                ollama_source,
                symbol_suffix,
                symbol_blacklist,
                lookback_periods,
                rsi_period,
                ma_period,
                pretty_json
            )
            
            if symbols_collected == 0:
                print("⚠️  Žádná tržní data k dispozici")
                print("   Čekám 5 minut na další pokus...")
                
                for _ in range(300):
                    if stop_event.is_set():
                        shutdown_mt5()
                        return
                    time.sleep(1)
                continue
            
            print(f"\n✅ Staženo {symbols_collected} symbolů z MT5")
            
            # Start timestamp for predictions
            start_time = datetime.now(tz=timezone.utc)
            start_time_str = start_time.strftime("%H:%M:%S")
            print(f"\n🔮 Začínám generovat predikce pomocí Ollama... [{start_time_str}]")
            
            # Process each symbol
            processed_count = 0
            skipped_count = 0
            
            for symbol_file in ollama_source.glob("*.json"):
                if stop_event.is_set():
                    print("\n🛑 Zastavuji Ollama service...")
                    return
                
                success = process_symbol_with_ollama(
                    symbol_file,
                    ollama_predictions,
                    ollama_url,
                    ollama_model
                )
                
                if success:
                    processed_count += 1
                else:
                    skipped_count += 1
                
                # Small delay between requests
                time.sleep(2)
            
            # End timestamp for predictions
            end_time = datetime.now(tz=timezone.utc)
            end_time_str = end_time.strftime("%H:%M:%S")
            duration = (end_time - start_time).total_seconds()
            duration_mins = int(duration // 60)
            duration_secs = int(duration % 60)
            
            print(f"\n🏁 Predikce dokončeny [{end_time_str}] - trvání: {duration_mins}m {duration_secs}s")
            print(f"\n{'='*60}")
            print(f"✅ Ollama cyklus #{cycle_count} dokončen")
            print(f"   Zpracováno: {processed_count} symbolů")
            print(f"   Přeskočeno: {skipped_count} symbolů")
            print(f"   Celková doba: {duration_mins} minut {duration_secs} sekund")
            print(f"{'='*60}")
            print("\n⏳ Čekám 10 minut před dalším cyklem...")
            
            # Wait 10 minutes before next cycle
            for _ in range(600):  # 10 minutes = 600 seconds
                if stop_event.is_set():
                    return
                time.sleep(1)
            
        except Exception as e:
            print(f"\n❌ Chyba v Ollama service loop: {e}")
            print("⏳ Čekám 5 minut před dalším pokusem...")
            
            for _ in range(300):
                if stop_event.is_set():
                    shutdown_mt5()
                    return
                time.sleep(1)
    
    shutdown_mt5()
    print("\n🛑 Ollama Service Loop ukončen")
