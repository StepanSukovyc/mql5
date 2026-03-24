"""Final trading decision module - queries remaining predictions and open positions."""

from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
import MetaTrader5 as mt5


# Global variable to track Gemini quota suspension
_gemini_suspended_until: Optional[datetime] = None


def _get_gemini_full_control_every_n_trades() -> int:
	"""Return how often Gemini should fully control lot size and take profit."""
	raw = os.getenv("GEMINI_FULL_CONTROL_EVERY_N_TRADES", "3")
	try:
		value = int(raw)
		if value <= 0:
			return 3
		return value
	except (TypeError, ValueError):
		return 3


def _count_successful_trades(service_folder: Path) -> int:
	"""Count successful trades from trade log CSV."""
	if service_folder is None:
		return 0

	log_file = service_folder / "trade_logs" / "trades.csv"
	if not log_file.exists():
		return 0

	success_count = 0
	try:
		with open(log_file, "r", encoding="utf-8", newline="") as f:
			reader = csv.DictReader(f)
			for row in reader:
				raw_success = str(row.get("success", "")).strip().lower()
				if raw_success in {"true", "1", "yes"}:
					success_count += 1
	except Exception as exc:
		print(f"⚠️  Could not read trade history for mode selection: {exc}")
		return 0

	return success_count


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


def log_trade(symbol: str, action: str, lot_size: float, price: float, success: bool, error_msg: str = "", service_folder: Path = None) -> None:
	"""
	Log trade execution to CSV file.
	
	Args:
		symbol: Trading symbol
		action: BUY or SELL
		lot_size: Lot size used
		price: Execution price
		success: Whether trade succeeded
		error_msg: Error message if failed
		service_folder: SERVICE_DEST_FOLDER for saving logs
	"""
	if service_folder is None:
		return
	
	logs_folder = service_folder / "trade_logs"
	logs_folder.mkdir(parents=True, exist_ok=True)
	
	log_file = logs_folder / "trades.csv"
	
	# Check if file exists to write header
	file_exists = log_file.exists()
	
	with open(log_file, "a", newline="", encoding="utf-8") as f:
		writer = csv.writer(f)
		
		if not file_exists:
			writer.writerow(["timestamp", "symbol", "action", "lot_size", "price", "success", "error_msg"])
		
		writer.writerow([
			datetime.now(tz=timezone.utc).isoformat(),
			symbol,
			action,
			lot_size,
			price,
			success,
			error_msg
		])
	
	print(f"📝 Trade logged to: {log_file}")


def validate_symbol(symbol: str) -> Tuple[bool, str]:
	"""
	Validate symbol and ensure it's available in MarketWatch.
	
	Args:
		symbol: Trading symbol (e.g., "EURUSD_ecn")
	
	Returns:
		Tuple of (is_valid, error_message)
	"""
	# Check if symbol exists
	symbol_info = mt5.symbol_info(symbol)
	
	if symbol_info is None:
		return False, f"Symbol {symbol} does not exist"
	
	# Check if symbol is visible in MarketWatch
	if not symbol_info.visible:
		print(f"   ⚠️  Symbol {symbol} not in MarketWatch, adding...")
		if not mt5.symbol_select(symbol, True):
			return False, f"Failed to add {symbol} to MarketWatch"
		print(f"   ✅ Symbol {symbol} added to MarketWatch")
	
	# Check if trading is allowed
	if not symbol_info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL:
		return False, f"Trading not allowed for {symbol}"
	
	return True, ""


def validate_lot_size(symbol: str, lot_size: float) -> Tuple[float, str]:
	"""
	Validate and adjust lot size according to broker requirements.
	
	Args:
		symbol: Trading symbol
		lot_size: Requested lot size
	
	Returns:
		Tuple of (adjusted_lot_size, error_message)
	"""
	symbol_info = mt5.symbol_info(symbol)
	
	if symbol_info is None:
		return 0.0, f"Cannot get symbol info for {symbol}"
	
	min_lot = symbol_info.volume_min
	max_lot = symbol_info.volume_max
	lot_step = symbol_info.volume_step
	
	# Check if lot_size is too small
	if lot_size < min_lot:
		return min_lot, f"Lot size {lot_size} too small, adjusted to min {min_lot}"
	
	# Check if lot_size is too large
	if lot_size > max_lot:
		return max_lot, f"Lot size {lot_size} too large, adjusted to max {max_lot}"
	
	# Round to lot_step
	adjusted_lot = round(lot_size / lot_step) * lot_step
	adjusted_lot = round(adjusted_lot, 2)  # Round to 2 decimal places
	
	if adjusted_lot != lot_size:
		return adjusted_lot, f"Lot size adjusted from {lot_size} to {adjusted_lot} (step: {lot_step})"
	
	return lot_size, ""


def check_margin_requirements(symbol: str, action: str, lot_size: float) -> Tuple[bool, str]:
	"""
	Check if there is enough free margin for the trade.
	
	Args:
		symbol: Trading symbol
		action: BUY or SELL
		lot_size: Lot size to trade
	
	Returns:
		Tuple of (has_margin, error_message)
	"""
	account_info = mt5.account_info()
	if account_info is None:
		return False, "Failed to get account info"
	
	free_margin = account_info.margin_free
	
	# Calculate required margin for this trade
	order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
	
	# Use symbol_info to calculate margin
	symbol_info = mt5.symbol_info(symbol)
	if symbol_info is None:
		return False, f"Cannot get symbol info for {symbol}"
	
	# Get current price
	tick = mt5.symbol_info_tick(symbol)
	if tick is None:
		return False, f"Cannot get tick for {symbol}"
	
	price = tick.ask if action == "BUY" else tick.bid
	
	# Calculate margin requirement
	# Note: This is approximate, MT5 has more complex margin calculation
	required_margin = mt5.order_calc_margin(order_type, symbol, lot_size, price)
	
	if required_margin is None:
		return False, f"Cannot calculate margin for {symbol}"
	
	if required_margin > free_margin:
		return False, f"Insufficient margin: required {required_margin:.2f}, available {free_margin:.2f}"
	
	print(f"   ✅ Margin check passed: required {required_margin:.2f}, available {free_margin:.2f}")
	return True, ""


def calculate_lot_size(balance: float) -> float:
	"""
	Calculate lot size based on account balance.
	
	Formula: floor((balance + 500) / 500) / 100
	
	Example:
		balance = 1893 → (1893 + 500) / 500 = 4.786 → floor = 4 → 4/100 = 0.04
	
	Args:
		balance: Current account balance
	
	Returns:
		Lot size (e.g., 0.04)
	"""
	import math
	numerator = balance + 500
	quotient = numerator / 500
	whole_number = math.floor(quotient)
	lot_size = whole_number / 100
	
	print(f"\n💰 Lot Size Calculation:")
	print(f"   Balance: {balance:.2f}")
	print(f"   ({balance:.2f} + 500) / 500 = {quotient:.3f}")
	print(f"   Floor: {whole_number}")
	print(f"   Lot size: {lot_size:.2f}")
	
	return lot_size


def execute_trade(
	symbol: str,
	action: str,
	lot_size: float,
	service_folder: Path = None,
	take_profit: Optional[float] = None,
) -> bool:
	"""
	Execute a trade on MT5 with comprehensive validation.
	
	Args:
		symbol: Trading symbol (e.g., "EURUSD_ecn")
		action: "BUY" or "SELL"
		lot_size: Number of lots to trade
		service_folder: SERVICE_DEST_FOLDER for logging
	
	Returns:
		True if trade was successful
	"""
	print(f"\n🔄 Executing trade...")
	print(f"   Symbol: {symbol}")
	print(f"   Action: {action}")
	print(f"   Requested Lot Size: {lot_size}")
	print(f"   Take Profit: {take_profit if take_profit is not None else 'None'}")
	
	# Validate symbol
	is_valid, error_msg = validate_symbol(symbol)
	if not is_valid:
		print(f"❌ Symbol validation failed: {error_msg}")
		log_trade(symbol, action, lot_size, 0.0, False, error_msg, service_folder)
		return False
	
	# Validate and adjust lot size
	adjusted_lot, lot_msg = validate_lot_size(symbol, lot_size)
	if adjusted_lot == 0.0:
		print(f"❌ Lot size validation failed: {lot_msg}")
		log_trade(symbol, action, lot_size, 0.0, False, lot_msg, service_folder)
		return False
	
	if lot_msg:
		print(f"   ⚠️  {lot_msg}")
	
	lot_size = adjusted_lot
	print(f"   Final Lot Size: {lot_size}")
	
	# Check margin requirements
	has_margin, margin_msg = check_margin_requirements(symbol, action, lot_size)
	if not has_margin:
		print(f"❌ Margin check failed: {margin_msg}")
		log_trade(symbol, action, lot_size, 0.0, False, margin_msg, service_folder)
		return False
	
	# Get current price
	tick = mt5.symbol_info_tick(symbol)
	if tick is None:
		error_msg = f"Failed to get tick for {symbol}: {mt5.last_error()}"
		print(f"❌ {error_msg}")
		log_trade(symbol, action, lot_size, 0.0, False, error_msg, service_folder)
		return False
	
	# Determine order type and price
	if action == "BUY":
		order_type = mt5.ORDER_TYPE_BUY
		price = tick.ask
	elif action == "SELL":
		order_type = mt5.ORDER_TYPE_SELL
		price = tick.bid
	else:
		error_msg = f"Invalid action: {action} (must be BUY or SELL)"
		print(f"❌ {error_msg}")
		log_trade(symbol, action, lot_size, 0.0, False, error_msg, service_folder)
		return False
	
	print(f"   Price: {price}")

	validated_tp: Optional[float] = None
	if take_profit is not None:
		try:
			validated_tp = float(take_profit)
		except (TypeError, ValueError):
			error_msg = f"Invalid take_profit value: {take_profit}"
			print(f"❌ {error_msg}")
			log_trade(symbol, action, lot_size, price, False, error_msg, service_folder)
			return False

		if validated_tp <= 0:
			error_msg = f"Invalid take_profit <= 0: {validated_tp}"
			print(f"❌ {error_msg}")
			log_trade(symbol, action, lot_size, price, False, error_msg, service_folder)
			return False

		if action == "BUY" and validated_tp <= price:
			error_msg = f"Invalid take_profit for BUY: TP ({validated_tp}) must be > market price ({price})"
			print(f"❌ {error_msg}")
			log_trade(symbol, action, lot_size, price, False, error_msg, service_folder)
			return False

		if action == "SELL" and validated_tp >= price:
			error_msg = f"Invalid take_profit for SELL: TP ({validated_tp}) must be < market price ({price})"
			print(f"❌ {error_msg}")
			log_trade(symbol, action, lot_size, price, False, error_msg, service_folder)
			return False

		print(f"   Validated TP: {validated_tp}")
	
	# Prepare trade request
	request = {
		"action": mt5.TRADE_ACTION_DEAL,
		"symbol": symbol,
		"volume": lot_size,
		"type": order_type,
		"price": price,
		"deviation": 20,
		"magic": 234000,
		"comment": "Gemini AI decision",
		"type_time": mt5.ORDER_TIME_GTC,
		"type_filling": mt5.ORDER_FILLING_IOC,
	}

	if validated_tp is not None:
		request["tp"] = validated_tp
	
	# Send order
	result = mt5.order_send(request)
	
	if result is None:
		error_msg = f"Order send failed: {mt5.last_error()}"
		print(f"❌ {error_msg}")
		log_trade(symbol, action, lot_size, price, False, error_msg, service_folder)
		return False
	
	if result.retcode != mt5.TRADE_RETCODE_DONE:
		error_msg = f"Order failed with retcode: {result.retcode} - {result.comment}"
		print(f"❌ {error_msg}")
		log_trade(symbol, action, lot_size, price, False, error_msg, service_folder)
		return False
	
	print(f"✅ Trade executed successfully!")
	print(f"   Order: {result.order}")
	print(f"   Volume: {result.volume}")
	print(f"   Price: {result.price}")
	
	log_trade(symbol, action, lot_size, result.price, True, "", service_folder)
	return True


def get_open_positions() -> List[Dict]:
	"""
	Get all currently open positions on the MT5 account.
	
	Returns:
		List of position dicts with:
		- symbol
		- type (BUY/SELL)
		- open_time
		- volume (lot size)
		- open_price
		- current_price
		- pnl (profit/loss)
		- swap
	"""
	positions = mt5.positions_get()
	if positions is None:
		raise RuntimeError(f"Failed to get positions: {mt5.last_error()}")
	
	open_positions = []
	
	for pos in positions:
		# Get current tick price for the symbol
		tick = mt5.symbol_info_tick(pos.symbol)
		current_price = tick.bid if tick else pos.price_open
		
		# Calculate current PnL (using actual profit from MT5)
		pnl = float(pos.profit)
		
		position_info = {
			"symbol": pos.symbol,
			"type": "BUY" if pos.type == 0 else "SELL",
			"open_time": datetime.fromtimestamp(pos.time, tz=timezone.utc).isoformat(),
			"volume": float(pos.volume),
			"open_price": float(pos.price_open),
			"current_price": float(current_price),
			"pnl": pnl,
			"swap": float(pos.swap)
		}
		open_positions.append(position_info)
	
	return open_positions


def get_account_state() -> Dict:
	"""Get current account state (balance, equity, free margin)."""
	account = mt5.account_info()
	if account is None:
		raise RuntimeError(f"Failed to get account info: {mt5.last_error()}")
	
	return {
		"balance": float(account.balance),
		"equity": float(account.equity),
		"margin": float(account.margin),
		"margin_free": float(account.margin_free),
		"margin_percent": (account.margin_free / account.balance * 100) if account.balance > 0 else 0
	}


def load_predictions(predictions_folder: Path) -> List[Dict]:
	"""
	Load all remaining prediction files (not deleted by filter).
	
	Returns:
		List of prediction objects with symbol, BUY, SELL, HOLD, reasoning
	"""
	predictions = []
	
	for pred_file in predictions_folder.glob("*.json"):
		try:
			with open(pred_file, "r", encoding="utf-8") as f:
				content = f.read()
			
			# Clean markdown formatting if present
			cleaned_content = _clean_gemini_response(content)
			prediction = json.loads(cleaned_content)
			
			buy_pct = prediction.get("BUY", 0)
			sell_pct = prediction.get("SELL", 0)
			
			# Only include predictions with BUY or SELL >= 35
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
	"""
	Ask Gemini AI for final trading decision based on predictions and current account state.
	
	Args:
		predictions: List of predictions (symbol, BUY, SELL, HOLD, reasoning)
		open_positions: List of currently open positions
		account_state: Current account balance, equity, margin info
		api_key: Gemini API key
		api_url: Gemini API URL
		excluded_symbols: List of symbols to exclude from recommendations
	
	Returns:
		Gemini's decision text (JSON format) or None if failed
	"""
	global _gemini_suspended_until
	
	# Check if Gemini is suspended
	if _gemini_suspended_until is not None:
		now = datetime.now(tz=timezone.utc)
		if now < _gemini_suspended_until:
			remaining = (_gemini_suspended_until - now).total_seconds() / 3600
			print(f"  ⏸️  Gemini suspended until {_gemini_suspended_until.isoformat()}")
			print(f"     Remaining: {remaining:.1f} hours")
			return None
		else:
			print(f"  ✅ Gemini suspension lifted")
			_gemini_suspended_until = None
	
	# Filter out excluded symbols
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
			f"- U ne-plně řízených obchodů se lot_size a take_profit ve finální exekuci ignoruje."
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
		print("  📡 Dotazuji Gemini na finální rozhodnutí...")
		
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
			# Suspend Gemini until midnight of next day
			now = datetime.now(tz=timezone.utc)
			tomorrow = now + timedelta(days=1)
			midnight_tomorrow = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
			_gemini_suspended_until = midnight_tomorrow
			
			hours_until = (midnight_tomorrow - now).total_seconds() / 3600
			print(f"  🚫 Quota překročena!")
			print(f"  ⏸️  Gemini suspended until {midnight_tomorrow.isoformat()}")
			print(f"     Suspension duration: {hours_until:.1f} hours")
			return None
		
		response.raise_for_status()
		
		response_data = response.json()
		text_response = response_data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
		
		if text_response:
			# Clean markdown formatting from response
			cleaned_response = _clean_gemini_response(text_response)
			print(f"  ✅ Finální rozhodnutí získáno")
			return cleaned_response
		else:
			print(f"  ⚠️  Prázdná odpověď od Gemini")
			return None
			
	except httpx.HTTPError as exc:
		print(f"  ❌ HTTP chyba při dotazu na Gemini: {exc}")
		return None
	except Exception as exc:
		print(f"  ❌ Chyba při dotazu na Gemini: {exc}")
		return None


def make_final_trading_decision(predictions_folder: Path, service_folder: Path) -> bool:
	"""
	Make final trading decision and execute trade with retry mechanism.
	
	Args:
		predictions_folder: Folder with filtered predictions
		service_folder: SERVICE_DEST_FOLDER for saving results and logs
	
	Returns:
		True if decision was made and saved
	"""
	print("\n" + "="*60)
	print("🎯 Final Trading Decision Phase")
	print("="*60)
	
	try:
		# Load predictions
		print("\n📊 Loading remaining predictions...")
		predictions = load_predictions(predictions_folder)
		print(f"   Found {len(predictions)} strong predictions (BUY/SELL >= 35%)")
		
		if not predictions:
			print("⚠️  No strong predictions available, skipping final decision")
			return False
		
		# Get account state
		print("\n💰 Getting account state...")
		account_state = get_account_state()
		print(f"   Balance: {account_state['balance']:.2f}")
		print(f"   Equity: {account_state['equity']:.2f}")
		print(f"   Free Margin: {account_state['margin_free']:.2f} ({account_state['margin_percent']:.2f}%)")
		
		# Get open positions
		print("\n📈 Getting open positions...")
		open_positions = get_open_positions()
		print(f"   Open positions: {len(open_positions)}")
		for pos in open_positions:
			print(f"     - {pos['symbol']}: {pos['type']} {pos['volume']} (PnL: {pos['pnl']:.2f})")
		
		# Load Gemini config
		api_key = os.getenv("GEMINI_API_KEY")
		api_url = os.getenv("GEMINI_URL")
		
		if not api_key or not api_url:
			raise ValueError("GEMINI_API_KEY or GEMINI_URL not found in environment")
		
		# Retry mechanism for symbol validation failures
		max_retries = 3
		excluded_symbols = []
		full_control_every_n = _get_gemini_full_control_every_n_trades()
		successful_trades = _count_successful_trades(service_folder)
		next_trade_number = successful_trades + 1
		gemini_full_control_mode = (next_trade_number % full_control_every_n) == 0

		print("\n🧭 Trade Execution Mode")
		print(f"   Successful trades so far: {successful_trades}")
		print(f"   Current trade number: #{next_trade_number}")
		print(f"   Full Gemini control every N trades: N={full_control_every_n}")
		print(
			"   Mode: "
			+ (
				"FULL GEMINI (lot_size + take_profit from Gemini)"
				if gemini_full_control_mode
				else "STANDARD (calculated lot_size, no take_profit)"
			)
		)
		
		for attempt in range(max_retries):
			print(f"\n🔄 Decision attempt {attempt + 1}/{max_retries}")
			
			# Ask Gemini for final decision
			decision_text = ask_gemini_final_decision(
				predictions,
				open_positions,
				account_state,
				api_key,
				api_url,
				excluded_symbols if excluded_symbols else None,
				trade_number=next_trade_number,
				full_control_every_n=full_control_every_n,
				gemini_full_control_mode=gemini_full_control_mode,
			)
			
			if not decision_text:
				print("❌ Failed to get decision from Gemini")
				return False
			
			# Save decision
			timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
			geminipredictions_folder = service_folder / "geminipredictions"
			geminipredictions_folder.mkdir(parents=True, exist_ok=True)
			
			decision_file = geminipredictions_folder / f"PREDIKCE_{timestamp}.json"
			decision_file.write_text(decision_text, encoding="utf-8")
			
			print(f"\n✅ Final decision saved:")
			print(f"   File: {decision_file}")
			print(f"\n📋 Decision Content:")
			print(decision_text)
			
			# Parse decision and execute trade
			try:
				decision = json.loads(decision_text)
				symbol = decision.get("recommended_symbol")
				action = decision.get("action")
				gemini_lot_size = decision.get("lot_size")
				gemini_take_profit = decision.get("take_profit")
				
				if not symbol or not action:
					print("⚠️  Missing symbol or action in decision, skipping trade execution")
					return False
				
				# Validate symbol first
				is_valid, error_msg = validate_symbol(symbol)
				if not is_valid:
					print(f"⚠️  Symbol validation failed: {error_msg}")
					print(f"   Adding {symbol} to exclusion list and retrying...")
					excluded_symbols.append(symbol)
					
					# Check if we have more predictions to try
					remaining = len([p for p in predictions if p.get("symbol") not in excluded_symbols])
					if remaining == 0:
						print("❌ No more symbols to try")
						return False
					
					continue  # Retry with exclusion
				
				if gemini_full_control_mode:
					try:
						lot_size = float(gemini_lot_size)
					except (TypeError, ValueError):
						print("⚠️  Gemini lot_size missing/invalid in FULL GEMINI mode")
						return False

					try:
						take_profit = float(gemini_take_profit)
					except (TypeError, ValueError):
						print("⚠️  Gemini take_profit missing/invalid in FULL GEMINI mode")
						return False

					print(f"   Using Gemini lot_size: {lot_size}")
					print(f"   Using Gemini take_profit: {take_profit}")
				else:
					# Standard mode: lot size is computed and TP is intentionally disabled
					lot_size = calculate_lot_size(account_state['balance'])
					take_profit = None
					print("   Standard mode active: take_profit disabled for this trade")

					has_margin, margin_msg = check_margin_requirements(symbol, action, lot_size)
					if not has_margin and "insufficient margin" in margin_msg.lower():
						print("   Calculated lot_size failed margin check in STANDARD mode")
						print("   Trying lot_size from prediction instead...")

						try:
							fallback_lot_size = float(gemini_lot_size)
						except (TypeError, ValueError):
							print("⚠️  Prediction lot_size missing/invalid, cannot use margin fallback")
							return False

						if fallback_lot_size <= 0:
							print(f"⚠️  Prediction lot_size invalid for fallback: {fallback_lot_size}")
							return False

						lot_size = fallback_lot_size
						print(f"   Fallback to prediction lot_size: {lot_size}")
				
				if lot_size <= 0:
					print(f"⚠️  Final lot size is {lot_size}, skipping trade execution")
					return False
				
				# Execute trade
				success = execute_trade(symbol, action, lot_size, service_folder, take_profit)
				
				if success:
					print("\n🎉 Trade executed successfully!")
					print("\n" + "="*60)
					print("✅ Final Trading Decision Completed")
					print("="*60)
					return True
				else:
					print("\n⚠️  Trade execution failed")
					# Don't retry on trade execution failure, just return
					return False
			
			except json.JSONDecodeError as exc:
				print(f"⚠️  Failed to parse decision JSON: {exc}")
				return False
			except Exception as exc:
				print(f"⚠️  Error during trade execution: {exc}")
				return False
		
		# If we exhausted all retries
		print(f"❌ Exhausted all {max_retries} retry attempts")
		return False
		
	except Exception as exc:
		print(f"❌ Error in final decision phase: {exc}")
		import traceback
		traceback.print_exc()
		return False
