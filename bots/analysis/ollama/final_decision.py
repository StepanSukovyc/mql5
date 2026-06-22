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
from ollama_service import is_ollama_cloud_enabled
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
from quant_math_strategy import build_quant_candidates, can_activate_quant_strategy, is_quant_strategy_enabled, validate_quant_signal
from reversal_pattern_strategy import (
	can_activate_reversal_strategy,
	is_reversal_strategy_enabled,
	validate_reversal_pattern_signal,
)
from risk_engine import calculate_synthetic_risk_plan
from signal_rules import validate_trend_following_signal
from strategy_context import (
	StrategyContext,
	build_strategy_comment,
	count_open_positions_for_strategy,
	get_ollama_cloud_strategy_context,
	get_parallel_strategy_context,
	get_primary_strategy_context,
	get_quant_strategy_context,
	get_reversal_strategy_context,
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
	"candidate_queue",
	"candidate_rank",
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
	"candidate_queue",
	"candidate_rank",
	"stage",
	"trade_executed",
	"reason",
	"reason_cs",
	"summary_cs",
	"transition_stage",
	"transition_reason",
	"transition_reason_cs",
	"transition_summary_cs",
	"transition_details",
	"details",
]

PARALLEL_STRATEGY_STATUS_HEADERS = [
	"timestamp",
	"strategy_id",
	"strategy_label",
	"symbol",
	"action",
	"candidate_source",
	"candidate_queue",
	"candidate_rank",
	"stage",
	"trade_executed",
	"reason",
	"reason_cs",
	"root_rejection_reason",
	"root_rejection_reason_cs",
	"rule_failures",
	"rule_failures_cs",
	"cooldown_expires_at",
	"summary_cs",
	"details",
]

REASON_TEXT_CS = {
	"activation_margin_below_threshold": "Volna marze je pod aktivacnim prahem strategie.",
	"outside_session_window": "Strategie je mimo povolene obchodni hodiny.",
	"max_open_positions_reached": "Strategie uz ma maximalni pocet otevrenych pozic.",
	"daily_trade_limit_reached": "Byl dosazen denni limit obchodu pro strategii.",
	"rejection_cooldown_active": "Bezi cooldown po predchozim zamitnuti kandidata.",
	"open_position_exists": "Na tomto symbolu uz existuje otevrena pozice.",
	"recent_symbol_trade": "Na tomto symbolu probehl obchod prilis nedavno.",
	"per_symbol_daily_limit": "Byl dosazen denni limit obchodu pro tento symbol.",
	"market_data_missing": "Pro symbol chybi market data potrebna pro vyhodnoceni.",
	"signal_rejected": "Signal alternativni strategie nesplnil vstupni pravidla.",
	"crypto_position_limit": "Byl dosazen limit otevrenych crypto pozic.",
	"symbol_validation_failed": "Symbol neprosel validaci pro exekuci.",
	"trade_parameters_invalid": "Nepodarilo se pripravit platne parametry obchodu.",
	"trade_execution_failed": "Exekuce obchodu na brokerovi selhala.",
	"trade_opened": "Obchod byl uspesne otevren.",
	"activation_gate_not_satisfied": "Aktivacni podminky paralelni strategie nejsou splnene.",
	"no_quant_candidates": "Kvantitativni strategie nenasla zadne kandidaty bez AI.",
	"gemini_candidates_exhausted_local_fallback": "Gemini kandidati byli vycerpani a strategie presla na lokalni fallback.",
	"no_executable_trade": "V tomto cyklu nebyl nalezen zadny obchod k exekuci.",
	"no_predictions": "Nejsou k dispozici zadne pouzitelne predikce.",
	"exception": "Behem rozhodovaci faze doslo k vyjimce.",
}

RULE_REASON_TEXT_CS = {
	"symbol_not_in_parallel_whitelist": "Symbol neni na whitelistu paralelni strategie.",
	"adx_above_range_threshold": "ADX je prilis vysoky, trh je moc trendovy pro mean reversion.",
	"spread_above_limit": "Spread je vyssi nez povoleny limit strategie.",
	"vwap_distance_below_threshold": "Cena neni dostatecne vzdalena od VWAP vzhledem k ATR.",
	"news_blocked": "Symbol je blokovany kvuli zpravodajskemu filtru.",
	"close_not_below_lower_band": "Pro BUY neni close pod dolnim Bollinger bandem.",
	"rsi2_not_extreme_long": "RSI2 neni dostatecne preprodane pro long vstup.",
	"close_not_above_upper_band": "Pro SELL neni close nad hornim Bollinger bandem.",
	"rsi2_not_extreme_short": "RSI2 neni dostatecne prekoupene pro short vstup.",
	"symbol_not_in_reversal_whitelist": "Symbol neni na whitelistu reverzni strategie.",
	"adx_above_reversal_threshold": "ADX je prilis vysoky pro reverzni vstup proti pohybu.",
	"pattern_range_below_threshold": "Reverzni pattern je vzhledem k ATR prilis maly.",
	"pattern_not_at_lower_extreme": "Bullish reverzni pattern nevznikl dostatecne nizko u extremu.",
	"pattern_not_at_upper_extreme": "Bearish reverzni pattern nevznikl dostatecne vysoko u extremu.",
	"close_not_below_vwap": "Pro long reverzni setup je close uz nad VWAP, vstup je pozde.",
	"close_not_above_vwap": "Pro short reverzni setup je close uz pod VWAP, vstup je pozde.",
	"rsi_not_supportive_long": "RSI nepotvrzuje preprodany long reverzni setup.",
	"rsi_not_supportive_short": "RSI nepotvrzuje prekoupene short reverzni setup.",
	"bullish_reversal_pattern_missing": "Na long chybi bullish reverzni svickovy pattern.",
	"bearish_reversal_pattern_missing": "Na short chybi bearish reverzni svickovy pattern.",
	"confirmation_close_too_weak": "Zaviraci cena nepotvrdila reverzni pattern dostatecne silne.",
	"symbol_not_in_quant_whitelist": "Symbol neni na whitelistu kvantitativni strategie.",
	"quant_score_below_threshold": "Matematicke score kandidata je pod minimem.",
	"adx_below_quant_threshold": "ADX je pod minimem pro kvantitativni vstup.",
	"quant_direction_conflict": "Smer obchodu neodpovida matematickemu score.",
	"quant_distance_too_small": "Cena je prilis blizko EMA20 a impuls neni dost silny.",
	"quant_body_too_small": "Cena je prilis blizko EMA20 a impuls neni dost silny.",
	"quant_rsi_not_supportive_long": "RSI nepotvrzuje long kvantitativni setup.",
	"quant_rsi_not_supportive_short": "RSI nepotvrzuje short kvantitativni setup.",
	"missing_indicator_data": "Chybi indikatorova data potrebna pro vyhodnoceni.",
	"open_position_exists": "Na tomto symbolu uz existuje otevrena pozice.",
	"recent_symbol_trade": "Na tomto symbolu uz probehl nedavny obchod.",
	"per_symbol_daily_limit": "Byl dosazen denni limit obchodu pro symbol.",
	"market_data_missing": "Chybi market data pro vyhodnoceni kandidata.",
	"trade_parameters_invalid": "Vypoctene parametry obchodu nejsou platne.",
	"trade_execution_failed": "Broker odmitl nebo nedokoncil exekuci obchodu.",
	"symbol_validation_failed": "Symbol neprosel kontrolou pred exekuci.",
}


def _translate_reason_cs(code: str) -> str:
	return REASON_TEXT_CS.get(code, code)


def _translate_rule_reason_cs(code: str) -> str:
	return RULE_REASON_TEXT_CS.get(code, code)


def _describe_candidate_source(candidate_source: str) -> Tuple[str, str]:
	normalized = str(candidate_source or "").strip()
	if not normalized:
		return "", ""
	if normalized.startswith("gemini_cached_advisory_candidate_"):
		return "gemini_cached", normalized.rsplit("_", 1)[-1]
	if normalized.startswith("gemini_live_advisory_candidate_"):
		return "gemini_live", normalized.rsplit("_", 1)[-1]
	if normalized == "gemini_cached_advisory":
		return "gemini_cached", "1"
	if normalized == "gemini_live_advisory":
		return "gemini_live", "1"
	if normalized == "local_prediction_ranking":
		return "local", ""
	if normalized.startswith("quant_signal_ranking_candidate_"):
		return "quant", normalized.rsplit("_", 1)[-1]
	if normalized == "quant_signal_ranking":
		return "quant", "1"
	return normalized, ""


def _extract_root_rejection_reason(details: Optional[Dict[str, object]]) -> str:
	if not isinstance(details, dict):
		return ""
	reason_value = str(details.get("reason", "") or "").strip()
	if not reason_value:
		return ""
	if ":" in reason_value:
		return reason_value.split(":", 1)[0].strip()
	return reason_value


def _extract_rule_failures(reason: str, details: Optional[Dict[str, object]]) -> List[str]:
	if not isinstance(details, dict):
		return []
	if reason == "signal_rejected":
		raw_codes = details.get("reason_codes")
		if isinstance(raw_codes, list):
			return [str(code).strip() for code in raw_codes if str(code).strip()]
	reason_value = str(details.get("reason", "") or "").strip()
	if reason_value.startswith("signal_rejected:"):
		raw_codes = reason_value.split(":", 1)[1]
		return [code.strip() for code in raw_codes.split(",") if code.strip()]
	return []


def _build_reason_summary(reason: str, details: Optional[Dict[str, object]]) -> str:
	root_rejection_reason = _extract_root_rejection_reason(details)
	rule_failures = _extract_rule_failures(reason, details)
	cooldown_expires_at = ""
	if isinstance(details, dict):
		cooldown_expires_at = str(details.get("expires_at", "") or "")

	summary_parts = [
		_translate_reason_cs(reason),
	]
	if root_rejection_reason:
		summary_parts.append(f"Puvodni duvod: {_translate_reason_cs(root_rejection_reason)}")
	if rule_failures:
		summary_parts.append(
			"Filtry ktere neprosly: " + "; ".join(_translate_rule_reason_cs(code) for code in rule_failures)
		)
	if cooldown_expires_at:
		summary_parts.append(f"Cooldown plati do {cooldown_expires_at}.")

	return " ".join(part for part in summary_parts if part)


def _build_strategy_status_row(base_row: Dict[str, str], details: Optional[Dict[str, object]]) -> Dict[str, str]:
	reason = base_row.get("reason", "")
	root_rejection_reason = _extract_root_rejection_reason(details)
	rule_failures = _extract_rule_failures(reason, details)
	cooldown_expires_at = ""
	if isinstance(details, dict):
		cooldown_expires_at = str(details.get("expires_at", "") or "")

	return {
		"timestamp": base_row.get("timestamp", ""),
		"strategy_id": base_row.get("strategy_id", ""),
		"strategy_label": base_row.get("strategy_label", ""),
		"symbol": base_row.get("symbol", ""),
		"action": base_row.get("action", ""),
		"candidate_source": base_row.get("candidate_source", ""),
		"candidate_queue": base_row.get("candidate_queue", ""),
		"candidate_rank": base_row.get("candidate_rank", ""),
		"stage": base_row.get("stage", ""),
		"trade_executed": base_row.get("trade_executed", ""),
		"reason": reason,
		"reason_cs": _translate_reason_cs(reason),
		"root_rejection_reason": root_rejection_reason,
		"root_rejection_reason_cs": _translate_reason_cs(root_rejection_reason) if root_rejection_reason else "",
		"rule_failures": ",".join(rule_failures),
		"rule_failures_cs": " | ".join(_translate_rule_reason_cs(code) for code in rule_failures),
		"cooldown_expires_at": cooldown_expires_at,
		"summary_cs": _build_reason_summary(reason, details),
		"details": base_row.get("details", ""),
	}


def _strategy_status_file_name(strategy_label: str) -> Optional[str]:
	if strategy_label in {"parallel", "reversal", "quant"}:
		return f"{strategy_label}_strategy_status.csv"
	return None


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


def _load_market_data_for_symbol(
	predictions_folder: Optional[Path],
	symbol: str,
	source_folder_override: Optional[Path] = None,
	service_folder_fallback: Optional[Path] = None,
) -> Optional[Dict]:
	if source_folder_override is not None:
		source_folder = source_folder_override
	elif predictions_folder is not None:
		source_folder = predictions_folder.parent / "source"
	elif service_folder_fallback is not None:
		source_folder = service_folder_fallback
	else:
		return None
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
	candidate_queue, candidate_rank = _describe_candidate_source(candidate_source)
	reason_cs = _translate_reason_cs(reason)
	summary_cs = _build_reason_summary(reason, details)
	rows_by_label: Dict[str, Dict[str, str]] = {}
	if log_file.exists():
		with open(log_file, "r", newline="", encoding="utf-8") as handle:
			for row in csv.DictReader(handle):
				label = str(row.get("strategy_label", "") or "").strip()
				if label:
					rows_by_label[label] = row

	existing_row = rows_by_label.get(strategy_label, {})
	transition_stage = str(existing_row.get("transition_stage", "") or "")
	transition_reason = str(existing_row.get("transition_reason", "") or "")
	transition_reason_cs = str(existing_row.get("transition_reason_cs", "") or "")
	transition_summary_cs = str(existing_row.get("transition_summary_cs", "") or "")
	transition_details = str(existing_row.get("transition_details", "") or "")
	if stage == "queue_transition":
		transition_stage = stage
		transition_reason = reason
		transition_reason_cs = reason_cs
		transition_summary_cs = summary_cs
		transition_details = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)

	rows_by_label[strategy_label] = {
		"timestamp": datetime.now(tz=timezone.utc).isoformat(),
		"strategy_id": strategy_id,
		"strategy_label": strategy_label,
		"symbol": symbol,
		"action": action,
		"candidate_source": candidate_source,
		"candidate_queue": candidate_queue,
		"candidate_rank": candidate_rank,
		"stage": stage,
		"trade_executed": str(trade_executed),
		"reason": reason,
		"reason_cs": reason_cs,
		"summary_cs": summary_cs,
		"transition_stage": transition_stage,
		"transition_reason": transition_reason,
		"transition_reason_cs": transition_reason_cs,
		"transition_summary_cs": transition_summary_cs,
		"transition_details": transition_details,
		"details": json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
	}

	with open(log_file, "w", newline="", encoding="utf-8") as handle:
		writer = csv.DictWriter(handle, fieldnames=TRADE_DECISION_SNAPSHOT_HEADERS)
		writer.writeheader()
		for label in sorted(rows_by_label):
			writer.writerow(rows_by_label[label])

		status_file_name = _strategy_status_file_name(strategy_label)
		if status_file_name:
			strategy_status_file = log_dir / status_file_name
			with open(strategy_status_file, "w", newline="", encoding="utf-8") as handle:
				writer = csv.DictWriter(handle, fieldnames=PARALLEL_STRATEGY_STATUS_HEADERS)
				writer.writeheader()
				writer.writerow(_build_strategy_status_row(rows_by_label[strategy_label], details))


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
	candidate_queue, candidate_rank = _describe_candidate_source(candidate_source)
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
				candidate_queue,
				candidate_rank,
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


def _get_strategy_activation_margin_percent(account_state: Dict) -> float:
	balance = float(account_state.get("balance", 0.0) or 0.0)
	if balance <= 0:
		return 0.0
	raw_free_margin = float(account_state.get("raw_margin_free", account_state.get("margin_free", 0.0)) or 0.0)
	return (raw_free_margin / balance) * 100.0


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
	source_folder: Optional[Path] = None


def _build_quant_ranked_candidates(source_folder: Path, service_folder: Path) -> List[RankedCandidate]:
	quant_candidates = build_quant_candidates(source_folder)
	ranked: List[RankedCandidate] = []
	for index, candidate in enumerate(quant_candidates, start=1):
		ranked.append(
			RankedCandidate(
				symbol=candidate.symbol,
				action=candidate.action,
				source=f"quant_signal_ranking_candidate_{index}",
				score=float(candidate.score),
			)
		)

	for index, candidate in enumerate(quant_candidates, start=1):
		_log_jsonl(
			service_folder,
			"quant_candidate_audit.jsonl",
			{
				"timestamp": datetime.now(tz=timezone.utc).isoformat(),
				"strategy_id": get_quant_strategy_context().strategy_id,
				"candidate_rank": index,
				"symbol": candidate.symbol,
				"action": candidate.action,
				"score": candidate.score,
				"metrics": candidate.metrics,
			},
		)
	return ranked


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


def _get_gemini_advisory_max_candidates() -> int:
	return _get_int_env("GEMINI_ADVISORY_MAX_CANDIDATES", 3, minimum=0)


def _candidate_key(symbol: str, action: str) -> Tuple[str, str]:
	return symbol.strip(), action.strip().upper()


def _extract_ranked_candidates_from_decision_payload(payload: Dict[str, object], source: str) -> List[RankedCandidate]:
	candidates: List[RankedCandidate] = []
	seen: set[Tuple[str, str]] = set()

	top_symbol = str(payload.get("recommended_symbol", "") or "").strip()
	top_action = str(payload.get("action", "") or "").strip().upper()
	if top_symbol and top_action:
		key = _candidate_key(top_symbol, top_action)
		seen.add(key)
		candidates.append(RankedCandidate(symbol=top_symbol, action=top_action, source=source, score=10_000_000.0))

	raw_candidates = payload.get("candidates")
	if isinstance(raw_candidates, list):
		for index, candidate in enumerate(raw_candidates, start=1):
			if not isinstance(candidate, dict):
				continue
			symbol = str(candidate.get("symbol", "") or candidate.get("recommended_symbol", "") or "").strip()
			action = str(candidate.get("action", "") or "").strip().upper()
			if not symbol or not action:
				continue
			key = _candidate_key(symbol, action)
			if key in seen:
				continue
			seen.add(key)
			candidates.append(
				RankedCandidate(
					symbol=symbol,
					action=action,
					source=f"{source}_candidate_{index}",
					score=10_000_000.0 - float(index),
				)
			)

	return candidates


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


def _build_candidate_queue(predictions: List[Dict], advisory_candidates: Optional[List[RankedCandidate]]) -> List[RankedCandidate]:
	ordered: List[RankedCandidate] = []
	seen: set[Tuple[str, str]] = set()

	for advisory_candidate in advisory_candidates or []:
		key = _candidate_key(advisory_candidate.symbol, advisory_candidate.action)
		if key in seen:
			continue
		ordered.append(advisory_candidate)
		seen.add(key)

	for candidate in _build_local_candidates(predictions):
		key = _candidate_key(candidate.symbol, candidate.action)
		if key in seen:
			continue
		ordered.append(candidate)
		seen.add(key)

	return ordered


def _log_candidate_queue(service_folder: Path, candidates: List[RankedCandidate]) -> None:
	queue_rows: List[Dict[str, object]] = []
	for index, candidate in enumerate(candidates, start=1):
		candidate_queue, candidate_rank = _describe_candidate_source(candidate.source)
		queue_rows.append(
			{
				"queue_index": index,
				"symbol": candidate.symbol,
				"action": candidate.action,
				"candidate_source": candidate.source,
				"candidate_queue": candidate_queue,
				"candidate_rank": candidate_rank,
				"score": candidate.score,
			}
		)

	_log_jsonl(
		service_folder,
		"decision_log.jsonl",
		{
			"timestamp": datetime.now(tz=timezone.utc).isoformat(),
			"event": "candidate_queue_built",
			"candidate_count": len(queue_rows),
			"candidates": queue_rows,
		},
	)


def _resolve_ollama_cloud_advisory_candidates(
	*,
	predictions: List[Dict],
	open_positions: List[Dict],
	account_state: Dict,
	service_folder: Path,
) -> List[RankedCandidate]:
	"""Ask cloud Ollama for candidate ranking (analogous to _resolve_gemini_advisory_candidates)."""
	if not predictions:
		return []

	from ollama_advisory import ask_ollama_final_decision as _ask_cloud

	max_candidates = _get_gemini_advisory_max_candidates()
	cache_signature = build_decision_signature(account_state, open_positions, predictions)
	cached_decision = get_cached_decision(service_folder, cache_signature, _get_gemini_decision_cache_minutes())
	if isinstance(cached_decision, dict):
		cached_source = cached_decision.get("_source", "")
		if str(cached_source) == "ollama_cloud":
			cached_candidates = _extract_ranked_candidates_from_decision_payload(
				cached_decision, "ollama_cloud_cached_advisory"
			)
			if cached_candidates:
				return cached_candidates[:max_candidates]

	decision_text = _ask_cloud(predictions, open_positions, account_state)
	if not decision_text:
		return []

	try:
		decision_payload = json.loads(decision_text)
	except json.JSONDecodeError:
		return []

	decision_payload["_source"] = "ollama_cloud"
	store_cached_decision(service_folder, cache_signature, decision_payload)

	live_candidates = _extract_ranked_candidates_from_decision_payload(
		decision_payload, "ollama_cloud_live_advisory"
	)
	return live_candidates[:max_candidates] if live_candidates else []


def _resolve_gemini_advisory_candidates(
	*,
	predictions: List[Dict],
	open_positions: List[Dict],
	account_state: Dict,
	service_folder: Path,
) -> List[RankedCandidate]:
	max_candidates = _get_gemini_advisory_max_candidates()
	if max_candidates == 0:
		print("ℹ️  Gemini advisory candidate limit is 0, using local candidate ranking only")
		return []

	cache_signature = build_decision_signature(account_state, open_positions, predictions)
	cached_decision = get_cached_decision(service_folder, cache_signature, _get_gemini_decision_cache_minutes())
	if isinstance(cached_decision, dict):
		cached_candidates = _extract_ranked_candidates_from_decision_payload(cached_decision, "gemini_cached_advisory")
		if cached_candidates:
			limited_cached_candidates = cached_candidates[:max_candidates]
			_log_jsonl(
				service_folder,
				"ai_log.jsonl",
				{
					"timestamp": datetime.now(tz=timezone.utc).isoformat(),
					"source": "gemini_cached_advisory",
					"signature": cache_signature,
					"recommended_symbol": limited_cached_candidates[0].symbol,
					"action": limited_cached_candidates[0].action,
					"candidate_count": len(limited_cached_candidates),
					"candidate_limit": max_candidates,
				},
			)
			return limited_cached_candidates

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
		return []

	_save_decision_text(service_folder, decision_text)
	try:
		decision_payload = json.loads(decision_text)
	except json.JSONDecodeError:
		return []
	store_cached_decision(service_folder, cache_signature, decision_payload)

	live_candidates = _extract_ranked_candidates_from_decision_payload(decision_payload, "gemini_live_advisory")
	if not live_candidates:
		return []
	limited_live_candidates = live_candidates[:max_candidates]

	_log_jsonl(
		service_folder,
		"ai_log.jsonl",
		{
			"timestamp": datetime.now(tz=timezone.utc).isoformat(),
			"source": "gemini_live_advisory",
			"signature": cache_signature,
			"recommended_symbol": limited_live_candidates[0].symbol,
			"action": limited_live_candidates[0].action,
			"candidate_count": len(limited_live_candidates),
			"candidate_limit": max_candidates,
		},
	)
	return limited_live_candidates


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
	predictions_folder: Optional[Path],
	service_folder: Path,
	account_state: Dict,
	open_positions: List[Dict],
	open_crypto_positions: int,
) -> bool:
	open_strategy_positions = count_open_positions_for_strategy(open_positions, profile.context)
	activation_margin_percent = _get_strategy_activation_margin_percent(account_state)
	if activation_margin_percent < profile.context.activation_margin_percent:
		print(
			f"⚠️  {profile.label} strategy activation threshold not met "
			f"({activation_margin_percent:.2f}% < {profile.context.activation_margin_percent:.2f}%), skipping"
		)
		_log_trade_decision_audit(
			service_folder,
			strategy_id=profile.context.strategy_id,
			strategy_label=profile.label,
			stage="strategy_blocked",
			trade_executed=False,
			reason="activation_margin_below_threshold",
			details={
				"activation_margin_percent": round(activation_margin_percent, 2),
				"required_margin_percent": profile.context.activation_margin_percent,
			},
		)
		return False
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
	gemini_attempts = 0
	local_fallback_logged = False

	for candidate in candidates:
		symbol = candidate.symbol
		action = candidate.action
		candidate_queue, candidate_rank = _describe_candidate_source(candidate.source)
		if candidate_queue.startswith("gemini"):
			gemini_attempts += 1
		elif candidate_queue == "local" and gemini_attempts > 0 and not local_fallback_logged:
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				symbol=symbol,
				action=action,
				candidate_source=candidate.source,
				stage="queue_transition",
				trade_executed=False,
				reason="gemini_candidates_exhausted_local_fallback",
				details={
					"failed_gemini_candidates": gemini_attempts,
					"local_candidate_rank": candidate_rank,
				},
			)
			local_fallback_logged = True
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

		market_data = _load_market_data_for_symbol(
			predictions_folder, symbol,
			source_folder_override=profile.source_folder,
			service_folder_fallback=service_folder,
		)
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


def make_final_trading_decision(predictions_folder: Optional[Path], service_folder: Path) -> bool:
	"""Make a final trading decision and execute a trade with limited retries."""
	print("\n" + "=" * 60)
	print("🎯 Final Trading Decision Phase")
	print("=" * 60)

	try:
		print("\n📊 Loading remaining predictions...")
		predictions = load_predictions(predictions_folder) if predictions_folder is not None else []

		# Load cloud Ollama predictions early – used both for the cloud strategy slot and
		# as a fallback candidate pool for secondary (parallel/reversal) strategies when
		# Gemini predictions are unavailable.
		cloud_preds_folder = service_folder / "ollama_cloud" / "predikce"
		cloud_predictions_loaded: List[Dict] = (
			load_predictions(cloud_preds_folder)
			if is_ollama_cloud_enabled() and cloud_preds_folder.exists()
			else []
		)

		# Quant reads raw market data – falls back to service_folder when predictions_folder
		# is None (e.g. cloud-only mode where Gemini was not called).
		if is_quant_strategy_enabled():
			quant_source = (
				predictions_folder.parent / "source"
				if predictions_folder is not None
				else service_folder
			)
			quant_candidates = _build_quant_ranked_candidates(quant_source, service_folder)
		else:
			quant_candidates = []

		print(
			f"   Found {len(predictions)} Gemini predictions "
			f"(standard >= {get_base_prediction_threshold():.0f}%, crypto >= {get_crypto_prediction_threshold():.0f}%)"
		)
		if cloud_predictions_loaded:
			print(f"   Found {len(cloud_predictions_loaded)} Cloud Ollama predictions")
		if quant_candidates:
			print(f"   Found {len(quant_candidates)} quant candidates from raw market data")
		if not predictions and not quant_candidates and not cloud_predictions_loaded:
			print("⚠️  No strong predictions available and quant strategy found no candidates")
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

		# ── Cloud Ollama strategy (runs first when enabled) ──────────────────────
		if is_ollama_cloud_enabled():
			cloud_predictions_folder = service_folder / "ollama_cloud" / "predikce"
			cloud_source_folder = service_folder / "ollama" / "source"
			cloud_predictions = cloud_predictions_loaded  # reuse already-loaded list
			print(f"\n☁️  Cloud Ollama: {len(cloud_predictions)} predikcí k dispozici")
			if cloud_predictions:
				cloud_advisory = _resolve_ollama_cloud_advisory_candidates(
					predictions=cloud_predictions,
					open_positions=open_positions,
					account_state=account_state,
					service_folder=service_folder,
				)
				cloud_queue = _build_candidate_queue(cloud_predictions, cloud_advisory)
				_log_candidate_queue(service_folder, cloud_queue)
				cloud_profile = StrategyExecutionProfile(
					label="ollama_cloud",
					context=get_ollama_cloud_strategy_context(),
					signal_validator=validate_trend_following_signal,
					risk_percent_env="OLLAMA_CLOUD_RISK_PER_TRADE_PERCENT",
					stop_atr_multiplier_env="OLLAMA_CLOUD_SYNTHETIC_STOP_ATR_MULTIPLIER",
					tp_r_multiple_env="OLLAMA_CLOUD_TAKE_PROFIT_R_MULTIPLIER",
					max_trades_per_day_env="OLLAMA_CLOUD_MAX_TRADES_PER_DAY",
					max_trades_per_symbol_per_day_env="OLLAMA_CLOUD_MAX_TRADES_PER_SYMBOL_PER_DAY",
					trade_cooldown_env="OLLAMA_CLOUD_SYMBOL_TRADE_COOLDOWN_MINUTES",
					default_trade_cooldown_minutes=15,
					source_folder=cloud_source_folder,
				)
				if cloud_queue and _attempt_strategy_trade(
					profile=cloud_profile,
					candidates=cloud_queue,
					predictions_folder=cloud_predictions_folder,
					service_folder=service_folder,
					account_state=account_state,
					open_positions=open_positions,
					open_crypto_positions=open_crypto_positions,
				):
					print("\n" + "=" * 60)
					print("✅ Final Trading Decision Completed (Cloud Ollama)")
					print("=" * 60)
					return True
			else:
				print("ℹ️  Cloud Ollama: žádné predikce, cloud slot přeskočen")
		# ── End cloud Ollama slot ─────────────────────────────────────────────────

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
		activation_margin_percent = _get_strategy_activation_margin_percent(account_state)
		primary_activation_met = activation_margin_percent >= primary_profile.context.activation_margin_percent
		parallel_activation_met = can_activate_parallel_strategy(account_state, open_positions)

		# Primary strategy depends on Gemini predictions – skip advisory and trade if unavailable.
		advisory_candidates: List[RankedCandidate] = []
		if primary_activation_met and predictions:
			advisory_candidates = _resolve_gemini_advisory_candidates(
				predictions=predictions,
				open_positions=open_positions,
				account_state=account_state,
				service_folder=service_folder,
			)
		elif primary_activation_met and not predictions:
			print("ℹ️  Primary (Gemini) strategy: no Gemini predictions available, skipping primary")
		else:
			print("ℹ️  Primary activation threshold not met, skipping Gemini advisory")

		# Candidate pool: Gemini-ranked when available; cloud Ollama predictions as fallback
		# for secondary strategies (parallel/reversal) when Gemini was not called.
		gemini_candidate_queue = _build_candidate_queue(predictions, advisory_candidates)
		if predictions:
			candidate_queue = gemini_candidate_queue
		elif cloud_predictions_loaded:
			print("ℹ️  No Gemini predictions – Cloud Ollama predictions used as secondary candidate pool")
			candidate_queue = _build_candidate_queue(cloud_predictions_loaded, [])
		else:
			candidate_queue = []
		_log_candidate_queue(service_folder, candidate_queue)

		# Primary trades only with Gemini predictions.
		if predictions and gemini_candidate_queue and _attempt_strategy_trade(
			profile=primary_profile,
			candidates=gemini_candidate_queue,
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

		secondary_profiles: List[Tuple[bool, StrategyExecutionProfile]] = [
			(
				parallel_activation_met,
				StrategyExecutionProfile(
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
				),
			),
		]
		if is_reversal_strategy_enabled():
			secondary_profiles.append(
				(
					can_activate_reversal_strategy(account_state, open_positions),
					StrategyExecutionProfile(
						label="reversal",
						context=get_reversal_strategy_context(),
						signal_validator=validate_reversal_pattern_signal,
						risk_percent_env="REVERSAL_RISK_PER_TRADE_PERCENT",
						stop_atr_multiplier_env="REVERSAL_SYNTHETIC_STOP_ATR_MULTIPLIER",
						tp_r_multiple_env="REVERSAL_TAKE_PROFIT_R_MULTIPLIER",
						max_trades_per_day_env="REVERSAL_MAX_TRADES_PER_DAY",
						max_trades_per_symbol_per_day_env="REVERSAL_MAX_TRADES_PER_SYMBOL_PER_DAY",
						trade_cooldown_env="REVERSAL_SYMBOL_TRADE_COOLDOWN_MINUTES",
						default_trade_cooldown_minutes=60,
						default_max_trades_per_symbol_per_day=1,
					),
				)
			)

		for activation_met, profile in secondary_profiles:
			if activation_met:
				if candidate_queue and _attempt_strategy_trade(
					profile=profile,
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
				continue

			print(f"⚠️  {profile.label.capitalize()} strategy activation gate not satisfied")
			_log_trade_decision_audit(
				service_folder,
				strategy_id=profile.context.strategy_id,
				strategy_label=profile.label,
				stage="strategy_blocked",
				trade_executed=False,
				reason="activation_gate_not_satisfied",
			)

		quant_profile = StrategyExecutionProfile(
			label="quant",
			context=get_quant_strategy_context(),
			signal_validator=validate_quant_signal,
			risk_percent_env="QUANT_RISK_PER_TRADE_PERCENT",
			stop_atr_multiplier_env="QUANT_SYNTHETIC_STOP_ATR_MULTIPLIER",
			tp_r_multiple_env="QUANT_TAKE_PROFIT_R_MULTIPLIER",
			max_trades_per_day_env="QUANT_MAX_TRADES_PER_DAY",
			max_trades_per_symbol_per_day_env="QUANT_MAX_TRADES_PER_SYMBOL_PER_DAY",
			trade_cooldown_env="QUANT_SYMBOL_TRADE_COOLDOWN_MINUTES",
			default_trade_cooldown_minutes=10,
			default_max_trades_per_day=8,
			default_max_trades_per_symbol_per_day=2,
		)
		quant_enabled = is_quant_strategy_enabled()
		quant_activation_met = quant_enabled and can_activate_quant_strategy(account_state, open_positions)
		if quant_activation_met and quant_candidates:
			if _attempt_strategy_trade(
				profile=quant_profile,
				candidates=quant_candidates,
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
		elif quant_enabled:
			reason = "no_quant_candidates" if quant_activation_met and not quant_candidates else "activation_gate_not_satisfied"
			_log_trade_decision_audit(
				service_folder,
				strategy_id=quant_profile.context.strategy_id,
				strategy_label=quant_profile.label,
				stage="strategy_blocked",
				trade_executed=False,
				reason=reason,
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
