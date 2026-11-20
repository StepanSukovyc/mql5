"""Read-only comparison of the loss fallback with a public-market Gemini signal."""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, TypedDict

import MetaTrader5 as mt5

from account_state import get_account_state, get_effective_free_margin
from gemini_config import load_gemini_api_config
from gemini_vertex import RotationMarketScore, request_rotation_market_scores
from instrument_utils import is_symbol_blacklisted
from mt5_symbols import estimate_order_profit, get_symbol_info, get_symbol_tick
from risk_engine import calculate_synthetic_risk_plan
from signal_rules import validate_trend_following_signal
from swap_rollover import PRAGUE_TZ, get_prague_entry_block_end_utc, get_swap_block_window
from trading_validation import validate_lot_size

if TYPE_CHECKING:
	from position_rotation_strategy import PositionSnapshot


_PUBLIC_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "XAUUSD", "XAGUSD")
_LOG = "position_rotation_shadow.jsonl"
_PENDING = "position_rotation_shadow_pending.json"
_HORIZONS = (1, 3)
_SAMPLE_GRACE = timedelta(minutes=5)
_LOCK = threading.Lock()
_FETCH_LOCK = threading.Lock()
_PENDING_LOCK = threading.Lock()
_requested: tuple[Path, str] | None = None
_signal: tuple[tuple[Path, str], datetime, list[RotationMarketScore]] | None = None


class Observation(TypedDict):
	ticket: int | None
	symbol: str
	action: str
	volume: float
	net_profit: float
	baseline_price: float | None
	standalone_margin: float | None


class PendingObservation(TypedDict):
	window_key_prague: str
	observed_at: str
	hour: int
	positions: list[Observation]


class Replacement(TypedDict):
	close_ticket: int
	entry: Observation
	entry_exit_bid_ask_price: float
	estimated_fees_and_slippage: float
	estimated_post_close_margin: float
	estimated_target_pnl: float
	estimated_stop_pnl: float
	capacity_block: str


def _log(folder: Path, now: datetime, event: str, **details: object) -> None:
	path = folder / "trade_logs" / _LOG
	path.parent.mkdir(parents=True, exist_ok=True)
	with _LOCK, path.open("a", encoding="utf-8") as handle:
		handle.write(json.dumps({"timestamp_utc": now.isoformat(), "event": event, **details}, allow_nan=False) + "\n")


def _load_pending(folder: Path) -> list[PendingObservation]:
	path = folder / "trade_logs" / _PENDING
	if not path.exists():
		return []
	return json.loads(path.read_text(encoding="utf-8"))


def _save_pending(folder: Path, pending: list[PendingObservation]) -> None:
	path = folder / "trade_logs" / _PENDING
	tmp = path.with_suffix(".tmp")
	tmp.write_text(json.dumps(pending, allow_nan=False), encoding="utf-8")
	tmp.replace(path)


def _window_key(now: datetime) -> str:
	return now.astimezone(PRAGUE_TZ).strftime("%Y-%m-%d-%H%z")


def _rotation_key(now: datetime) -> str:
	return now.astimezone(PRAGUE_TZ).strftime("%Y-%m-%d-%H")


def _finite_price(value: object) -> float | None:
	if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
		return None
	return float(value)


def _fresh_tick(symbol: str, now: datetime, tick_time_to_utc: Callable[[float], datetime]):
	tick = get_symbol_tick(symbol)
	if tick is None:
		return None
	try:
		age = now - tick_time_to_utc(float(tick.time))
	except (AttributeError, TypeError, ValueError, OverflowError, OSError):
		return None
	return tick if timedelta(seconds=-5) <= age <= timedelta(minutes=2) else None


def _public_features(now: datetime, tick_time_to_utc: Callable[[float], datetime]) -> dict[str, dict[str, list[float]]]:
	features = {}
	for symbol in _PUBLIC_SYMBOLS:
		broker_symbol = f"{symbol}_ecn"
		if _fresh_tick(broker_symbol, now, tick_time_to_utc) is None:
			continue
		frames = {}
		for name, timeframe in (("H1", mt5.TIMEFRAME_H1), ("H4", mt5.TIMEFRAME_H4)):
			bars = mt5.copy_rates_from_pos(broker_symbol, timeframe, 1, 13)
			if bars is None or len(bars) != 13:
				break
			closes = bars["close"]
			if any(not math.isfinite(price) or price <= 0 for price in closes):
				break
			frames[name] = [round(100 * (float(end) / float(start) - 1), 4) for start, end in zip(closes, closes[1:])]
		if len(frames) == 2:
			features[symbol] = frames
	return features


def _fetch_signal(
	folder: Path, now: datetime, key: tuple[Path, str], tick_time_to_utc: Callable[[float], datetime],
) -> None:
	global _signal
	try:
		features = _public_features(now, tick_time_to_utc)
		if not features:
			raise RuntimeError("No current data for the fixed public market universe")
		scores = request_rotation_market_scores(load_gemini_api_config(), features)
		finished = datetime.now(timezone.utc)
		_log(folder, finished, "market_scores", window_key_prague=_rotation_key(now), features=features, scores=scores)
		with _LOCK:
			_signal = (key, finished, scores)
	except (OSError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
		try:
			_log(folder, datetime.now(timezone.utc), "market_error", window_key_prague=_rotation_key(now), error=str(exc))
		except OSError as log_error:
			print(f"Position rotation shadow market error: {exc}; audit write failed: {log_error}")
	finally:
		_FETCH_LOCK.release()


def maintain(folder: Path, now: datetime, tick_time_to_utc: Callable[[float], datetime]) -> None:
	global _requested
	key = (folder, _window_key(now))
	# This schedule is independent of positions, fallback eligibility and account P/L.
	if 20 <= now.astimezone(PRAGUE_TZ).minute <= 24 and key != _requested and _FETCH_LOCK.acquire(blocking=False):
		_requested = key
		try:
			threading.Thread(target=_fetch_signal, args=(folder, now, key, tick_time_to_utc), daemon=True).start()
		except RuntimeError:
			_FETCH_LOCK.release()
			raise
	with _PENDING_LOCK:
		_sample_due(folder, now, tick_time_to_utc)


def _observe(candidate: PositionSnapshot, now: datetime, convert: Callable[[float], datetime]) -> Observation:
	buy = candidate.position_type == mt5.POSITION_TYPE_BUY
	action = "BUY" if buy else "SELL"
	tick = _fresh_tick(candidate.symbol, now, convert)
	price = _finite_price(getattr(tick, "bid" if buy else "ask", None))
	margin = None
	if price is not None:
		order_type = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL
		open_price = _finite_price(getattr(tick, "ask" if buy else "bid", None))
		if open_price is not None:
			margin = mt5.order_calc_margin(order_type, candidate.symbol, candidate.volume, open_price)
	return {
		"ticket": candidate.ticket, "symbol": candidate.symbol, "action": action,
		"volume": candidate.volume, "net_profit": candidate.net_profit,
		"baseline_price": price, "standalone_margin": _finite_price(margin),
	}


def _fresh_entry_data(folder: Path, symbol: str, now: datetime) -> dict | None:
	for root in (folder / "ollama" / "source", folder / "source", folder):
		path = root / f"{symbol}.json"
		try:
			data = json.loads(path.read_text(encoding="utf-8"))
		except (FileNotFoundError, json.JSONDecodeError):
			continue
		if not isinstance(data, dict) or data.get("symbol") != symbol:
			continue
		stamp = data.get("generated_at")
		if not isinstance(stamp, str):
			continue
		try:
			generated = datetime.fromisoformat(stamp)
		except ValueError:
			continue
		if generated.tzinfo is not None and timedelta(0) <= now - generated <= timedelta(minutes=15):
			return data
	return None


def _plan_replacement(
	folder: Path, now: datetime, outgoing: Observation, scores: list[RotationMarketScore],
	open_symbols: tuple[str, ...], position_cap: int, convert: Callable[[float], datetime],
) -> tuple[Replacement | None, list[dict[str, str]]]:
	rejections: list[dict[str, str]] = []
	if get_prague_entry_block_end_utc(now) is not None or get_swap_block_window(now_utc=now).contains(now):
		return None, [{"reason": "entry_time_blocked"}]
	if len(open_symbols) - 1 >= position_cap:
		return None, [{"reason": "one_close_does_not_free_slot"}]

	account = get_account_state()
	free = account["margin_free"]
	needs_slot = len(open_symbols) >= position_cap
	close_cost = round(outgoing["volume"] / 0.01 * 0.10, 2)
	# Standalone margin is not portfolio margin. Do not count it for stacked/hedged symbols.
	released = outgoing["standalone_margin"] if open_symbols.count(outgoing["symbol"]) == 1 else None
	for row in sorted(scores, key=lambda score: -abs(score["score"])):
		symbol = f'{row["symbol"]}_ecn'
		action = "BUY" if row["score"] > 0 else "SELL"

		def reject(reason: str) -> None:
			rejections.append({"symbol": symbol, "action": action, "reason": reason})

		if not row["score"] or symbol in open_symbols or is_symbol_blacklisted(symbol):
			reject("neutral_open_or_blacklisted")
			continue
		data = _fresh_entry_data(folder, symbol, now)
		if data is None or not validate_trend_following_signal(symbol, action, data).allowed:
			reject("missing_fresh_data_or_signal_rejected")
			continue
		tick = _fresh_tick(symbol, now, convert)
		bid = _finite_price(getattr(tick, "bid", None))
		ask = _finite_price(getattr(tick, "ask", None))
		info = get_symbol_info(symbol)
		if bid is None or ask is None or ask < bid or info is None or info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
			reject("missing_quote_or_symbol_not_tradable")
			continue
		plan = calculate_synthetic_risk_plan(symbol=symbol, action=action, account_state=account, market_data=data)
		if plan is None or _finite_price(plan.lot_size) is None:
			reject("risk_plan_unavailable")
			continue
		volume, _ = validate_lot_size(symbol, plan.lot_size)
		if _finite_price(volume) is None:
			reject("invalid_order_volume")
			continue
		entry_price, exit_price = (ask, bid) if action == "BUY" else (bid, ask)
		order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
		risk = estimate_order_profit(symbol, action, volume, entry_price, plan.synthetic_stop_price)
		margin = _finite_price(mt5.order_calc_margin(order_type, symbol, volume, entry_price))
		spread_pnl = estimate_order_profit(symbol, action, volume, entry_price, exit_price)
		entry_cost = round(volume / 0.01 * 0.10, 2)
		if (
			risk is None or not math.isfinite(risk) or not -plan.risk_usd <= risk < 0
			or margin is None or spread_pnl is None or not math.isfinite(spread_pnl) or spread_pnl > 0
		):
			reject("risk_margin_or_cost_unavailable")
			continue
		cost = close_cost + 2 * entry_cost - spread_pnl  # entry/exit fee plus one spread of slippage reserve
		needs_margin = margin > free
		if not needs_slot and not needs_margin:
			reject("entry_possible_without_rotation")
			continue
		post_balance = account["raw_balance"] + outgoing["net_profit"] - cost
		if post_balance <= 0:
			reject("insufficient_projected_balance")
			continue
		post_margin = get_effective_free_margin(
			post_balance, account["raw_margin_free"] + (released or 0) - cost,
		)
		if post_margin < margin:
			reject("estimated_margin_after_close_insufficient")
			continue
		# A target is a cost hurdle, not an expected return. The shadow followup measures the actual move.
		target = estimate_order_profit(symbol, action, volume, entry_price, plan.take_profit_price)
		if target is None or not math.isfinite(target) or target <= cost + abs(spread_pnl):
			reject("modeled_target_does_not_cover_costs")
			continue
		ticket = outgoing["ticket"]
		assert ticket is not None
		return {
			"close_ticket": ticket, "estimated_post_close_margin": post_margin,
			"capacity_block": "slots_and_margin" if needs_slot and needs_margin else "slots" if needs_slot else "margin",
			"entry_exit_bid_ask_price": exit_price, "estimated_fees_and_slippage": round(cost, 2),
			"estimated_target_pnl": round(target - cost, 2),
			"estimated_stop_pnl": round(risk - cost, 2),
			"entry": {
				"ticket": None, "symbol": symbol, "action": action, "volume": volume,
				"net_profit": -round(cost, 2), "baseline_price": entry_price, "standalone_margin": margin,
			},
		}, rejections
	return None, rejections


def record_fallback(
	folder: Path, now: datetime, candidates: list[PositionSnapshot], baseline_count: int,
	dry_run: bool, convert: Callable[[float], datetime],
	*, open_symbols: tuple[str, ...] = (), position_cap: int = 0,
) -> None:
	if not 30 <= now.astimezone(PRAGUE_TZ).minute < 35:
		return
	with _LOCK:
		signal = _signal
	scores = signal[2] if signal is not None and signal[0] == (folder, _window_key(now)) and signal[1] <= now else []
	market = {row["symbol"]: row for row in scores}
	ranked = []
	for candidate in candidates:
		score = market.get(candidate.symbol.removesuffix("_ecn"))
		if score is None:
			continue
		if candidate.position_type == mt5.POSITION_TYPE_BUY:
			adverse = -score["score"]
		elif candidate.position_type == mt5.POSITION_TYPE_SELL:
			adverse = score["score"]
		else:
			continue
		ranked.append((adverse, candidate, score["reasoning"]))
	worst = max(ranked, key=lambda row: (row[0], -row[1].net_profit, -row[1].ticket), default=None)
	proposed = worst[1] if worst is not None and worst[0] > 0 else None
	baseline = candidates[:baseline_count]
	compared = {candidate.ticket: candidate for candidate in baseline}
	if proposed is not None:
		compared[proposed.ticket] = proposed
	observations = [_observe(candidate, now, convert) for candidate in compared.values()]
	replacement = None
	rejections: list[dict[str, str]] = []
	if proposed is not None and open_symbols and position_cap:
		outgoing = next(item for item in observations if item["ticket"] == proposed.ticket)
		if outgoing["baseline_price"] is not None:
			try:
				replacement, rejections = _plan_replacement(folder, now, outgoing, scores, open_symbols, position_cap, convert)
			except (OSError, RuntimeError, TypeError, ValueError, OverflowError, KeyError, AttributeError) as exc:
				rejections = [{"reason": "replacement_unavailable", "error": str(exc)}]
		else:
			rejections = [{"reason": "missing_close_quote"}]
	key = _rotation_key(now)
	_log(
		folder, now, "comparison", window_key_prague=key, dry_run=dry_run,
		status="selected" if proposed else ("hold" if ranked else ("not_covered" if scores else "no_market_scores")),
		baseline_one=baseline[0].ticket, baseline_rotation=[item.ticket for item in baseline],
		gemini_ticket=proposed.ticket if proposed else None,
		shadow_decision="swap" if replacement else "keep",
		replacement=replacement, entry_rejections=rejections,
		adverse_scores=[{"ticket": item.ticket, "adverse_score": score, "reasoning": reasoning}
			for score, item, reasoning in ranked],
		eligible=[{"ticket": item.ticket, "symbol": item.symbol, "net_profit": item.net_profit}
			for item in candidates],
		positions=observations,
	)
	with _PENDING_LOCK:
		pending = _load_pending(folder)
		pending.extend({
			"window_key_prague": key, "observed_at": now.isoformat(), "hour": hour,
			"positions": observations + ([replacement["entry"]] if replacement else []),
		} for hour in _HORIZONS)
		_save_pending(folder, pending)


def _sample_due(folder: Path, now: datetime, convert: Callable[[float], datetime]) -> None:
	pending = _load_pending(folder)
	remaining = []
	changed = False
	for item in pending:
		due = datetime.fromisoformat(item["observed_at"]) + timedelta(hours=item["hour"])
		if now < due:
			remaining.append(item)
			continue
		rows = []
		waiting = []
		for position in item["positions"]:
			start = position["baseline_price"]
			price = None
			pnl_change = None
			if now > due + _SAMPLE_GRACE:
				status = "missed"
			elif start is None:
				status = "missing_baseline_quote"
			else:
				tick = _fresh_tick(position["symbol"], now, convert)
				price = _finite_price(getattr(tick, "bid" if position["action"] == "BUY" else "ask", None))
				if price is None and now < due + _SAMPLE_GRACE:
					waiting.append(position)
					continue
				if price is not None:
					pnl_change = estimate_order_profit(
						position["symbol"], position["action"], position["volume"], start, price,
					)
					if pnl_change is not None and not math.isfinite(pnl_change):
						pnl_change = None
				status = "marked" if pnl_change is not None else ("no_quote" if price is None else "pnl_unavailable")
			rows.append({
				"ticket": position["ticket"], "symbol": position["symbol"], "status": status, "exit_side_price": price,
				"estimated_pnl_change": pnl_change,
				"estimated_net_if_held": round(position["net_profit"] + pnl_change, 2) if pnl_change is not None else None,
			})
		if rows:
			_log(folder, now, "followup", window_key_prague=item["window_key_prague"], hour=item["hour"],
				observed_at=item["observed_at"], due_utc=due.isoformat(), positions=rows)
			changed = True
		if waiting:
			remaining.append({**item, "positions": waiting})
	if changed:
		_save_pending(folder, remaining)
