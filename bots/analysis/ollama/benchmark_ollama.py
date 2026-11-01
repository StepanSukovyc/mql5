"""Compare cloud prediction responses on frozen inputs without trading."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

import httpx

from logika import _load_dotenv
from ollama_service import (
    _build_compact_market_data_summary,
    _build_ollama_prompt,
    _extract_json_from_text,
    get_ollama_cloud_api_key,
    get_ollama_cloud_num_ctx,
    get_ollama_cloud_timeout_seconds,
    get_ollama_cloud_url,
    get_symbol_prompt_guidance,
)


def validate_prediction(text: str, symbol: str) -> dict[str, Any]:
    parsed = _extract_json_from_text(text)
    if not isinstance(parsed, dict):
        raise ValueError("Response does not contain a JSON object")
    prediction = parsed.get(symbol, parsed)
    if not isinstance(prediction, dict):
        raise ValueError("Prediction is not an object")
    for name in ("BUY", "SELL", "HOLD"):
        value = prediction.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError(f"{name} must be finite and between 0 and 100")
    if not math.isclose(sum(prediction[k] for k in ("BUY", "SELL", "HOLD")), 100, abs_tol=0.1):
        raise ValueError("BUY + SELL + HOLD must sum to 100 (tolerance 0.1)")
    if not isinstance(prediction.get("reasoning"), str) or not prediction["reasoning"].strip():
        raise ValueError("reasoning must be a nonempty string")
    if prediction.get("symbol", symbol) != symbol:
        raise ValueError("Response symbol does not match input")
    return prediction


def build_prompt(symbol: str, data: dict[str, Any]) -> str:
    summary = {}
    for timeframe, oscillators in data.get("oscillators", {}).items():
        summary[timeframe] = {
            name: values[-1]["value"] if values else None
            for name, values in (
                ("rsi", oscillators.get("rsi", [])),
                ("ma", oscillators.get("ma", [])),
            )
        }
    return _build_ollama_prompt(
        symbol, data.get("current_price"), summary,
        _build_compact_market_data_summary(data),
        get_symbol_prompt_guidance(symbol), True,
        {"fundamental_data_status": "NO_DATA"},
    )


def action(prediction: dict[str, Any]) -> str:
    best = max(prediction[k] for k in ("BUY", "SELL", "HOLD"))
    winners = [k for k in ("BUY", "SELL", "HOLD") if prediction[k] == best]
    return winners[0] if len(winners) == 1 else "TIE"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--models", nargs=2, default=["gpt-oss:120b", "deepseek-v4-pro:0813-cloud"])
    args = parser.parse_args()
    if args.limit < 1 or args.repeats < 1 or args.models[0] == args.models[1]:
        parser.error("Use positive limit/repeats and two different models")
    _load_dotenv(Path(__file__).with_name(".env"))
    files = sorted(args.source.glob("*.json"))[:args.limit]
    if not files:
        parser.error("Source contains no JSON inputs")
    key = get_ollama_cloud_api_key()
    if not key:
        parser.error("OLLAMA_CLOUD_API_KEY is missing")
    inputs = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("candles"):
            parser.error(f"Missing market candles in {path.name}")
        if data.get("symbol", path.stem) != path.stem:
            parser.error(f"Symbol mismatch in {path.name}")
        inputs.append((path.stem, data, build_prompt(path.stem, data)))
    directory = Path(__file__).parent / "benchmarks" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "inputs.json").write_text(
        json.dumps([{"symbol": s, "data": d, "prompt": p} for s, d, p in inputs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    counts: dict[str, Counter] = {model: Counter() for model in args.models}
    latencies: dict[str, list[float]] = {model: [] for model in args.models}
    decisions: dict[tuple[str, int], dict[str, str]] = {}
    failures = 0
    print(f"{len(inputs) * 2 * args.repeats} API requests planned; allowance will be consumed.")
    print(f"Output: {directory}")
    with (directory / "responses.jsonl").open("w", encoding="utf-8") as output:
        with httpx.Client(timeout=get_ollama_cloud_timeout_seconds()) as client:
            for index, (symbol, _data, prompt) in enumerate(inputs):
                for repeat in range(args.repeats):
                    models = args.models if (index + repeat) % 2 == 0 else list(reversed(args.models))
                    for model in models:
                        row: dict[str, Any] = {
                            "symbol": symbol, "model": model, "repeat": repeat + 1,
                            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                        started = time.perf_counter()
                        try:
                            response = client.post(
                                get_ollama_cloud_url(),
                                headers={"Authorization": f"Bearer {key}"},
                                json={
                                    "model": model, "prompt": prompt, "stream": False,
                                    "options": {"num_ctx": get_ollama_cloud_num_ctx(), "temperature": 0.7, "top_p": 0.9},
                                },
                            )
                            response.raise_for_status()
                            payload = response.json()
                            row["response"] = payload
                            prediction = validate_prediction(payload.get("response", ""), symbol)
                            row["prediction"] = prediction
                            row["action"] = action(prediction)
                            counts[model][row["action"]] += 1
                            decisions.setdefault((symbol, repeat), {})[model] = row["action"]
                            row["status"] = "ok"
                        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                            row["status"] = "error"
                            row["error"] = str(exc)
                            failures += 1
                            counts[model]["errors"] += 1
                            print(f"ERROR {model} {symbol}: {exc}")
                            if isinstance(exc, httpx.HTTPStatusError):
                                row["http_status"] = exc.response.status_code
                                row["error_body"] = exc.response.text[:2000]
                        row["latency_seconds"] = round(time.perf_counter() - started, 3)
                        latencies[model].append(row["latency_seconds"])
                        output.write(json.dumps(row, ensure_ascii=False) + "\n")
                        output.flush()
                        print(f"{model} {symbol}: {row['status']} ({row['latency_seconds']}s)")
                        if row.get("http_status") in (401, 403, 404, 429):
                            print("Stopping: fix authentication, model availability or quota before retrying.")
                            return 1
    paired = [pair for pair in decisions.values() if len(pair) == 2]
    summary = {
        "scope": "Compact per-symbol predictions only; no advisory, trade simulation or profitability measurement",
        "fundamental_context": "NO_DATA (frozen; no live calendar)",
        "thinking": "model defaults; no explicit think or format, matching production prediction request",
        "models": {
            model: {"counts": dict(counts[model]), "mean_latency_seconds": sum(latencies[model]) / len(latencies[model])}
            for model in args.models
        },
        "valid_pairs": len(paired),
        "direction_agreements": sum(pair[args.models[0]] == pair[args.models[1]] for pair in paired),
        "errors": failures,
    }
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
