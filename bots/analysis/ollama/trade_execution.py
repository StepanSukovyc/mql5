"""Shared trade execution helpers."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import MetaTrader5 as mt5

from mt5_symbols import get_symbol_info, get_symbol_tick
from swap_rollover import get_prague_entry_block_end_utc
from trading_validation import get_margin_requirement, validate_lot_size, validate_symbol


MAX_MARGIN_RETRIES = 6
MARGIN_SAFETY_FACTOR = 0.95


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
	margin_retry_count: int = 0
	error_message: str = ""


def get_max_open_positions() -> int:
	"""Return the account-wide position cap, defaulting to 17."""
	raw = os.getenv("MT5_MAX_OPEN_POSITIONS", "17")
	try:
		value = int(raw)
		return value if value > 0 else 17
	except (TypeError, ValueError):
		return 17


def _check_open_position_limit() -> tuple[bool, str]:
	"""Allow a new entry only when the account-wide position count is below the cap."""
	positions = mt5.positions_get()
	if positions is None:
		return False, f"Failed to verify open position limit: {mt5.last_error()}"

	max_open_positions = get_max_open_positions()
	open_position_count = len(positions)
	if open_position_count >= max_open_positions:
		return False, (
			f"Open position limit reached: {open_position_count}/{max_open_positions} "
			"(MT5_MAX_OPEN_POSITIONS)"
		)
	return True, ""


def _next_margin_sized_lot(
	symbol: str,
	current_lot: float,
	required_margin: float,
	available_margin: float,
	*,
	broker_rejected: bool = False,
) -> Optional[float]:
	"""Return a strictly smaller broker-valid volume sized for available margin."""
	symbol_info = get_symbol_info(symbol)
	if symbol_info is None or required_margin <= 0 or available_margin <= 0:
		return None

	minimum = float(symbol_info.volume_min)
	step = float(symbol_info.volume_step)
	if minimum <= 0 or step <= 0:
		return None

	scale = (available_margin / required_margin) * MARGIN_SAFETY_FACTOR
	if broker_rejected:
		scale = min(scale, 0.5)
	scale = min(scale, MARGIN_SAFETY_FACTOR)
	target = current_lot * scale
	minimum_decimal = Decimal(str(minimum))
	step_decimal = Decimal(str(step))
	current_decimal = Decimal(str(current_lot))
	target_decimal = Decimal(str(target))
	if target_decimal < minimum_decimal:
		return None
	target_steps = int(((target_decimal - minimum_decimal) / step_decimal).to_integral_value(rounding=ROUND_FLOOR))
	max_strict_steps = int(
		((current_decimal - minimum_decimal) / step_decimal).to_integral_value(rounding=ROUND_CEILING)
	) - 1
	steps = min(target_steps, max_strict_steps)
	next_lot = minimum_decimal + (steps * step_decimal)
	if steps < 0 or next_lot >= current_decimal:
		return None
	return float(next_lot)


def _log_margin_retry(
	service_folder: Optional[Path],
	*,
	symbol: str,
	action: str,
	attempt: int,
	previous_lot: float,
	next_lot: float,
	required_margin: float,
	available_margin: float,
	reason: str,
) -> None:
	if service_folder is None:
		return
	log_path = service_folder / "trade_logs" / "execution_log.jsonl"
	log_path.parent.mkdir(parents=True, exist_ok=True)
	payload = {
		"timestamp": datetime.now(tz=timezone.utc).isoformat(),
		"event": "margin_sizing_retry",
		"symbol": symbol,
		"action": action,
		"attempt": attempt,
		"previous_lot": previous_lot,
		"next_lot": next_lot,
		"required_margin": required_margin,
		"available_margin": available_margin,
		"reason": reason,
	}
	with open(log_path, "a", encoding="utf-8") as handle:
		handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


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
	requested_lot_size = float(lot_size)

	entry_block_end = get_prague_entry_block_end_utc()
	if entry_block_end is not None:
		error_msg = (
			"NEW_ENTRY_BLOCKED_PRAGUE_EVENING_WINDOW: no new positions from 21:50 "
			f"until 00:00 Europe/Prague (resumes at {entry_block_end.isoformat()})"
		)
		print(f"⏸️  Trade blocked: {error_msg}")
		log_trade(
			strategy_id,
			magic,
			comment or strategy_id,
			symbol,
			action,
			lot_size,
			lot_source,
			0.0,
			take_profit,
			False,
			error_msg,
			service_folder,
		)
		return _result(False, error_msg)

	within_position_limit, position_limit_error = _check_open_position_limit()
	if not within_position_limit:
		print(f"❌ Trade blocked: {position_limit_error}")
		log_trade(
			strategy_id,
			magic,
			comment or strategy_id,
			symbol,
			action,
			lot_size,
			lot_source,
			0.0,
			take_profit,
			False,
			position_limit_error,
			service_folder,
		)
		return _result(False, position_limit_error)

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

	margin_retry_count = 0
	last_margin_error = ""
	while True:
		margin = get_margin_requirement(symbol, action, lot_size)
		if margin.error or margin.required is None or margin.available is None:
			last_margin_error = margin.error or "Margin data unavailable"
			break
		if margin.required <= margin.available:
			if margin.raw_available is not None and margin.available != margin.raw_available:
				print(
					f"   ✅ Margin check passed: required {margin.required:.2f}, "
					f"available {margin.available:.2f} (strategy cap applied, raw {margin.raw_available:.2f})"
				)
			else:
				print(
					f"   ✅ Margin check passed: required {margin.required:.2f}, "
					f"available {margin.available:.2f}"
				)
			break

		last_margin_error = (
			f"Insufficient margin: required {margin.required:.2f}, available {margin.available:.2f}"
		)
		if margin.raw_available is not None and margin.available != margin.raw_available:
			last_margin_error += f" (strategy cap applied, raw free margin {margin.raw_available:.2f})"
		if margin_retry_count >= MAX_MARGIN_RETRIES:
			break

		smaller_lot = _next_margin_sized_lot(
			symbol,
			lot_size,
			margin.required,
			margin.available,
		)
		if smaller_lot is None:
			last_margin_error += "; no smaller broker-valid volume can be placed"
			break

		margin_retry_count += 1
		print(
			f"⚠️  Insufficient margin; retrying with smaller volume "
			f"{lot_size:g} -> {smaller_lot:g} "
			f"({margin_retry_count}/{MAX_MARGIN_RETRIES})"
		)
		_log_margin_retry(
			service_folder,
			symbol=symbol,
			action=action,
			attempt=margin_retry_count,
			previous_lot=lot_size,
			next_lot=smaller_lot,
			required_margin=margin.required,
			available_margin=margin.available,
			reason="preflight_insufficient_margin",
		)
		lot_size = smaller_lot

	if last_margin_error and (
		margin.error
		or margin.required is None
		or margin.available is None
		or margin.required > margin.available
	):
		print(f"❌ Margin check failed: {last_margin_error}")
		log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, 0.0, take_profit, False, last_margin_error, service_folder)
		return _result(False, last_margin_error)

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

	# This applies only to new entries in this function; close_position_by_ticket remains untouched.
	try:
		from economic_calendar import EconomicCalendarService

		event_risk = EconomicCalendarService(service_folder).evaluate(symbol)
		if not event_risk.allow_new_trade:
			error_msg = "ECONOMIC_EVENT_BLOCKED_AT_EXECUTION"
			print(f"❌ Trade blocked: {error_msg}")
			log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, price, take_profit, False, error_msg, service_folder)
			return _result(False, error_msg)
	except Exception as exc:
		# Calendar faults never break the legacy execution path; configured fail mode is applied by the service.
		print(f"⚠️ Economic calendar execution recheck unavailable: {exc}")

	entry_block_end = get_prague_entry_block_end_utc()
	if entry_block_end is not None:
		error_msg = (
			"NEW_ENTRY_BLOCKED_PRAGUE_EVENING_WINDOW: no new positions from 21:50 "
			f"until 00:00 Europe/Prague (resumes at {entry_block_end.isoformat()})"
		)
		print(f"⏸️  Trade blocked: {error_msg}")
		log_trade(
			strategy_id,
			magic,
			comment or strategy_id,
			symbol,
			action,
			lot_size,
			lot_source,
			price,
			take_profit,
			False,
			error_msg,
			service_folder,
		)
		return _result(False, error_msg)

	result = None
	while True:
		request["volume"] = lot_size
		result = mt5.order_send(request)
		if result is None:
			error_msg = f"Order send failed: {mt5.last_error()}"
			print(f"❌ {error_msg}")
			log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, price, take_profit, False, error_msg, service_folder)
			return _result(False, error_msg)

		if result.retcode == mt5.TRADE_RETCODE_DONE:
			break

		error_msg = f"Order failed with retcode: {result.retcode} - {result.comment}"
		no_money_retcode = getattr(mt5, "TRADE_RETCODE_NO_MONEY", 10019)
		if result.retcode != no_money_retcode or margin_retry_count >= MAX_MARGIN_RETRIES:
			print(f"❌ {error_msg}")
			log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, price, take_profit, False, error_msg, service_folder)
			return _result(False, error_msg)

		margin = get_margin_requirement(symbol, action, lot_size)
		if margin.error or margin.required is None or margin.available is None:
			error_msg = f"{error_msg}; unable to recalculate margin for retry: {margin.error or 'missing margin data'}"
			print(f"❌ {error_msg}")
			log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, price, take_profit, False, error_msg, service_folder)
			return _result(False, error_msg)
		smaller_lot = _next_margin_sized_lot(
			symbol,
			lot_size,
			margin.required,
			margin.available,
			broker_rejected=True,
		)
		if smaller_lot is None:
			error_msg = f"{error_msg}; no smaller broker-valid volume can be placed"
			print(f"❌ {error_msg}")
			log_trade(strategy_id, magic, comment or strategy_id, symbol, action, lot_size, lot_source, price, take_profit, False, error_msg, service_folder)
			return _result(False, error_msg)

		margin_retry_count += 1
		print(
			f"⚠️  Broker rejected order for margin; retrying with smaller volume "
			f"{lot_size:g} -> {smaller_lot:g} "
			f"({margin_retry_count}/{MAX_MARGIN_RETRIES})"
		)
		_log_margin_retry(
			service_folder,
			symbol=symbol,
			action=action,
			attempt=margin_retry_count,
			previous_lot=lot_size,
			next_lot=smaller_lot,
			required_margin=margin.required,
			available_margin=margin.available,
			reason="broker_insufficient_margin",
		)
		lot_size = smaller_lot

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
			"requested_volume": requested_lot_size,
			"margin_retry_count": margin_retry_count,
			**extra_log_data,
		}
		with open(log_path, "a", encoding="utf-8") as handle:
			handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
	position_ticket = int(getattr(result, "order", 0) or 0) or None
	filled_volume = float(getattr(result, "volume", 0.0) or 0.0) or lot_size
	filled_price = float(getattr(result, "price", 0.0) or 0.0) or price
	if service_folder is not None and position_ticket is not None:
		try:
			from trade_outcomes import register_open_trade

			initial_risk = None
			if extra_log_data and extra_log_data.get("risk_usd") is not None:
				initial_risk = float(extra_log_data["risk_usd"]) * (filled_volume / requested_lot_size)
			register_open_trade(
				service_folder,
				position_id=position_ticket,
				strategy_id=strategy_id,
				symbol=symbol,
				action=action,
				entry_time_utc=datetime.now(tz=timezone.utc),
				entry_volume=filled_volume,
				entry_price=filled_price,
				initial_risk_usd=initial_risk,
			)
		except (OSError, RuntimeError, TypeError, ValueError) as exc:
			error = f"Could not register opened position for outcome tracking: {exc}"
			print(f"⚠️  {error}")
			if service_folder is not None:
				log_path = service_folder / "trade_logs" / "execution_log.jsonl"
				log_path.parent.mkdir(parents=True, exist_ok=True)
				with open(log_path, "a", encoding="utf-8") as handle:
					handle.write(
						json.dumps(
							{
								"timestamp": datetime.now(tz=timezone.utc).isoformat(),
								"event": "trade_outcome_registration_failed",
								"position_id": position_ticket,
								"strategy_id": strategy_id,
								"symbol": symbol,
								"error": str(exc),
							},
							ensure_ascii=False,
						)
						+ "\n"
					)
	return _result(
		True,
		deal_ticket=int(getattr(result, "deal", 0) or 0) or None,
		order_ticket=int(getattr(result, "order", 0) or 0) or None,
		# MT5 uses the opening order ticket as the position id in the supported hedging account mode.
		position_ticket=position_ticket,
		filled_price=filled_price,
		filled_volume=filled_volume,
		margin_retry_count=margin_retry_count,
	)
