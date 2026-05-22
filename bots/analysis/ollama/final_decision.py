"""Final trading decision orchestration."""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from account_state import get_account_state
from ai_advisory_state import build_decision_signature, get_active_rejection, get_cached_decision, record_rejection, store_cached_decision
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
from mt5_symbols import estimate_order_profit, get_current_price, get_symbol_info
from mt5_positions import get_open_positions
from parallel_strategy_mean_reversion import can_activate_parallel_strategy, validate_mean_reversion_signal
from risk_engine import calculate_synthetic_risk_plan
from signal_rules import validate_trend_following_signal
from strategy_context import (
	StrategyContext,
	build_strategy_comment,
	count_open_positions_for_strategy,
	get_parallel_strategy_context,
	get_primary_strategy_context,
	is_strategy_trade_window_open,
)
from trade_execution import execute_trade
from trade_history import count_successful_trades, count_successful_trades_since, count_successful_trades_today
from trading_validation import check_margin_requirements, validate_symbol


if hasattr(sys.stdout, "reconfigure"):
	try:
		sys.stdout.reconfigure(encoding="utf-8", errors="replace")
	except Exception:
		pass


TRADE_DECISION_AUDIT_HEADERS = [
	"timestamp",
	"strategy_id",
	"strategy_label",
	"symbol",
	"action",
	"candidate_source",
	"stage",
	"trade_executed",
	"reason",
	"details",
]

TRADE_DECISION_SNAPSHOT_HEADERS = [
	"timestamp",
	"strategy_id",
	"strategy_label",
	"symbol",
	"action",
	"candidate_source",
	"stage",
	"trade_executed",
	"reason",
	"details",
]


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
	print(f"   Legacy Gemini full-control cadence (ignored for lot/TP): N={full_control_every_n}")
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
			"ADVISORY GEMINI MODE (Gemini may rank symbol/action only)"
			if gemini_full_control_mode
			else "LOCAL SIGNAL/RISK MODE (lot_size + take_profit from local logic)"
		)
	)


def _get_int_env(name: str, default: int, *, minimum: int = 0) -> int:
	raw = os.getenv(name)
	if raw is None:
		return default
	try:
		value = int(raw)
		if value < minimum:
			raise ValueError
		return value
	except (TypeError, ValueError):
		return default


def _load_market_data_for_symbol(predictions_folder: Path, symbol: str) -> Optional[Dict]:
	source_folder = predictions_folder.parent / "source"
	market_data_file = source_folder / f"{symbol}.json"
	if not market_data_file.exists():
		return None
	try:
		return json.loads(market_data_file.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return None


def _log_jsonl(service_folder: Path, file_name: str, payload: Dict) -> None:
	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	with open(log_dir / file_name, "a", encoding="utf-8") as handle:
		handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _update_trade_decision_snapshot(
	service_folder: Path,
	*,
	strategy_id: str,
	strategy_label: str,
	symbol: str,
	action: str,
	candidate_source: str,
	stage: str,
	trade_executed: bool,
	reason: str,
	details: Optional[Dict[str, object]],
) -> None:
	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	log_file = log_dir / "trade_decision_snapshot.csv"
	rows_by_label: Dict[str, Dict[str, str]] = {}
	if log_file.exists():
		with open(log_file, "r", newline="", encoding="utf-8") as handle:
			for row in csv.DictReader(handle):
				label = str(row.get("strategy_label", "") or "").strip()
				if label:
					rows_by_label[label] = row

	rows_by_label[strategy_label] = {
		"timestamp": datetime.now(tz=timezone.utc).isoformat(),
		"strategy_id": strategy_id,
		"strategy_label": strategy_label,
		"symbol": symbol,
		"action": action,
		"candidate_source": candidate_source,
		"stage": stage,
		"trade_executed": str(trade_executed),
		"reason": reason,
		"details": json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
	}

	with open(log_file, "w", newline="", encoding="utf-8") as handle:
		writer = csv.DictWriter(handle, fieldnames=TRADE_DECISION_SNAPSHOT_HEADERS)
		writer.writeheader()
		for label in sorted(rows_by_label):
			writer.writerow(rows_by_label[label])


def _log_trade_decision_audit(
	service_folder: Path,
	*,
	strategy_id: str,
	strategy_label: str,
	symbol: str = "",
	action: str = "",
	candidate_source: str = "",
	stage: str,
	trade_executed: bool,
	reason: str = "",
	details: Optional[Dict[str, object]] = None,
) -> None:
	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	log_file = log_dir / "trade_decision_audit.csv"
	file_exists = log_file.exists()
	with open(log_file, "a", newline="", encoding="utf-8") as handle:
		writer = csv.writer(handle)
		if not file_exists:
			writer.writerow(TRADE_DECISION_AUDIT_HEADERS)
		writer.writerow(
			[
				datetime.now(tz=timezone.utc).isoformat(),
				strategy_id,
				strategy_label,
				symbol,
				action,
				candidate_source,
				stage,
				str(trade_executed),
				reason,
				json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
			]
		)
	_update_trade_decision_snapshot(
		service_folder,
		strategy_id=strategy_id,
		strategy_label=strategy_label,
		symbol=symbol,
		action=action,
		candidate_source=candidate_source,
		stage=stage,
		trade_executed=trade_executed,
		reason=reason,
		details=details,
	)


def _get_primary_max_open_positions() -> int:
	return _get_int_env("PRIMARY_MAX_OPEN_POSITIONS", 0, minimum=0)


def _get_primary_max_trades_per_day() -> int:
	return _get_int_env("PRIMARY_MAX_TRADES_PER_DAY", 0, minimum=0)


def _get_primary_max_trades_per_symbol_per_day() -> int:
	return _get_int_env("PRIMARY_MAX_TRADES_PER_SYMBOL_PER_DAY", 0, minimum=0)


def _get_symbol_trade_cooldown_minutes() -> int:
	return _get_int_env("SYMBOL_TRADE_COOLDOWN_MINUTES", 15, minimum=0)


def _has_open_position_on_symbol(open_positions: List[Dict], symbol: str) -> bool:
	return any(str(position.get("symbol", "")) == symbol for position in open_positions)


def _count_open_crypto_positions(open_positions: List[Dict]) -> int:
	"""Count open positions belonging to the crypto instrument group."""
	return len([pos for pos in open_positions if is_crypto_symbol(str(pos.get("symbol", "")))])


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


@dataclass(frozen=True)
class RankedCandidate:
	symbol: str
	action: str
	source: str
	score: float


@dataclass(frozen=True)
class StrategyExecutionProfile:
	label: str
	context: StrategyContext
	signal_validator: Callable[[str, str, Dict], object]
	risk_percent_env: str
	stop_atr_multiplier_env: str
	tp_r_multiple_env: str
	max_trades_per_day_env: str
	max_trades_per_symbol_per_day_env: str
	trade_cooldown_env: str
	default_trade_cooldown_minutes: int
	default_max_trades_per_day: int = 0
	default_max_trades_per_symbol_per_day: int = 0


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


def _get_gemini_decision_cache_minutes() -> int:
	return _get_int_env(
		"GEMINI_DECISION_CACHE_MINUTES",
		_get_int_env("GEMINI_QUERY_COOLDOWN_MINUTES", 15, minimum=0),
		minimum=0,
	)


def _get_gemini_rejection_cooldown_minutes() -> int:
	return _get_int_env("GEMINI_REJECTION_COOLDOWN_MINUTES", 30, minimum=0)


def _candidate_key(symbol: str, action: str) -> Tuple[str, str]:
	return symbol.strip(), action.strip().upper()


def _build_local_candidates(predictions: List[Dict]) -> List[RankedCandidate]:
	candidates: List[RankedCandidate] = []
	for prediction in predictions:
		symbol = str(prediction.get("symbol", "") or "").strip()
		if not symbol:
			continue
		buy_score = float(prediction.get("BUY", 0.0) or 0.0)
		sell_score = float(prediction.get("SELL", 0.0) or 0.0)
		action = "BUY" if buy_score >= sell_score else "SELL"
		confidence = max(buy_score, sell_score)
		separation = abs(buy_score - sell_score)
		candidates.append(
			RankedCandidate(
				symbol=symbol,
				action=action,
				source="local_prediction_ranking",
				score=(confidence * 1000.0) + separation,
			)
		)

	return sorted(candidates, key=lambda item: item.score, reverse=True)


def _build_candidate_queue(predictions: List[Dict], advisory_candidate: Optional[RankedCandidate]) -> List[RankedCandidate]:
	ordered: List[RankedCandidate] = []
	seen: set[Tuple[str, str]] = set()

	if advisory_candidate is not None:
		key = _candidate_key(advisory_candidate.symbol, advisory_candidate.action)
		ordered.append(advisory_candidate)
		seen.add(key)

	for candidate in _build_local_candidates(predictions):
		key = _candidate_key(candidate.symbol, candidate.action)
		if key in seen:
			continue
		ordered.append(candidate)
		seen.add(key)

	return ordered


def _resolve_gemini_advisory_candidate(
	*,
	predictions: List[Dict],
	open_positions: List[Dict],
	account_state: Dict,
	service_folder: Path,
) -> Optional[RankedCandidate]:
	cache_signature = build_decision_signature(account_state, open_positions, predictions)
	cached_decision = get_cached_decision(service_folder, cache_signature, _get_gemini_decision_cache_minutes())
	if isinstance(cached_decision, dict):
		symbol = str(cached_decision.get("recommended_symbol", "") or "").strip()
		action = str(cached_decision.get("action", "") or "").strip().upper()
		if symbol and action:
			_log_jsonl(
				service_folder,
				"ai_log.jsonl",
				{
					"timestamp": datetime.now(tz=timezone.utc).isoformat(),
					"source": "gemini_cached_advisory",
					"signature": cache_signature,
					"recommended_symbol": symbol,
					"action": action,
				},
			)
			return RankedCandidate(symbol=symbol, action=action, source="gemini_cached_advisory", score=10_000_000.0)

	gemini_config = _load_gemini_api_config()
	decision_text = ask_gemini_final_decision(
		predictions,
		open_positions,
		account_state,
		gemini_config,
		trade_number=count_successful_trades(service_folder) + 1,
		full_control_every_n=_get_gemini_full_control_every_n_trades(),
		gemini_full_control_mode=False,
	)
	if not decision_text:
		return None

	_save_decision_text(service_folder, decision_text)
	try:
		decision_payload = json.loads(decision_text)
	except json.JSONDecodeError:
		return None
	store_cached_decision(service_folder, cache_signature, decision_payload)

	symbol = str(decision_payload.get("recommended_symbol", "") or "").strip()
	action = str(decision_payload.get("action", "") or "").strip().upper()
	if not symbol or not action:
		return None

	_log_jsonl(
		service_folder,
		"ai_log.jsonl",
		{
			"timestamp": datetime.now(tz=timezone.utc).isoformat(),
			"source": "gemini_live_advisory",
			"signature": cache_signature,
			"recommended_symbol": symbol,
			"action": action,
		},
	)
	return RankedCandidate(symbol=symbol, action=action, source="gemini_live_advisory", score=10_000_000.0)


def _get_strategy_limit(env_name: str, default: int) -> int:
	return _get_int_env(env_name, default, minimum=0)


def _is_candidate_rejected(service_folder: Path, strategy_id: str, symbol: str, action: str) -> Optional[Dict[str, object]]:
	return get_active_rejection(service_folder, strategy_id=strategy_id, symbol=symbol, action=action)


def _record_candidate_rejection(service_folder: Path, strategy_id: str, symbol: str, action: str, reason: str) -> None:
	record_rejection(
		service_folder,
		strategy_id=strategy_id,
		symbol=symbol,
		action=action,
		reason=reason,
		cooldown_minutes=_get_gemini_rejection_cooldown_minutes(),
	)


def _attempt_strategy_trade(
	*,
	profile: StrategyExecutionProfile,
	candidates: List[RankedCandidate],
	predictions_folder: Path,
	service_folder: Path,
	account_state: Dict,
	open_positions: List[Dict],
	open_crypto_positions: int,
) -> bool:
	open_strategy_positions = count_open_positions_for_strategy(open_positions, profile.context)
	if not is_strategy_trade_window_open(profile.context):
		print(f"⚠️  {profile.label} strategy is outside its configured session window, skipping")
		_log_trade_decision_audit(
			service_folder,
			strategy_id=profile.context.strategy_id,
			strategy_label=profile.label,
			stage="strategy_blocked",
			trade_executed=False,
			reason="outside_session_window",
		)
		return False
	max_trades_per_day = _get_strategy_limit(profile.max_trades_per_day_env, profile.default_max_trades_per_day)
	if profile.context.max_open_positions > 0 and open_strategy_positions >= profile.context.max_open_positions:
		print(f"⚠️  {profile.label} strategy max open positions reached, skipping")
		_log_trade_decision_audit(
			service_folder,
			strategy_id=profile.context.strategy_id,
			strategy_label=profile.label,
			stage="strategy_blocked",
			trade_executed=False,
			reason="max_open_positions_reached",
			details={"open_strategy_positions": open_strategy_positions},
		)
		return False
	if max_trades_per_day > 0 and count_successful_trades_today(service_folder, strategy_id=profile.context.strategy_id) >= max_trades_per_day:
		print(f"⚠️  {profile.label} strategy daily trade limit reached, skipping")
		_log_trade_decision_audit(
			service_folder,
			strategy_id=profile.context.strategy_id,
			strategy_label=profile.label,
			stage="strategy_blocked",
			trade_executed=False,
			reason="daily_trade_limit_reached",
		)
		return False

	trade_cooldown_minutes = _get_strategy_limit(profile.trade_cooldown_env, profile.default_trade_cooldown_minutes)
	per_symbol_daily_limit = _get_strategy_limit(
		profile.max_trades_per_symbol_per_day_env,
		profile.default_max_trades_per_symbol_per_day,
	)

	for candidate in candidates:
		symbol = candidate.symbol
		action = candidate.action
		active_rejection = _is_candidate_rejected(service_folder, profile.context.strategy_id, symbol, action)
		if active_rejection is not None:
			print(f"⚠️  {profile.label} rejection cooldown active for {symbol} {action}")
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				symbol=symbol,
				action=action,
				candidate_source=candidate.source,
				stage="candidate_skipped",
				trade_executed=False,
				reason="rejection_cooldown_active",
				details=active_rejection,
			)
			continue
		if _has_open_position_on_symbol(open_positions, symbol):
			_record_candidate_rejection(service_folder, profile.context.strategy_id, symbol, action, "open_position_exists")
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				symbol=symbol,
				action=action,
				candidate_source=candidate.source,
				stage="candidate_skipped",
				trade_executed=False,
				reason="open_position_exists",
			)
			continue
		if trade_cooldown_minutes > 0 and count_successful_trades_since(
			service_folder,
			strategy_id=profile.context.strategy_id,
			symbol=symbol,
			lookback=timedelta(minutes=trade_cooldown_minutes),
		) > 0:
			_record_candidate_rejection(service_folder, profile.context.strategy_id, symbol, action, "recent_symbol_trade")
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				symbol=symbol,
				action=action,
				candidate_source=candidate.source,
				stage="candidate_skipped",
				trade_executed=False,
				reason="recent_symbol_trade",
				details={"cooldown_minutes": trade_cooldown_minutes},
			)
			continue
		if per_symbol_daily_limit > 0 and count_successful_trades_since(
			service_folder,
			strategy_id=profile.context.strategy_id,
			symbol=symbol,
			lookback=timedelta(hours=24),
		) >= per_symbol_daily_limit:
			_record_candidate_rejection(service_folder, profile.context.strategy_id, symbol, action, "per_symbol_daily_limit")
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				symbol=symbol,
				action=action,
				candidate_source=candidate.source,
				stage="candidate_skipped",
				trade_executed=False,
				reason="per_symbol_daily_limit",
				details={"per_symbol_daily_limit": per_symbol_daily_limit},
			)
			continue

		market_data = _load_market_data_for_symbol(predictions_folder, symbol)
		if market_data is None:
			_record_candidate_rejection(service_folder, profile.context.strategy_id, symbol, action, "market_data_missing")
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				symbol=symbol,
				action=action,
				candidate_source=candidate.source,
				stage="candidate_skipped",
				trade_executed=False,
				reason="market_data_missing",
			)
			continue

		signal_validation = profile.signal_validator(symbol, action, market_data)
		if not signal_validation.allowed:
			_log_jsonl(
				service_folder,
				"decision_log.jsonl",
				{
					"timestamp": datetime.now(tz=timezone.utc).isoformat(),
					"strategy_id": profile.context.strategy_id,
					"symbol": symbol,
					"action": action,
					"decision_source": candidate.source,
					"allowed": False,
					"reason_codes": signal_validation.reason_codes,
					"regime_state": signal_validation.regime_state,
					"metrics": signal_validation.metrics,
				},
			)
			_record_candidate_rejection(
				service_folder,
				profile.context.strategy_id,
				symbol,
				action,
				"signal_rejected:" + ",".join(signal_validation.reason_codes),
			)
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				symbol=symbol,
				action=action,
				candidate_source=candidate.source,
				stage="candidate_rejected",
				trade_executed=False,
				reason="signal_rejected",
				details={
					"reason_codes": signal_validation.reason_codes,
					"regime_state": signal_validation.regime_state,
				},
			)
			continue

		symbol_is_crypto = is_crypto_symbol(symbol)
		if profile.label == "primary" and symbol_is_crypto and open_crypto_positions >= get_crypto_max_open_positions():
			_record_candidate_rejection(service_folder, profile.context.strategy_id, symbol, action, "crypto_position_limit")
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				symbol=symbol,
				action=action,
				candidate_source=candidate.source,
				stage="candidate_skipped",
				trade_executed=False,
				reason="crypto_position_limit",
			)
			continue

		symbol_is_cfd = is_cfd_symbol(symbol)
		is_valid, error_msg = validate_symbol(symbol)
		if not is_valid:
			_record_candidate_rejection(service_folder, profile.context.strategy_id, symbol, action, f"symbol_validation_failed:{error_msg}")
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				symbol=symbol,
				action=action,
				candidate_source=candidate.source,
				stage="candidate_rejected",
				trade_executed=False,
				reason="symbol_validation_failed",
				details={"error_msg": error_msg},
			)
			continue

		resolved = _resolve_trade_parameters(
			gemini_full_control_mode=False,
			gemini_lot_size=None,
			gemini_take_profit=None,
			account_state=account_state,
			symbol=symbol,
			action=action,
			symbol_is_crypto=symbol_is_crypto,
			symbol_is_cfd=symbol_is_cfd,
			market_data=market_data,
			risk_percent_env=profile.risk_percent_env,
			stop_atr_multiplier_env=profile.stop_atr_multiplier_env,
			tp_r_multiple_env=profile.tp_r_multiple_env,
		)
		if resolved is None:
			_record_candidate_rejection(service_folder, profile.context.strategy_id, symbol, action, "trade_parameters_invalid")
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				symbol=symbol,
				action=action,
				candidate_source=candidate.source,
				stage="candidate_rejected",
				trade_executed=False,
				reason="trade_parameters_invalid",
			)
			continue

		lot_size, take_profit = resolved
		risk_plan = calculate_synthetic_risk_plan(
			symbol=symbol,
			action=action,
			account_state=account_state,
			market_data=market_data,
			risk_percent_env=profile.risk_percent_env,
			stop_atr_multiplier_env=profile.stop_atr_multiplier_env,
			tp_r_multiple_env=profile.tp_r_multiple_env,
		)
		extra_log_data = {
			"decision_source": candidate.source,
			"reason_codes": signal_validation.reason_codes,
			"regime_state": signal_validation.regime_state,
			"metrics": signal_validation.metrics,
		}
		if risk_plan is not None:
			extra_log_data.update(
				{
					"risk_usd": risk_plan.risk_usd,
					"synthetic_stop_price": risk_plan.synthetic_stop_price,
					"synthetic_stop_distance": risk_plan.synthetic_stop_distance,
					"take_profit_price": risk_plan.take_profit_price,
				}
			)

		if execute_trade(
			symbol,
			action,
			lot_size,
			service_folder,
			take_profit,
			lot_source="synthetic_risk",
			strategy_id=profile.context.strategy_id,
			magic=profile.context.magic,
			comment=build_strategy_comment(profile.context.strategy_id),
			extra_log_data=extra_log_data,
		):
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				symbol=symbol,
				action=action,
				candidate_source=candidate.source,
				stage="trade_executed",
				trade_executed=True,
				reason="trade_opened",
				details={"lot_size": lot_size, "take_profit": take_profit},
			)
			_log_jsonl(
				service_folder,
				"risk_log.jsonl",
				{
					"timestamp": datetime.now(tz=timezone.utc).isoformat(),
					"strategy_id": profile.context.strategy_id,
					"entry_owner_strategy_id": profile.context.strategy_id,
					"management_owner_strategy_id": profile.context.strategy_id,
					"symbol": symbol,
					"action": action,
					"lot_size": lot_size,
					"take_profit": take_profit,
					**extra_log_data,
				},
			)
			print(f"\n🎉 {profile.label.capitalize()} trade executed successfully!")
			return True

		_record_candidate_rejection(service_folder, profile.context.strategy_id, symbol, action, "trade_execution_failed")
		_log_trade_decision_audit(
			service_folder,
			strategy_id=profile.context.strategy_id,
			strategy_label=profile.label,
			symbol=symbol,
			action=action,
			candidate_source=candidate.source,
			stage="candidate_rejected",
			trade_executed=False,
			reason="trade_execution_failed",
		)

	return False


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
	symbol: str,
	action: str,
	symbol_is_crypto: bool,
	symbol_is_cfd: bool,
	market_data: Optional[Dict] = None,
	risk_percent_env: str = "PRIMARY_RISK_PER_TRADE_PERCENT",
	stop_atr_multiplier_env: str = "PRIMARY_SYNTHETIC_STOP_ATR_MULTIPLIER",
	tp_r_multiple_env: str = "PRIMARY_TAKE_PROFIT_R_MULTIPLIER",
) -> Optional[Tuple[float, Optional[float]]]:
	"""Resolve final lot size and take profit.

	When market_data is available, lot sizing and TP use the local synthetic-risk model.
	Legacy Gemini lot/TP behavior remains as a fallback for older call sites and tests.
	"""
	if market_data is not None:
		risk_plan = calculate_synthetic_risk_plan(
			symbol=symbol,
			action=action,
			account_state=account_state,
			market_data=market_data,
			risk_percent_env=risk_percent_env,
			stop_atr_multiplier_env=stop_atr_multiplier_env,
			tp_r_multiple_env=tp_r_multiple_env,
		)
		if risk_plan is None:
			print(f"⚠️  Synthetic risk plan could not be resolved for {symbol}")
			return None

		lot_size = risk_plan.lot_size
		print(f"   Local synthetic-risk lot_size: {lot_size}")
		base_take_profit = risk_plan.take_profit_price
		if symbol_is_crypto:
			take_profit = _resolve_crypto_take_profit(symbol, action, base_take_profit)
			if take_profit is None:
				return None
			return lot_size, take_profit
		if symbol_is_cfd:
			take_profit = _resolve_cfd_take_profit(symbol, action, lot_size, base_take_profit)
			return lot_size, take_profit
		return lot_size, base_take_profit

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

	print(f"   Using Gemini lot_size: {lot_size}")

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
		print(
			f"   Found {len(predictions)} filtered predictions "
			f"(standard >= {get_base_prediction_threshold():.0f}%, crypto >= {get_crypto_prediction_threshold():.0f}%)"
		)
		if not predictions:
			print("⚠️  No strong predictions available, skipping final decision")
			_log_trade_decision_audit(
				service_folder,
				strategy_id="runtime",
				strategy_label="cycle",
				stage="cycle_skipped",
				trade_executed=False,
				reason="no_predictions",
			)
			return False

		account_state = get_account_state(include_margin_percent=True)
		_print_account_state(account_state)

		open_positions = get_open_positions()
		_print_open_positions(open_positions)
		open_crypto_positions = _count_open_crypto_positions(open_positions)
		print(f"   Open crypto positions: {open_crypto_positions}/{get_crypto_max_open_positions()}")
		full_control_every_n = _get_gemini_full_control_every_n_trades()
		successful_trades = count_successful_trades(service_folder)
		next_trade_number = successful_trades + 1
		gemini_full_control_mode = (next_trade_number % full_control_every_n) == 0

		_print_trade_mode(successful_trades, next_trade_number, full_control_every_n, gemini_full_control_mode)

		advisory_candidate = _resolve_gemini_advisory_candidate(
			predictions=predictions,
			open_positions=open_positions,
			account_state=account_state,
			service_folder=service_folder,
		)
		candidate_queue = _build_candidate_queue(predictions, advisory_candidate)

		primary_profile = StrategyExecutionProfile(
			label="primary",
			context=get_primary_strategy_context(),
			signal_validator=validate_trend_following_signal,
			risk_percent_env="PRIMARY_RISK_PER_TRADE_PERCENT",
			stop_atr_multiplier_env="PRIMARY_SYNTHETIC_STOP_ATR_MULTIPLIER",
			tp_r_multiple_env="PRIMARY_TAKE_PROFIT_R_MULTIPLIER",
			max_trades_per_day_env="PRIMARY_MAX_TRADES_PER_DAY",
			max_trades_per_symbol_per_day_env="PRIMARY_MAX_TRADES_PER_SYMBOL_PER_DAY",
			trade_cooldown_env="SYMBOL_TRADE_COOLDOWN_MINUTES",
			default_trade_cooldown_minutes=_get_symbol_trade_cooldown_minutes(),
		)
		if _attempt_strategy_trade(
			profile=primary_profile,
			candidates=candidate_queue,
			predictions_folder=predictions_folder,
			service_folder=service_folder,
			account_state=account_state,
			open_positions=open_positions,
			open_crypto_positions=open_crypto_positions,
		):
			print("\n" + "=" * 60)
			print("✅ Final Trading Decision Completed")
			print("=" * 60)
			return True

		if can_activate_parallel_strategy(account_state, open_positions):
			parallel_profile = StrategyExecutionProfile(
				label="parallel",
				context=get_parallel_strategy_context(),
				signal_validator=validate_mean_reversion_signal,
				risk_percent_env="PARALLEL_RISK_PER_TRADE_PERCENT",
				stop_atr_multiplier_env="PARALLEL_SYNTHETIC_STOP_ATR_MULTIPLIER",
				tp_r_multiple_env="PARALLEL_TAKE_PROFIT_R_MULTIPLIER",
				max_trades_per_day_env="PARALLEL_MAX_TRADES_PER_DAY",
				max_trades_per_symbol_per_day_env="PARALLEL_MAX_TRADES_PER_SYMBOL_PER_DAY",
				trade_cooldown_env="PARALLEL_SYMBOL_TRADE_COOLDOWN_MINUTES",
				default_trade_cooldown_minutes=30,
				default_max_trades_per_symbol_per_day=1,
			)
			if _attempt_strategy_trade(
				profile=parallel_profile,
				candidates=candidate_queue,
				predictions_folder=predictions_folder,
				service_folder=service_folder,
				account_state=account_state,
				open_positions=open_positions,
				open_crypto_positions=open_crypto_positions,
			):
				print("\n" + "=" * 60)
				print("✅ Final Trading Decision Completed")
				print("=" * 60)
				return True
		else:
			print("⚠️  Parallel strategy activation gate not satisfied")
			_log_trade_decision_audit(
				service_folder,
				strategy_id=get_parallel_strategy_context().strategy_id,
				strategy_label="parallel",
				stage="strategy_blocked",
				trade_executed=False,
				reason="activation_gate_not_satisfied",
			)

		print("❌ No strategy found an executable trade in this cycle")
		_log_trade_decision_audit(
			service_folder,
			strategy_id="runtime",
			strategy_label="cycle",
			stage="cycle_completed",
			trade_executed=False,
			reason="no_executable_trade",
		)
		return False

	except Exception as exc:
		print(f"❌ Error in final decision phase: {exc}")
		_log_trade_decision_audit(
			service_folder,
			strategy_id="runtime",
			strategy_label="cycle",
			stage="cycle_error",
			trade_executed=False,
			reason="exception",
			details={"error": str(exc)},
		)
		import traceback

		traceback.print_exc()
		return False
