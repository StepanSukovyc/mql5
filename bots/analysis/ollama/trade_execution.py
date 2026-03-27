"""Shared trade execution helpers."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import MetaTrader5 as mt5

from mt5_symbols import get_symbol_tick
from trading_validation import check_margin_requirements, validate_lot_size, validate_symbol


def log_trade(
	symbol: str,
	action: str,
	lot_size: float,
	price: float,
	success: bool,
	error_msg: str = "",
	service_folder: Path = None,
) -> None:
	"""Log trade execution to the service trade log CSV."""
	if service_folder is None:
		return

	logs_folder = service_folder / "trade_logs"
	logs_folder.mkdir(parents=True, exist_ok=True)

	log_file = logs_folder / "trades.csv"
	file_exists = log_file.exists()

	with open(log_file, "a", newline="", encoding="utf-8") as f:
		writer = csv.writer(f)

		if not file_exists:
			writer.writerow(["timestamp", "symbol", "action", "lot_size", "price", "success", "error_msg"])

		writer.writerow(
			[
				datetime.now(tz=timezone.utc).isoformat(),
				symbol,
				action,
				lot_size,
				price,
				success,
				error_msg,
			]
		)

	print(f"📝 Trade logged to: {log_file}")


def execute_trade(
	symbol: str,
	action: str,
	lot_size: float,
	service_folder: Path = None,
	take_profit: Optional[float] = None,
) -> bool:
	"""Execute a trade on MT5 with validation and logging."""
	print(f"\n🔄 Executing trade...")
	print(f"   Symbol: {symbol}")
	print(f"   Action: {action}")
	print(f"   Requested Lot Size: {lot_size}")
	print(f"   Take Profit: {take_profit if take_profit is not None else 'None'}")

	is_valid, error_msg = validate_symbol(symbol)
	if not is_valid:
		print(f"❌ Symbol validation failed: {error_msg}")
		log_trade(symbol, action, lot_size, 0.0, False, error_msg, service_folder)
		return False

	adjusted_lot, lot_msg = validate_lot_size(symbol, lot_size)
	if adjusted_lot == 0.0:
		print(f"❌ Lot size validation failed: {lot_msg}")
		log_trade(symbol, action, lot_size, 0.0, False, lot_msg, service_folder)
		return False

	if lot_msg:
		print(f"   ⚠️  {lot_msg}")

	lot_size = adjusted_lot
	print(f"   Final Lot Size: {lot_size}")

	has_margin, margin_msg = check_margin_requirements(symbol, action, lot_size)
	if not has_margin:
		print(f"❌ Margin check failed: {margin_msg}")
		log_trade(symbol, action, lot_size, 0.0, False, margin_msg, service_folder)
		return False

	tick = get_symbol_tick(symbol)
	if tick is None:
		error_msg = f"Failed to get tick for {symbol}: {mt5.last_error()}"
		print(f"❌ {error_msg}")
		log_trade(symbol, action, lot_size, 0.0, False, error_msg, service_folder)
		return False

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