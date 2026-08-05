"""Shared trade execution helpers."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import MetaTrader5 as mt5

from mt5_symbols import get_symbol_tick
from trading_validation import check_margin_requirements, validate_lot_size, validate_symbol


TRADE_LOG_HEADERS = [
	"timestamp",
	"strategy_id",
	"magic",
	"comment",
	"symbol",
	"action",
	"lot_size",
	"lot_source",
	"price",
	"take_profit",
	"success",
	"error_msg",
]


@dataclass(frozen=True)
class TradeExecutionResult:
	"""Broker execution details for callers that need to reconcile an outcome."""

	success: bool
	deal_ticket: Optional[int] = None
	order_ticket: Optional[int] = None
	position_ticket: Optional[int] = None
	filled_price: Optional[float] = None
	filled_volume: Optional[float] = None
	error_message: str = ""


def close_position_by_ticket(
	position_ticket: int,
	symbol: str,
	position_type: int,
	volume: float,
	*,
	comment: str = "Close position",
	magic: int = 234000,
) -> bool:
	"""Close an existing MT5 position by sending the opposite market order."""
	tick = get_symbol_tick(symbol)
	if tick is None:
		print(f"❌ Failed to get tick for closing {symbol}: {mt5.last_error()}")
		return False

	if position_type == mt5.POSITION_TYPE_BUY:
		close_type = mt5.ORDER_TYPE_SELL
		price = tick.bid
	elif position_type == mt5.POSITION_TYPE_SELL:
		close_type = mt5.ORDER_TYPE_BUY
		price = tick.ask
	else:
		print(f"❌ Unsupported position type for ticket {position_ticket}: {position_type}")
		return False

	request = {
		"action": mt5.TRADE_ACTION_DEAL,
		"symbol": symbol,
		"volume": volume,
		"type": close_type,
		"position": position_ticket,
		"price": price,
		"deviation": 20,
		"magic": magic,
		"comment": comment,
		"type_time": mt5.ORDER_TIME_GTC,
		"type_filling": mt5.ORDER_FILLING_IOC,
	}

	result = mt5.order_send(request)
	if result is None:
		print(f"❌ Position close failed for ticket {position_ticket}: {mt5.last_error()}")
		return False

	if result.retcode != mt5.TRADE_RETCODE_DONE:
		print(
			f"❌ Position close failed for ticket {position_ticket}: "
			f"{result.retcode} - {result.comment}"
		)
		return False

	print(
		f"✅ Position closed: ticket={position_ticket}, symbol={symbol}, "
		f"volume={volume}, price={result.price}"
	)
	return True


def _ensure_trade_log_schema(log_file: Path) -> None:
	"""Ensure trade log CSV contains the current headers, migrating legacy files if needed."""
	if not log_file.exists():
		return

	with open(log_file, "r", newline="", encoding="utf-8") as f:
		reader = csv.reader(f)
		rows = list(reader)

	if not rows:
		return

	headers = rows[0]
	if headers == TRADE_LOG_HEADERS:
		return

	data_rows: List[List[str]] = []
	for row in rows[1:]:
		if not row:
			continue

		if len(headers) == 7:
			mapped = dict(zip(headers, row))
			data_rows.append(
				[
					mapped.get("timestamp", ""),
					mapped.get("symbol", ""),
					mapped.get("action", ""),
					mapped.get("lot_size", ""),
					"legacy_unknown",
					mapped.get("price", ""),
					mapped.get("success", ""),
					mapped.get("error_msg", ""),
				]
			)
			continue

		padded = row[: len(TRADE_LOG_HEADERS)] + [""] * max(0, len(TRADE_LOG_HEADERS) - len(row))
		data_rows.append(padded[: len(TRADE_LOG_HEADERS)])

	with open(log_file, "w", newline="", encoding="utf-8") as f:
		writer = csv.writer(f)
		writer.writerow(TRADE_LOG_HEADERS)
		writer.writerows(data_rows)


def log_trade(
	strategy_id: str,
	magic: int,
	comment: str,
	symbol: str,
	action: str,
	lot_size: float,
	lot_source: str,
	price: float,
	take_profit: Optional[float],
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
	_ensure_trade_log_schema(log_file)
	file_exists = log_file.exists()

	with open(log_file, "a", newline="", encoding="utf-8") as f:
		writer = csv.writer(f)

		if not file_exists:
			writer.writerow(TRADE_LOG_HEADERS)

		writer.writerow(
			[
				datetime.now(tz=timezone.utc).isoformat(),
				strategy_id,
				magic,
				comment,
				symbol,
				action,
				lot_size,
				lot_source,
				price,
				take_profit if take_profit is not None else "",
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
	lot_source: str = "unknown",
	strategy_id: str = "gemini_primary",
	magic: int = 234000,
	comment: Optional[str] = None,
	extra_log_data: Optional[dict] = None,
	return_execution_result: bool = False,
) -> bool | TradeExecutionResult:
	"""Execute a trade on MT5 with validation and logging."""
	def _result(success: bool, error_message: str = "", **details: object) -> bool | TradeExecutionResult:
		if not return_execution_result:
			return success
		return TradeExecutionResult(success=success, error_message=error_message, **details)

	print(f"\n🔄 Executing trade...")
	print(f"   Symbol: {symbol}")
	print(f"   Action: {action}")
	print(f"   Requested Lot Size: {lot_size}")
	print(f"   Take Profit: {take_profit if take_profit is not None else 'None'}")

	is_valid, error_msg = validate_symbol(symbol)
	if not is_valid:
		print(f"❌ Symbol validation failed: {error_msg}")
		log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, 0.0, take_profit, False, error_msg, service_folder)
		return _result(False, error_msg)

	adjusted_lot, lot_msg = validate_lot_size(symbol, lot_size)
	if adjusted_lot == 0.0:
		print(f"❌ Lot size validation failed: {lot_msg}")
		log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, 0.0, take_profit, False, lot_msg, service_folder)
		return _result(False, lot_msg)

	if lot_msg:
		print(f"   ⚠️  {lot_msg}")

	lot_size = adjusted_lot
	print(f"   Final Lot Size: {lot_size}")
	print(f"   Lot Source: {lot_source}")

	has_margin, margin_msg = check_margin_requirements(symbol, action, lot_size)
	if not has_margin:
		print(f"❌ Margin check failed: {margin_msg}")
		log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, 0.0, take_profit, False, margin_msg, service_folder)
		return _result(False, margin_msg)

	tick = get_symbol_tick(symbol)
	if tick is None:
		error_msg = f"Failed to get tick for {symbol}: {mt5.last_error()}"
		print(f"❌ {error_msg}")
		log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, 0.0, take_profit, False, error_msg, service_folder)
		return _result(False, error_msg)

	if action == "BUY":
		order_type = mt5.ORDER_TYPE_BUY
		price = tick.ask
	elif action == "SELL":
		order_type = mt5.ORDER_TYPE_SELL
		price = tick.bid
	else:
		error_msg = f"Invalid action: {action} (must be BUY or SELL)"
		print(f"❌ {error_msg}")
		log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, 0.0, take_profit, False, error_msg, service_folder)
		return _result(False, error_msg)

	print(f"   Price: {price}")

	validated_tp: Optional[float] = None
	if take_profit is not None:
		try:
			validated_tp = float(take_profit)
		except (TypeError, ValueError):
			error_msg = f"Invalid take_profit value: {take_profit}"
			print(f"❌ {error_msg}")
			log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, price, take_profit, False, error_msg, service_folder)
			return _result(False, error_msg)

		if validated_tp <= 0:
			error_msg = f"Invalid take_profit <= 0: {validated_tp}"
			print(f"❌ {error_msg}")
			log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, price, take_profit, False, error_msg, service_folder)
			return _result(False, error_msg)

		if action == "BUY" and validated_tp <= price:
			error_msg = f"Invalid take_profit for BUY: TP ({validated_tp}) must be > market price ({price})"
			print(f"❌ {error_msg}")
			log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, price, take_profit, False, error_msg, service_folder)
			return _result(False, error_msg)

		if action == "SELL" and validated_tp >= price:
			error_msg = f"Invalid take_profit for SELL: TP ({validated_tp}) must be < market price ({price})"
			print(f"❌ {error_msg}")
			log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, price, take_profit, False, error_msg, service_folder)
			return _result(False, error_msg)

		print(f"   Validated TP: {validated_tp}")

	request = {
		"action": mt5.TRADE_ACTION_DEAL,
		"symbol": symbol,
		"volume": lot_size,
		"type": order_type,
		"price": price,
		"deviation": 20,
		"magic": magic,
		"comment": comment or f"ga:{strategy_id}",
		"type_time": mt5.ORDER_TIME_GTC,
		"type_filling": mt5.ORDER_FILLING_IOC,
	}

	if validated_tp is not None:
		request["tp"] = validated_tp

	result = mt5.order_send(request)
	if result is None:
		error_msg = f"Order send failed: {mt5.last_error()}"
		print(f"❌ {error_msg}")
		log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, price, take_profit, False, error_msg, service_folder)
		return _result(False, error_msg)

	if result.retcode != mt5.TRADE_RETCODE_DONE:
		error_msg = f"Order failed with retcode: {result.retcode} - {result.comment}"
		print(f"❌ {error_msg}")
		log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, price, take_profit, False, error_msg, service_folder)
		return _result(False, error_msg)

	print(f"✅ Trade executed successfully!")
	print(f"   Order: {result.order}")
	print(f"   Volume: {result.volume}")
	print(f"   Price: {result.price}")

	log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, result.price, take_profit, True, "", service_folder)
	if service_folder is not None and extra_log_data:
		log_path = service_folder / "trade_logs" / "execution_log.jsonl"
		log_path.parent.mkdir(parents=True, exist_ok=True)
		payload = {
			"timestamp": datetime.now(tz=timezone.utc).isoformat(),
			"strategy_id": strategy_id,
			"magic": magic,
			"symbol": symbol,
			"action": action,
			"lot_size": lot_size,
			"take_profit": take_profit,
			"result_order": getattr(result, "order", None),
			"result_price": getattr(result, "price", None),
			"result_volume": getattr(result, "volume", None),
			**extra_log_data,
		}
		with open(log_path, "a", encoding="utf-8") as handle:
			handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
	return _result(
		True,
		deal_ticket=int(getattr(result, "deal", 0) or 0) or None,
		order_ticket=int(getattr(result, "order", 0) or 0) or None,
		# MT5 uses the opening order ticket as the position id in the supported hedging account mode.
		position_ticket=int(getattr(result, "order", 0) or 0) or None,
		filled_price=float(getattr(result, "price", 0.0) or 0.0),
		filled_volume=float(getattr(result, "volume", 0.0) or 0.0),
	)
