"""Final trading decision module - queries remaining predictions and open positions."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import MetaTrader5 as mt5


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


def execute_trade(symbol: str, action: str, lot_size: float) -> bool:
	"""
	Execute a trade on MT5.
	
	Args:
		symbol: Trading symbol (e.g., "EURUSD_ecn")
		action: "BUY" or "SELL"
		lot_size: Number of lots to trade
	
	Returns:
		True if trade was successful
	"""
	print(f"\n🔄 Executing trade...")
	print(f"   Symbol: {symbol}")
	print(f"   Action: {action}")
	print(f"   Lot Size: {lot_size}")
	
	# Get current price
	tick = mt5.symbol_info_tick(symbol)
	if tick is None:
		print(f"❌ Failed to get tick for {symbol}: {mt5.last_error()}")
		return False
	
	# Determine order type and price
	if action == "BUY":
		order_type = mt5.ORDER_TYPE_BUY
		price = tick.ask
	elif action == "SELL":
		order_type = mt5.ORDER_TYPE_SELL
		price = tick.bid
	else:
		print(f"❌ Invalid action: {action} (must be BUY or SELL)")
		return False
	
	print(f"   Price: {price}")
	
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
	
	# Send order
	result = mt5.order_send(request)
	
	if result is None:
		print(f"❌ Order send failed: {mt5.last_error()}")
		return False
	
	if result.retcode != mt5.TRADE_RETCODE_DONE:
		print(f"❌ Order failed with retcode: {result.retcode}")
		print(f"   Comment: {result.comment}")
		return False
	
	print(f"✅ Trade executed successfully!")
	print(f"   Order: {result.order}")
	print(f"   Volume: {result.volume}")
	print(f"   Price: {result.price}")
	
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
	api_url: str
) -> Optional[str]:
	"""
	Ask Gemini AI for final trading decision based on predictions and current account state.
	
	Args:
		predictions: List of predictions (symbol, BUY, SELL, HOLD, reasoning)
		open_positions: List of currently open positions
		account_state: Current account balance, equity, margin info
		api_key: Gemini API key
		api_url: Gemini API URL
	
	Returns:
		Gemini's decision text (JSON format) or None if failed
	"""
	prompt = f"""Jsi expert obchodní poradce. Musíš na základě analýzy učinit finální obchodní rozhodnutí.

DOSTUPNÉ INFORMACE:

1. Aktuální stav účtu:
{json.dumps(account_state, indent=2)}

2. Aktuálně otevřené pozice:
{json.dumps(open_positions, indent=2)}

3. Dostupné obchodní predikce (filtrované - pouze ty s BUY/SELL >= 35%):
{json.dumps(predictions, indent=2)}

ÚKOL:
Na základě všech dostupných informací (predikce, otevřené pozice, stav účtu):

1. Vyber PRÁVĚ JEDEN měnový pár z dostupných predikcí
2. Rozhodni se pro BUY nebo SELL
3. Doporuč velikost lotu (berouc v úvahu aktuální marži a risk management)
4. Zdůvodni rozhodnutí

Odpověď prosím formátuj POUZE jako JSON bez dalšího textu, v tomto formátu:

{{
  "recommended_symbol": "EURUSD_ecn",
  "action": "BUY",
  "lot_size": 0.5,
  "reasoning": "..."
}}

Kde lot_size je hodnota pro reálný obchod a reasoning obsahuje stručné vysvětlení"""
	
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
			print(f"  ⚠️  Quota překročena, čekám 60 sekund...")
			import time
			time.sleep(60)
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
			
	except Exception as exc:
		print(f"  ❌ Chyba při dotazu na Gemini: {exc}")
		return None


def make_final_trading_decision(predictions_folder: Path, service_folder: Path) -> bool:
	"""
	Make final trading decision and save it.
	
	Args:
		predictions_folder: Folder with filtered predictions
		service_folder: SERVICE_DEST_FOLDER for saving results
	
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
		
		# Ask Gemini for final decision
		decision_text = ask_gemini_final_decision(
			predictions,
			open_positions,
			account_state,
			api_key,
			api_url
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
			
			if not symbol or not action:
				print("⚠️  Missing symbol or action in decision, skipping trade execution")
			else:
				# Calculate lot size based on account balance
				lot_size = calculate_lot_size(account_state['balance'])
				
				if lot_size <= 0:
					print(f"⚠️  Calculated lot size is {lot_size}, skipping trade execution")
				else:
					# Execute trade
					success = execute_trade(symbol, action, lot_size)
					
					if success:
						print("\n🎉 Trade executed successfully!")
					else:
						print("\n⚠️  Trade execution failed")
		
		except json.JSONDecodeError as exc:
			print(f"⚠️  Failed to parse decision JSON: {exc}")
		except Exception as exc:
			print(f"⚠️  Error during trade execution: {exc}")
		
		print("\n" + "="*60)
		print("✅ Final Trading Decision Completed")
		print("="*60)
		
		return True
		
	except Exception as exc:
		print(f"❌ Error in final decision phase: {exc}")
		import traceback
		traceback.print_exc()
		return False
