from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def _state_path(service_folder: Path) -> Path:
	log_dir = service_folder / "trade_logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	return log_dir / "gemini_advisory_state.json"


def _parse_datetime(raw_value: object) -> Optional[datetime]:
	if not isinstance(raw_value, str) or not raw_value:
		return None
	try:
		parsed = datetime.fromisoformat(raw_value)
		if parsed.tzinfo is None:
			return parsed.replace(tzinfo=timezone.utc)
		return parsed.astimezone(timezone.utc)
	except ValueError:
		return None


def _load_state(service_folder: Path) -> Dict[str, object]:
	path = _state_path(service_folder)
	if not path.exists():
		return {"cache": {}, "rejections": []}
	try:
		loaded = json.loads(path.read_text(encoding="utf-8"))
		if isinstance(loaded, dict):
			loaded.setdefault("cache", {})
			loaded.setdefault("rejections", [])
			return loaded
	except (OSError, json.JSONDecodeError):
		pass
	return {"cache": {}, "rejections": []}


def _save_state(service_folder: Path, state: Dict[str, object]) -> None:
	_state_path(service_folder).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_account_state(account_state: Dict) -> Dict[str, float]:
	def _round_bucket(value: object, bucket: float) -> float:
		try:
			numeric = float(value or 0.0)
		except (TypeError, ValueError):
			numeric = 0.0
		if bucket <= 0:
			return round(numeric, 2)
		return round(round(numeric / bucket) * bucket, 2)

	return {
		"balance": _round_bucket(account_state.get("balance"), 50.0),
		"equity": _round_bucket(account_state.get("equity"), 50.0),
		"margin_free": _round_bucket(account_state.get("margin_free"), 50.0),
		"margin_percent": _round_bucket(account_state.get("margin_percent"), 1.0),
	}


def _normalize_positions(open_positions: Iterable[Dict]) -> List[Dict[str, object]]:
	normalized: List[Dict[str, object]] = []
	for position in open_positions:
		normalized.append(
			{
				"symbol": str(position.get("symbol", "") or ""),
				"type": str(position.get("type", "") or ""),
				"volume": round(float(position.get("volume", 0.0) or 0.0), 2),
				"magic": int(position.get("magic", 0) or 0),
				"comment": str(position.get("comment", "") or ""),
			}
		)
	return sorted(normalized, key=lambda item: (item["symbol"], item["type"], item["volume"], item["magic"], item["comment"]))


def _normalize_predictions(predictions: Iterable[Dict]) -> List[Dict[str, object]]:
	normalized: List[Dict[str, object]] = []
	for prediction in predictions:
		normalized.append(
			{
				"symbol": str(prediction.get("symbol", "") or ""),
				"BUY": round(float(prediction.get("BUY", 0.0) or 0.0), 2),
				"SELL": round(float(prediction.get("SELL", 0.0) or 0.0), 2),
				"HOLD": round(float(prediction.get("HOLD", 0.0) or 0.0), 2),
			}
		)
	return sorted(normalized, key=lambda item: item["symbol"])


def build_decision_signature(account_state: Dict, open_positions: Iterable[Dict], predictions: Iterable[Dict]) -> str:
	payload = {
		"account_state": _normalize_account_state(account_state),
		"open_positions": _normalize_positions(open_positions),
		"predictions": _normalize_predictions(predictions),
	}
	serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
	return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_cached_decision(service_folder: Path, signature: str, ttl_minutes: int) -> Optional[Dict[str, object]]:
	state = _load_state(service_folder)
	cache = state.get("cache", {})
	entry = cache.get(signature) if isinstance(cache, dict) else None
	if not isinstance(entry, dict):
		return None

	created_at = _parse_datetime(entry.get("created_at"))
	if created_at is None:
		return None
	if ttl_minutes > 0 and created_at < (datetime.now(tz=timezone.utc) - timedelta(minutes=ttl_minutes)):
		return None
	decision = entry.get("decision")
	if not isinstance(decision, dict):
		return None
	return decision


def store_cached_decision(service_folder: Path, signature: str, decision: Dict[str, object]) -> None:
	state = _load_state(service_folder)
	cache = state.setdefault("cache", {})
	if not isinstance(cache, dict):
		cache = {}
		state["cache"] = cache
	cache[signature] = {
		"created_at": datetime.now(tz=timezone.utc).isoformat(),
		"decision": decision,
	}
	_save_state(service_folder, state)


def record_rejection(
	service_folder: Path,
	*,
	strategy_id: str,
	symbol: str,
	action: str,
	reason: str,
	cooldown_minutes: int,
) -> None:
	state = _load_state(service_folder)
	rejections = state.setdefault("rejections", [])
	if not isinstance(rejections, list):
		rejections = []
		state["rejections"] = rejections

	now = datetime.now(tz=timezone.utc)
	expires_at = now + timedelta(minutes=max(cooldown_minutes, 0))
	rejections[:] = [
		entry
		for entry in rejections
		if not (
			isinstance(entry, dict)
			and str(entry.get("strategy_id", "")) == strategy_id
			and str(entry.get("symbol", "")) == symbol
			and str(entry.get("action", "")) == action
		)
	]
	rejections.append(
		{
			"strategy_id": strategy_id,
			"symbol": symbol,
			"action": action,
			"reason": reason,
			"created_at": now.isoformat(),
			"expires_at": expires_at.isoformat(),
		}
	)
	_save_state(service_folder, state)


def get_active_rejection(service_folder: Path, *, strategy_id: str, symbol: str, action: str) -> Optional[Dict[str, object]]:
	state = _load_state(service_folder)
	rejections = state.get("rejections", [])
	if not isinstance(rejections, list):
		return None

	now = datetime.now(tz=timezone.utc)
	active_entries: List[Dict[str, object]] = []
	found: Optional[Dict[str, object]] = None
	for entry in rejections:
		if not isinstance(entry, dict):
			continue
		expires_at = _parse_datetime(entry.get("expires_at"))
		if expires_at is None or expires_at <= now:
			continue
		active_entries.append(entry)
		if (
			str(entry.get("strategy_id", "")) == strategy_id
			and str(entry.get("symbol", "")) == symbol
			and str(entry.get("action", "")) == action
		):
			found = entry

	if len(active_entries) != len(rejections):
		state["rejections"] = active_entries
		_save_state(service_folder, state)

	return found