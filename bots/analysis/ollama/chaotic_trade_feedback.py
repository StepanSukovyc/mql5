"""Persistent outcome feedback for the TP-only chaotic strategy."""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import MetaTrader5 as mt5

from mt5_positions import get_open_positions


JOURNAL_FILE_NAME = "chaotic_decision_journal.jsonl"
MAX_REASONING_CHARS = 240


def is_chaotic_feedback_enabled() -> bool:
	return os.getenv("CHAOTIC_FEEDBACK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "y", "on"}


def _journal_file(service_folder: Path) -> Path:
	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	return log_dir / JOURNAL_FILE_NAME


def _append_event(service_folder: Path, event: Dict[str, object]) -> None:
	with open(_journal_file(service_folder), "a", encoding="utf-8") as handle:
		handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def _read_events(service_folder: Path) -> List[Dict[str, object]]:
	journal_file = _journal_file(service_folder)
	if not journal_file.exists():
		return []

	events: List[Dict[str, object]] = []
	for raw_line in journal_file.read_text(encoding="utf-8").splitlines():
		if not raw_line.strip():
			continue
		try:
			event = json.loads(raw_line)
		except json.JSONDecodeError:
			continue
		if isinstance(event, dict):
			events.append(event)
	return events


def _utc_now_iso() -> str:
	return datetime.now(tz=timezone.utc).isoformat()


def _short_text(value: object) -> str:
	return str(value or "").strip()[:MAX_REASONING_CHARS]


def record_chaotic_decision(
	service_folder: Path,
	*,
	symbol: str,
	action: str,
	candidate_source: str,
	decision_payload: Dict[str, object],
	prediction_snapshot: Optional[Dict[str, object]],
	account_state: Dict[str, object],
) -> str:
	"""Create an immutable journal entry and return its decision id."""
	decision_id = str(uuid.uuid4())
	_append_event(
		service_folder,
		{
			"event": "decision_created",
			"decision_id": decision_id,
			"timestamp_utc": _utc_now_iso(),
			"strategy_id": "chaotic",
			"symbol": symbol,
			"action": action,
			"candidate_source": candidate_source,
			"ollama_reasoning": _short_text(decision_payload.get("reasoning")),
			"feedback_assessment": _short_text(decision_payload.get("feedback_assessment")),
			"feedback_influenced_decision": bool(decision_payload.get("feedback_influenced_decision", False)),
			"prediction_snapshot": prediction_snapshot or {},
			"account_snapshot": {
				"balance": account_state.get("balance"),
				"equity": account_state.get("equity"),
				"free_margin_percent": account_state.get("margin_percent"),
			},
			"feedback_version": 1,
		},
	)
	return decision_id


def record_chaotic_position_opened(
	service_folder: Path,
	*,
	decision_id: str,
	position_ticket: Optional[int],
	order_ticket: Optional[int],
	deal_ticket: Optional[int],
	filled_price: Optional[float],
	filled_volume: Optional[float],
) -> None:
	"""Link a broker-confirmed open order to the previous decision."""
	_append_event(
		service_folder,
		{
			"event": "position_opened",
			"decision_id": decision_id,
			"timestamp_utc": _utc_now_iso(),
			"position_ticket": position_ticket,
			"order_ticket": order_ticket,
			"deal_ticket": deal_ticket,
			"filled_price": filled_price,
			"filled_volume": filled_volume,
		},
	)


def _parse_timestamp(raw_value: object) -> Optional[datetime]:
	try:
		parsed = datetime.fromisoformat(str(raw_value))
	except (TypeError, ValueError):
		return None
	return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _deal_timestamp(deal: Any) -> Optional[datetime]:
	time_msc = int(getattr(deal, "time_msc", 0) or 0)
	if time_msc > 0:
		return datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc)
	time_seconds = int(getattr(deal, "time", 0) or 0)
	return datetime.fromtimestamp(time_seconds, tz=timezone.utc) if time_seconds > 0 else None


def _is_close_deal(deal: Any) -> bool:
	return int(getattr(deal, "entry", -1) or -1) in {
		getattr(mt5, "DEAL_ENTRY_OUT", -1004),
		getattr(mt5, "DEAL_ENTRY_OUT_BY", -1005),
		getattr(mt5, "DEAL_ENTRY_INOUT", -1006),
	}


def _close_reason(deal: Any) -> str:
	reason = int(getattr(deal, "reason", -1) or -1)
	if reason == getattr(mt5, "DEAL_REASON_TP", -1001):
		return "take_profit"
	if reason == getattr(mt5, "DEAL_REASON_SL", -1002):
		return "stop_loss"
	if reason == getattr(mt5, "DEAL_REASON_EXPERT", -1003):
		return "expert"
	if reason == getattr(mt5, "DEAL_REASON_CLIENT", -1004):
		return "manual"
	return "mt5_history"


def reconcile_chaotic_trade_outcomes(
	service_folder: Path,
	*,
	open_positions: Optional[Iterable[Dict[str, object]]] = None,
	history_deals_get: Optional[Callable[[datetime, datetime], Optional[Iterable[Any]]]] = None,
	now_utc: Optional[datetime] = None,
) -> int:
	"""Append final outcomes for journaled chaotic positions that have closed."""
	if not is_chaotic_feedback_enabled():
		return 0
	now = now_utc or datetime.now(tz=timezone.utc)
	events = _read_events(service_folder)
	opened = {str(event.get("decision_id")): event for event in events if event.get("event") == "position_opened"}
	closed_ids = {str(event.get("decision_id")) for event in events if event.get("event") == "position_closed"}
	open_ticket_ids = {str(position.get("ticket")) for position in (open_positions if open_positions is not None else get_open_positions())}
	history_getter = history_deals_get or mt5.history_deals_get
	created = 0

	for decision_id, opened_event in opened.items():
		if decision_id in closed_ids:
			continue
		position_ticket = opened_event.get("position_ticket") or opened_event.get("order_ticket")
		if position_ticket is None or str(position_ticket) in open_ticket_ids:
			continue
		opened_at = _parse_timestamp(opened_event.get("timestamp_utc"))
		if opened_at is None:
			continue
		deals = history_getter(opened_at, now)
		if deals is None:
			continue
		close_deals = [
			deal for deal in deals
			if str(getattr(deal, "position_id", getattr(deal, "position", ""))) == str(position_ticket) and _is_close_deal(deal)
		]
		if not close_deals:
			continue
		close_deals.sort(key=lambda deal: _deal_timestamp(deal) or opened_at)
		latest_deal = close_deals[-1]
		closed_at = _deal_timestamp(latest_deal) or now
		profit = sum(float(getattr(deal, "profit", 0.0) or 0.0) for deal in close_deals)
		swap = sum(float(getattr(deal, "swap", 0.0) or 0.0) for deal in close_deals)
		commission = sum(float(getattr(deal, "commission", 0.0) or 0.0) for deal in close_deals)
		net_profit = round(profit + swap + commission, 2)
		outcome = "win" if net_profit > 0 else "loss" if net_profit < 0 else "flat"
		_append_event(
			service_folder,
			{
				"event": "position_closed",
				"decision_id": decision_id,
				"position_ticket": int(position_ticket),
				"closed_at_utc": closed_at.isoformat(),
				"close_price": float(getattr(latest_deal, "price", 0.0) or 0.0),
				"gross_profit": round(profit, 2),
				"swap": round(swap, 2),
				"commission": round(commission, 2),
				"net_profit": net_profit,
				"outcome": outcome,
				"holding_minutes": round((closed_at - opened_at).total_seconds() / 60, 1),
				"close_reason": _close_reason(latest_deal),
				"close_reason_source": "mt5_history",
			},
		)
		created += 1
	return created


def build_chaotic_recent_feedback(service_folder: Path, *, limit: int = 7) -> Dict[str, object]:
	"""Return a bounded advisory-only summary of the latest closed chaotic trades."""
	limit = max(0, limit)
	events = _read_events(service_folder)
	decisions = {str(event.get("decision_id")): event for event in events if event.get("event") == "decision_created"}
	outcomes = [event for event in events if event.get("event") == "position_closed" and str(event.get("decision_id")) in decisions]
	outcomes.sort(key=lambda event: str(event.get("closed_at_utc", "")), reverse=True)
	recent_outcomes = outcomes[:limit]
	recent_trades: List[Dict[str, object]] = []
	for outcome in recent_outcomes:
		decision = decisions[str(outcome["decision_id"])]
		recent_trades.append(
			{
				"closed_at_utc": outcome.get("closed_at_utc"),
				"symbol": decision.get("symbol"),
				"action": decision.get("action"),
				"prediction": decision.get("prediction_snapshot", {}),
				"net_profit": outcome.get("net_profit"),
				"outcome": outcome.get("outcome"),
				"holding_minutes": outcome.get("holding_minutes"),
				"close_reason": outcome.get("close_reason"),
				"decision_reasoning": _short_text(decision.get("ollama_reasoning")),
			}
		)

	counts = Counter(str(trade["outcome"]) for trade in recent_trades)
	max_loss_streak = 0
	current_loss_streak = 0
	for trade in reversed(recent_trades):
		if trade["outcome"] == "loss":
			current_loss_streak += 1
			max_loss_streak = max(max_loss_streak, current_loss_streak)
		else:
			current_loss_streak = 0
	wins = counts["win"]
	closed_count = len(recent_trades)
	return {
		"window": {
			"closed_trades": closed_count,
			"wins": wins,
			"losses": counts["loss"],
			"flat": counts["flat"],
			"net_profit_total": round(sum(float(trade["net_profit"] or 0.0) for trade in recent_trades), 2),
			"win_rate_percent": round((wins / closed_count) * 100, 1) if closed_count else 0.0,
			"max_consecutive_losses": max_loss_streak,
		},
		"recent_trades": recent_trades,
		"guardrails": {
			"feedback_is_advisory": True,
			"do_not_infer_causality_from_small_sample": True,
		},
	}