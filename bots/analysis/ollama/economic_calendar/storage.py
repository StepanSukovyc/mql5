from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def append_audit_jsonl(service_folder: Path, event_type: str, payload: dict[str, Any]) -> None:
    path = service_folder / "trade_logs" / "economic_calendar_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "event_type": event_type, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_filter_audit(service_folder: Path, row: dict[str, Any]) -> None:
    path = service_folder / "trade_logs" / "economic_calendar_filter.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = ["timestamp_utc", "symbol", "mapping_status", "calendar_status", "risk_level", "allow_new_trade", "would_block", "event_id", "value_id", "event_name", "currency", "importance", "event_time_utc", "minutes_to_event", "decision", "reason_code"]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in headers})