"""Final trading decision orchestration."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from account_state import get_account_state
from gemini_config import load_gemini_api_config
from gemini_decision import ask_gemini_final_decision, load_predictions
from mt5_positions import get_open_positions
from trade_execution import execute_trade
from trade_history import count_successful_trades
from trading_validation import check_margin_requirements, validate_symbol


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



def _print_account_state(account_state: Dict) -> None:
	"""Print a concise account-state summary."""
	print("\n💰 Getting account state...")
	print(f"   Strategy Balance Cap: {account_state['balance_cap']:.2f}")
	print(f"   Balance: {account_state['balance']:.2f}")
	print(f"   Equity: {account_state['equity']:.2f}")
	print(f"   Free Margin: {account_state['margin_free']:.2f} ({account_state['margin_percent']:.2f}%)")
	if account_state.get("balance_reserve", 0) > 0:
		print(f"   Safety reserve outside strategy: {account_state['balance_reserve']:.2f}")
		print(f"   Raw Balance: {account_state['raw_balance']:.2f}")
		print(f"   Raw Free Margin: {account_state['raw_margin_free']:.2f}")


def _print_open_positions(open_positions: List[Dict]) -> None:
	"""Print open positions summary."""
	print("\n📈 Getting open positions...")
	print(f"   Open positions: {len(open_positions)}")
	for pos in open_positions:
		print(f"     - {pos['symbol']}: {pos['type']} {pos['volume']} (PnL: {pos['pnl']:.2f})")


def _load_gemini_api_config() -> Tuple[str, str]:
	"""Load Gemini API config from environment."""
	return load_gemini_api_config()


def _print_trade_mode(successful_trades: int, next_trade_number: int, full_control_every_n: int, gemini_full_control_mode: bool) -> None:
	"""Print the current trade execution mode."""
	print("\n🧭 Trade Execution Mode")
	print(f"   Successful trades so far: {successful_trades}")
	print(f"   Current trade number: #{next_trade_number}")
	print(f"   Gemini take_profit control every N trades: N={full_control_every_n}")
	print(
		"   Mode: "
		+ (
			"FULL GEMINI TP MODE (lot_size + take_profit from Gemini)"
			if gemini_full_control_mode
			else "PREDICTION LOT MODE (lot_size from Gemini, no take_profit)"
		)
	)


def _save_decision_text(service_folder: Path, decision_text: str) -> None:
	"""Persist the Gemini decision JSON into the service output folder."""
	timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
	geminipredictions_folder = service_folder / "geminipredictions"
	geminipredictions_folder.mkdir(parents=True, exist_ok=True)

	decision_file = geminipredictions_folder / f"PREDIKCE_{timestamp}.json"
	decision_file.write_text(decision_text, encoding="utf-8")

	print("\n✅ Final decision saved:")
	print(f"   File: {decision_file}")
	print("\n📋 Decision Content:")
	print(decision_text)


def _parse_decision(decision_text: str) -> Optional[Tuple[str, str, object, object]]:
	"""Parse Gemini decision JSON and return required fields."""
	try:
		decision = json.loads(decision_text)
	except json.JSONDecodeError as exc:
		print(f"⚠️  Failed to parse decision JSON: {exc}")
		return None

	symbol = decision.get("recommended_symbol")
	action = decision.get("action")
	if not symbol or not action:
		print("⚠️  Missing symbol or action in decision, skipping trade execution")
		return None

	return symbol, action, decision.get("lot_size"), decision.get("take_profit")


def _handle_invalid_symbol(symbol: str, error_msg: str, predictions: List[Dict], excluded_symbols: List[str]) -> bool:
	"""Update exclusions after a symbol validation failure."""
	print(f"⚠️  Symbol validation failed: {error_msg}")
	print(f"   Adding {symbol} to exclusion list and retrying...")
	excluded_symbols.append(symbol)

	remaining = len([p for p in predictions if p.get("symbol") not in excluded_symbols])
	if remaining == 0:
		print("❌ No more symbols to try")
		return False

	return True


def _resolve_trade_parameters(
	*,
	gemini_full_control_mode: bool,
	gemini_lot_size: object,
	gemini_take_profit: object,
	account_state: Dict,
	symbol: str,
	action: str,
) -> Optional[Tuple[float, Optional[float]]]:
	"""Resolve final lot size and take profit for the selected trading mode."""
	try:
		lot_size = float(gemini_lot_size)
	except (TypeError, ValueError):
		print("⚠️  Gemini lot_size missing/invalid in final decision")
		return None

	if lot_size <= 0:
		print(f"⚠️  Gemini lot_size invalid: {lot_size}")
		return None

	print(f"   Using Gemini lot_size: {lot_size}")

	if gemini_full_control_mode:
		try:
			take_profit = float(gemini_take_profit)
		except (TypeError, ValueError):
			print("⚠️  Gemini take_profit missing/invalid in FULL GEMINI mode")
			return None

		print(f"   Using Gemini take_profit: {take_profit}")
		return lot_size, take_profit

	take_profit = None
	print("   Standard mode active: take_profit disabled for this trade")

	has_margin, margin_msg = check_margin_requirements(symbol, action, lot_size)
	if not has_margin and "insufficient margin" in margin_msg.lower():
		print("⚠️  Prediction lot_size failed margin check in STANDARD mode")
		return None

	return lot_size, take_profit


def make_final_trading_decision(predictions_folder: Path, service_folder: Path) -> bool:
	"""Make a final trading decision and execute a trade with limited retries."""
	print("\n" + "=" * 60)
	print("🎯 Final Trading Decision Phase")
	print("=" * 60)

	try:
		print("\n📊 Loading remaining predictions...")
		predictions = load_predictions(predictions_folder)
		print(f"   Found {len(predictions)} strong predictions (BUY/SELL >= 35%)")
		if not predictions:
			print("⚠️  No strong predictions available, skipping final decision")
			return False

		account_state = get_account_state(include_margin_percent=True)
		_print_account_state(account_state)

		open_positions = get_open_positions()
		_print_open_positions(open_positions)

		api_key, api_url = _load_gemini_api_config()

		max_retries = 3
		excluded_symbols: List[str] = []
		full_control_every_n = _get_gemini_full_control_every_n_trades()
		successful_trades = count_successful_trades(service_folder)
		next_trade_number = successful_trades + 1
		gemini_full_control_mode = (next_trade_number % full_control_every_n) == 0

		_print_trade_mode(successful_trades, next_trade_number, full_control_every_n, gemini_full_control_mode)

		for attempt in range(max_retries):
			print(f"\n🔄 Decision attempt {attempt + 1}/{max_retries}")
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

			_save_decision_text(service_folder, decision_text)

			parsed = _parse_decision(decision_text)
			if parsed is None:
				return False

			symbol, action, gemini_lot_size, gemini_take_profit = parsed
			is_valid, error_msg = validate_symbol(symbol)
			if not is_valid:
				if not _handle_invalid_symbol(symbol, error_msg, predictions, excluded_symbols):
					return False
				continue

			resolved = _resolve_trade_parameters(
				gemini_full_control_mode=gemini_full_control_mode,
				gemini_lot_size=gemini_lot_size,
				gemini_take_profit=gemini_take_profit,
				account_state=account_state,
				symbol=symbol,
				action=action,
			)
			if resolved is None:
				return False

			lot_size, take_profit = resolved
			if lot_size <= 0:
				print(f"⚠️  Final lot size is {lot_size}, skipping trade execution")
				return False

			if execute_trade(symbol, action, lot_size, service_folder, take_profit, lot_source="gemini_prediction"):
				print("\n🎉 Trade executed successfully!")
				print("\n" + "=" * 60)
				print("✅ Final Trading Decision Completed")
				print("=" * 60)
				return True

			print("\n⚠️  Trade execution failed")
			return False

		print(f"❌ Exhausted all {max_retries} retry attempts")
		return False

	except Exception as exc:
		print(f"❌ Error in final decision phase: {exc}")
		import traceback

		traceback.print_exc()
		return False
