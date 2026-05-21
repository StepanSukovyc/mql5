"""Final trading decision orchestration."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from account_state import get_account_state
from gemini_config import GeminiVertexConfig, load_gemini_api_config
from gemini_decision import ask_gemini_final_decision, load_predictions
from instrument_utils import (
	get_base_prediction_threshold,
	get_cfd_min_net_profit_usd,
	get_cfd_tp_max_distance_percent,
	get_crypto_lot_multiplier,
	get_crypto_max_open_positions,
	get_crypto_prediction_threshold,
	get_crypto_tp_distance_percent,
	is_cfd_full_tp_mode_allowed,
	is_cfd_symbol,
	is_crypto_full_tp_mode_allowed,
	is_crypto_symbol,
)
from news_filter import should_block_symbol_for_news
from mt5_symbols import estimate_order_profit, get_current_price, get_symbol_info
from mt5_positions import get_open_positions
from trade_execution import execute_trade
from trade_history import count_successful_trades, count_successful_trades_today
from trading_validation import check_margin_requirements, validate_symbol
from strategy_profile import StrategyProfile, get_primary_strategy_profile, is_strategy_session_open


if hasattr(sys.stdout, "reconfigure"):
	try:
		sys.stdout.reconfigure(encoding="utf-8", errors="replace")
	except Exception:
		pass


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


def _load_gemini_api_config() -> GeminiVertexConfig:
	"""Load Gemini Vertex AI config from environment."""
	return load_gemini_api_config()


def _print_trade_mode(successful_trades: int, next_trade_number: int, full_control_every_n: int, gemini_full_control_mode: bool) -> None:
	"""Print the current trade execution mode."""
	print("\n🧭 Trade Execution Mode")
	print(f"   Successful trades so far: {successful_trades}")
	print(f"   Current trade number: #{next_trade_number}")
	print(f"   Gemini take_profit control every N trades: N={full_control_every_n}")
	print(
		f"   Crypto safeguards: min signal {get_crypto_prediction_threshold():.0f}%, "
		f"lot x{get_crypto_lot_multiplier():.2f}, max open positions {get_crypto_max_open_positions()}, "
		f"TP distance {get_crypto_tp_distance_percent():.2f}%"
	)
	print(
		f"   CFD safeguards: min net ${get_cfd_min_net_profit_usd():.2f} after modeled fee, "
		f"TP max distance {get_cfd_tp_max_distance_percent():.2f}%"
	)
	print(
		"   Mode: "
		+ (
			"FULL GEMINI TP MODE (lot_size + take_profit from Gemini)"
			if gemini_full_control_mode
			else "PREDICTION LOT MODE (lot_size from Gemini, no take_profit)"
		)
	)


def _count_open_crypto_positions(open_positions: List[Dict]) -> int:
	"""Count open positions belonging to the crypto instrument group."""
	return len([pos for pos in open_positions if is_crypto_symbol(str(pos.get("symbol", "")))])


def _position_belongs_to_profile(position: Dict, profile: StrategyProfile) -> bool:
	"""Return True when the serialized open position belongs to the strategy profile."""
	if int(position.get("magic", 0) or 0) == profile.magic:
		return True
	comment = str(position.get("comment", "") or "")
	return profile.strategy_id in comment


def _get_profile_open_positions(open_positions: List[Dict], profile: StrategyProfile) -> List[Dict]:
	"""Return open positions belonging to the current strategy profile."""
	return [position for position in open_positions if _position_belongs_to_profile(position, profile)]


def _get_prediction_market_context(prediction: Dict) -> Dict[str, object]:
	"""Return embedded market context for a prediction when available."""
	context = prediction.get("market_context")
	return context if isinstance(context, dict) else {}


def _is_limit_enabled(limit: int) -> bool:
	"""Return True when a non-zero configured limit should be enforced."""
	return limit > 0


def _apply_balance_based_lot_cap(
	lot_size: float,
	*,
	account_state: Dict,
	profile: StrategyProfile,
	owned_open_positions: int,
) -> float:
	"""Cap lot size using a balance-based guardrail when no stop-loss is configured."""
	balance = float(account_state.get("balance", 0.0) or 0.0)
	steps = max(int(balance / profile.balance_step_usd), 0) + 1
	max_lot = min(profile.max_lot_cap, steps * profile.lot_per_balance_step)
	if _is_limit_enabled(profile.max_open_positions) and owned_open_positions >= max(profile.max_open_positions - 1, 1):
		max_lot = min(max_lot, profile.lot_per_balance_step)
	if lot_size > max_lot:
		print(f"   Balance-based lot cap applied: {lot_size} -> {max_lot}")
		return max_lot
	return lot_size


def _resolve_deterministic_take_profit(
	*,
	symbol: str,
	action: str,
	lot_size: float,
	market_context: Dict[str, object],
	symbol_is_crypto: bool,
	symbol_is_cfd: bool,
) -> Optional[float]:
	"""Build deterministic TP from ATR and regime while keeping existing instrument safeguards."""
	current_price = get_current_price(symbol, action=action)
	if current_price is None:
		print(f"⚠️  Cannot resolve current price for deterministic TP on {symbol}")
		return None

	try:
		atr_h1 = float(market_context.get("atr_h1_value"))
	except (TypeError, ValueError):
		return None

	strong_trend = bool(market_context.get("strong_trend", False))
	multiplier = 2.8 if strong_trend else 2.2
	tp_distance = atr_h1 * multiplier
	requested_tp = current_price + tp_distance if action == "BUY" else current_price - tp_distance

	if symbol_is_crypto:
		return _resolve_crypto_take_profit(symbol, action, requested_tp)
	if symbol_is_cfd:
		return _resolve_cfd_take_profit(symbol, action, lot_size, requested_tp)
	return _round_price_for_symbol(symbol, requested_tp)


def _get_modeled_trade_fee(lot_size: float) -> float:
	"""Model per-trade fee using the same convention as the cleanup strategies."""
	return round((float(lot_size) / 0.01) * 0.10, 2)


def _round_price_for_symbol(symbol: str, price: float) -> float:
	"""Round a price to the symbol precision when available."""
	symbol_info = get_symbol_info(symbol)
	digits = getattr(symbol_info, "digits", None) if symbol_info is not None else None
	if isinstance(digits, int) and digits >= 0:
		return round(price, digits)
	return price


def _resolve_crypto_take_profit(symbol: str, action: str, gemini_take_profit: object) -> Optional[float]:
	"""Resolve a conservative crypto take-profit near the current market price."""
	current_price = get_current_price(symbol, action=action)
	if current_price is None:
		print(f"⚠️  Cannot resolve current price for crypto TP on {symbol}")
		return None

	tp_distance_percent = get_crypto_tp_distance_percent()
	tp_distance_ratio = tp_distance_percent / 100.0
	if action == "BUY":
		fallback_tp = current_price * (1.0 + tp_distance_ratio)
	else:
		fallback_tp = current_price * (1.0 - tp_distance_ratio)

	try:
		requested_tp = float(gemini_take_profit)
	except (TypeError, ValueError):
		requested_tp = fallback_tp

	if action == "BUY":
		if requested_tp <= current_price:
			requested_tp = fallback_tp
		resolved_tp = min(requested_tp, fallback_tp)
	else:
		if requested_tp >= current_price:
			requested_tp = fallback_tp
		resolved_tp = max(requested_tp, fallback_tp)

	print(
		f"   Crypto TP resolved from current price {current_price:.6f} "
		f"with max distance {tp_distance_percent:.2f}% -> {resolved_tp:.6f}"
	)
	return resolved_tp


def _resolve_cfd_take_profit(symbol: str, action: str, lot_size: float, gemini_take_profit: object) -> Optional[float]:
	"""Resolve a fee-aware CFD take-profit or disable it when no safe target exists."""
	current_price = get_current_price(symbol, action=action)
	if current_price is None:
		print(f"⚠️  Cannot resolve current price for CFD TP on {symbol}")
		return None

	direction = 1.0 if action == "BUY" else -1.0
	max_distance_percent = get_cfd_tp_max_distance_percent()
	max_distance_ratio = max_distance_percent / 100.0
	modeled_fee = _get_modeled_trade_fee(lot_size)
	required_profit = modeled_fee + get_cfd_min_net_profit_usd()

	def _candidate_profit(candidate_tp: float) -> Optional[float]:
		return estimate_order_profit(symbol, action, lot_size, current_price, candidate_tp)

	def _is_directionally_valid(candidate_tp: float) -> bool:
		if action == "BUY":
			return candidate_tp > current_price
		return candidate_tp < current_price

	def _price_from_ratio(distance_ratio: float) -> float:
		return current_price * (1.0 + (direction * distance_ratio))

	try:
		requested_tp = float(gemini_take_profit)
	except (TypeError, ValueError):
		requested_tp = None

	max_tp = _price_from_ratio(max_distance_ratio)
	max_profit = _candidate_profit(max_tp)
	if max_profit is None:
		print(f"⚠️  Failed to estimate CFD TP profit for {symbol}")
		return None

	if requested_tp is not None and _is_directionally_valid(requested_tp):
		requested_profit = _candidate_profit(requested_tp)
		if requested_profit is not None and requested_profit >= required_profit:
			requested_distance_ratio = abs((requested_tp - current_price) / current_price)
			if requested_distance_ratio <= max_distance_ratio:
				print(
					f"   Gemini TP for CFD already covers modeled fee {modeled_fee:.2f} and min net target -> {requested_tp}"
				)
				return _round_price_for_symbol(symbol, requested_tp)
			print(
				f"   CFD TP from Gemini is too far for {symbol}; using closer fee-safe target within {max_distance_percent:.2f}%"
			)

	if max_profit < required_profit:
		print(
			f"⚠️  Safe CFD TP for {symbol} not found within {max_distance_percent:.2f}% distance; TP disabled "
			f"(max gross profit {max_profit:.2f}, required {required_profit:.2f})"
		)
		return None

	low_ratio = 0.0
	high_ratio = max_distance_ratio
	for _ in range(32):
		mid_ratio = (low_ratio + high_ratio) / 2.0
		candidate_tp = _price_from_ratio(mid_ratio)
		candidate_profit = _candidate_profit(candidate_tp)
		if candidate_profit is None:
			print(f"⚠️  Failed to estimate CFD TP profit while resolving {symbol}")
			return None
		if candidate_profit >= required_profit:
			high_ratio = mid_ratio
		else:
			low_ratio = mid_ratio

	resolved_tp = _round_price_for_symbol(symbol, _price_from_ratio(high_ratio))
	resolved_profit = _candidate_profit(resolved_tp)
	if resolved_profit is None:
		print(f"⚠️  Failed to validate resolved CFD TP for {symbol}")
		return None

	print(
		f"   CFD TP resolved from current price {current_price:.6f} with max distance {max_distance_percent:.2f}% "
		f"and required gross profit {required_profit:.2f} -> {resolved_tp:.6f} (estimated gross {resolved_profit:.2f})"
	)
	return resolved_tp


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


def _exclude_symbol_and_retry(symbol: str, reason: str, predictions: List[Dict], excluded_symbols: List[str]) -> bool:
	"""Exclude the current symbol and continue with a different prediction when possible."""
	print(f"⚠️  {reason}")
	if symbol not in excluded_symbols:
		print(f"   Adding {symbol} to exclusion list and retrying...")
		excluded_symbols.append(symbol)
	else:
		print(f"   {symbol} is already excluded, retrying without it...")

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
	profile: StrategyProfile | None = None,
	owned_open_positions: int = 0,
	symbol: str,
	action: str,
	symbol_is_crypto: bool,
	symbol_is_cfd: bool,
	market_context: Dict[str, object] | None = None,
) -> Optional[Tuple[float, Optional[float]]]:
	"""Resolve final lot size and take profit for the selected trading mode."""
	profile = profile or get_primary_strategy_profile()
	market_context = market_context or {}
	try:
		lot_size = float(gemini_lot_size)
	except (TypeError, ValueError):
		print("⚠️  Gemini lot_size missing/invalid in final decision")
		return None

	if lot_size <= 0:
		print(f"⚠️  Gemini lot_size invalid: {lot_size}")
		return None

	if symbol_is_crypto:
		crypto_multiplier = get_crypto_lot_multiplier()
		lot_size *= crypto_multiplier
		print(f"   Crypto lot multiplier applied ({crypto_multiplier:.2f}), adjusted lot_size: {lot_size}")

	lot_size = _apply_balance_based_lot_cap(
		lot_size,
		account_state=account_state,
		profile=profile,
		owned_open_positions=owned_open_positions,
	)

	print(f"   Using Gemini lot_size: {lot_size}")

	if market_context.get("atr_h1_value") is not None:
		take_profit = _resolve_deterministic_take_profit(
			symbol=symbol,
			action=action,
			lot_size=lot_size,
			market_context=market_context,
			symbol_is_crypto=symbol_is_crypto,
			symbol_is_cfd=symbol_is_cfd,
		)
		if take_profit is not None:
			print(f"   Using deterministic ATR take_profit: {take_profit}")
			return lot_size, take_profit

	if symbol_is_crypto and gemini_full_control_mode:
		take_profit = _resolve_crypto_take_profit(symbol, action, gemini_take_profit)
		if take_profit is None:
			return None

		print(f"   Using conservative crypto take_profit: {take_profit}")
		return lot_size, take_profit

	if symbol_is_cfd and gemini_full_control_mode:
		take_profit = _resolve_cfd_take_profit(symbol, action, lot_size, gemini_take_profit)
		if take_profit is None:
			print("   CFD symbol selected: take_profit disabled because no fee-safe target was found")
			return lot_size, None

		print(f"   Using fee-aware CFD take_profit: {take_profit}")
		return lot_size, take_profit

	if gemini_full_control_mode:
		try:
			take_profit = float(gemini_take_profit)
		except (TypeError, ValueError):
			print("⚠️  Gemini take_profit missing/invalid in FULL GEMINI mode")
			return None

		print(f"   Fallback to Gemini take_profit: {take_profit}")
		return lot_size, take_profit

	take_profit = None
	print("   Standard mode active: take_profit disabled for this trade")

	has_margin, margin_msg = check_margin_requirements(symbol, action, lot_size)
	if not has_margin and "insufficient margin" in margin_msg.lower():
		print("⚠️  Prediction lot_size failed margin check in STANDARD mode")
		return None

	return lot_size, take_profit


def make_final_trading_decision(
	predictions_folder: Path,
	service_folder: Path,
	*,
	strategy_profile: Optional[StrategyProfile] = None,
) -> bool:
	"""Make a final trading decision and execute a trade with limited retries."""
	print("\n" + "=" * 60)
	print("🎯 Final Trading Decision Phase")
	print("=" * 60)
	profile = strategy_profile or get_primary_strategy_profile()
	if not is_strategy_session_open(profile):
		print(f"⏸️  Session closed for strategy profile {profile.label}, skipping final decision")
		return False

	try:
		print("\n📊 Loading remaining predictions...")
		predictions = load_predictions(predictions_folder)
		predictions = [prediction for prediction in predictions if profile.allows_symbol(str(prediction.get("symbol", "")))]
		print(
			f"   Found {len(predictions)} filtered predictions "
			f"(standard >= {get_base_prediction_threshold():.0f}%, crypto >= {get_crypto_prediction_threshold():.0f}%)"
		)
		if not predictions:
			print("⚠️  No strong predictions available, skipping final decision")
			return False

		account_state = get_account_state(include_margin_percent=True)
		_print_account_state(account_state)

		open_positions = get_open_positions()
		_print_open_positions(open_positions)
		profile_open_positions = _get_profile_open_positions(open_positions, profile)
		profile_limit_label = profile.max_open_positions if _is_limit_enabled(profile.max_open_positions) else "unlimited"
		print(f"   Open positions for profile {profile.strategy_id}: {len(profile_open_positions)}/{profile_limit_label}")
		if _is_limit_enabled(profile.max_open_positions) and len(profile_open_positions) >= profile.max_open_positions:
			print("⚠️  Strategy max open positions reached, skipping final decision")
			return False
		open_crypto_positions = _count_open_crypto_positions(open_positions)
		print(f"   Open crypto positions: {open_crypto_positions}/{get_crypto_max_open_positions()}")

		gemini_config = _load_gemini_api_config()

		max_retries = 3
		excluded_symbols: List[str] = []
		full_control_every_n = _get_gemini_full_control_every_n_trades()
		successful_trades = count_successful_trades(service_folder, strategy_id=profile.strategy_id)
		trades_today = count_successful_trades_today(service_folder, strategy_id=profile.strategy_id)
		if _is_limit_enabled(profile.max_trades_per_day) and trades_today >= profile.max_trades_per_day:
			print(
				f"⚠️  Daily trade limit reached for {profile.strategy_id}: "
				f"{trades_today}/{profile.max_trades_per_day}"
			)
			return False
		next_trade_number = successful_trades + 1
		gemini_full_control_mode = (next_trade_number % full_control_every_n) == 0

		_print_trade_mode(successful_trades, next_trade_number, full_control_every_n, gemini_full_control_mode)

		for attempt in range(max_retries):
			print(f"\n🔄 Decision attempt {attempt + 1}/{max_retries}")
			decision_text = ask_gemini_final_decision(
				predictions,
				open_positions,
				account_state,
				gemini_config,
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
			if not profile.allows_symbol(symbol):
				if not _exclude_symbol_and_retry(symbol, f"Symbol {symbol} is outside profile whitelist", predictions, excluded_symbols):
					return False
				continue

			symbol_trades_today = count_successful_trades_today(
				service_folder,
				strategy_id=profile.strategy_id,
				symbol=symbol,
			)
			if _is_limit_enabled(profile.max_trades_per_symbol_per_day) and symbol_trades_today >= profile.max_trades_per_symbol_per_day:
				if not _exclude_symbol_and_retry(
					symbol,
					f"Daily symbol limit reached for {symbol} ({symbol_trades_today}/{profile.max_trades_per_symbol_per_day})",
					predictions,
					excluded_symbols,
				):
					return False
				continue

			prediction_match = next((prediction for prediction in predictions if prediction.get("symbol") == symbol), None)
			market_context = _get_prediction_market_context(prediction_match or {})
			news_decision = should_block_symbol_for_news(symbol)
			if news_decision.blocked:
				if not _exclude_symbol_and_retry(symbol, f"News filter rejected {symbol} ({news_decision.reason})", predictions, excluded_symbols):
					return False
				continue
			if market_context.get("spread_ok") is False:
				if not _exclude_symbol_and_retry(symbol, f"Spread filter rejected {symbol}", predictions, excluded_symbols):
					return False
				continue
			symbol_is_crypto = is_crypto_symbol(symbol)
			symbol_is_cfd = is_cfd_symbol(symbol)
			if symbol_is_crypto and open_crypto_positions >= get_crypto_max_open_positions():
				if not _exclude_symbol_and_retry(
					symbol,
					f"Crypto position limit reached for {symbol} ({open_crypto_positions}/{get_crypto_max_open_positions()})",
					predictions,
					excluded_symbols,
				):
					return False
				continue

			is_valid, error_msg = validate_symbol(symbol)
			if not is_valid:
				if not _exclude_symbol_and_retry(symbol, f"Symbol validation failed: {error_msg}", predictions, excluded_symbols):
					return False
				continue

			symbol_full_control_mode = gemini_full_control_mode
			if symbol_is_crypto and gemini_full_control_mode and not is_crypto_full_tp_mode_allowed():
				symbol_full_control_mode = False
				print("   Crypto symbol selected: full Gemini TP mode disabled for this trade")
			if symbol_is_cfd and gemini_full_control_mode and not is_cfd_full_tp_mode_allowed():
				symbol_full_control_mode = False
				print("   CFD symbol selected: TP mode disabled by configuration for this trade")

			resolved = _resolve_trade_parameters(
				gemini_full_control_mode=symbol_full_control_mode,
				gemini_lot_size=gemini_lot_size,
				gemini_take_profit=gemini_take_profit,
				account_state=account_state,
				profile=profile,
				owned_open_positions=len(profile_open_positions),
				symbol=symbol,
				action=action,
				symbol_is_crypto=symbol_is_crypto,
				symbol_is_cfd=symbol_is_cfd,
				market_context=market_context,
			)
			if resolved is None:
				if not _exclude_symbol_and_retry(symbol, f"Trade parameters invalid for {symbol}", predictions, excluded_symbols):
					return False
				continue

			lot_size, take_profit = resolved
			if lot_size <= 0:
				if not _exclude_symbol_and_retry(symbol, f"Final lot size is {lot_size} for {symbol}", predictions, excluded_symbols):
					return False
				continue

			if execute_trade(
				symbol,
				action,
				lot_size,
				service_folder,
				take_profit,
				lot_source="gemini_prediction",
				strategy_id=profile.strategy_id,
				magic=profile.magic,
			):
				print("\n🎉 Trade executed successfully!")
				print("\n" + "=" * 60)
				print("✅ Final Trading Decision Completed")
				print("=" * 60)
				return True

			if not _exclude_symbol_and_retry(symbol, f"Trade execution failed for {symbol}", predictions, excluded_symbols):
				return False

		print(f"❌ Exhausted all {max_retries} retry attempts")
		return False

	except Exception as exc:
		print(f"❌ Error in final decision phase: {exc}")
		import traceback

		traceback.print_exc()
		return False
