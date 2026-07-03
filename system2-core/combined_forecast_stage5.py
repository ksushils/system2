#!/usr/bin/env python3
"""Sequential Chronos + Kronos forecast enrichment for the news-safe top 40."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from c2_chronos_stage4_ridealong import chronos_fields


ROOT = Path(__file__).resolve().parent
INPUT_PATH = ROOT / "stage4_options_enriched_top40.json"
OUTPUT_PATH = ROOT / "stage5_combined_forecast_top40.json"
META_PATH = ROOT / "stage5_combined_forecast_metadata.json"
CHRONOS_URL = os.environ.get("CHRONOS_FORECAST_URL", "http://127.0.0.1:8000/forecast")
KRONOS_URL = os.environ.get("KRONOS_FORECAST_URL", "http://127.0.0.1:8001/forecast")


class ForecastHttpError(RuntimeError):
    def __init__(self, url: str, status: int, body: str) -> None:
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status} from {url}: {body}")


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def post_json(url: str, payload: dict, timeout: int, headers: dict[str, str] | None = None) -> dict:
    request_headers = {
        "Content-Type": "application/json",
        "User-Agent": "system2-combined-forecast/1.0",
    }
    request_headers.update(headers or {})
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=request_headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        print(f"Forecast HTTP error: status={exc.code} url={url} body={body}")
        raise ForecastHttpError(url, exc.code, body) from exc


def number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def normalized_dir(value: Any) -> str | None:
    direction = str(value or "").strip().upper()
    return direction if direction in {"UP", "DOWN", "FLAT"} else None


def combine(chronos_dir: str | None, kronos_dir: str | None) -> tuple[str, bool]:
    if chronos_dir is None and kronos_dir is None:
        return "NO_FORECAST", False
    if chronos_dir is None:
        return kronos_dir or "NO_FORECAST", False
    if kronos_dir is None:
        return chronos_dir, False
    models_agree = chronos_dir == kronos_dir
    pair = {chronos_dir, kronos_dir}
    if models_agree:
        return {
            "UP": "STRONG_UP",
            "DOWN": "STRONG_DOWN",
            "FLAT": "FLAT",
        }[chronos_dir], True
    if pair == {"UP", "FLAT"}:
        return "LEAN_UP", False
    if pair == {"DOWN", "FLAT"}:
        return "LEAN_DOWN", False
    if pair == {"UP", "DOWN"}:
        return "CONFLICTED", False
    return "NO_FORECAST", False


def average_available(*values: Any) -> float | None:
    nums = [number(value) for value in values]
    nums = [value for value in nums if value is not None]
    return round(sum(nums) / len(nums), 4) if nums else None


def blank_chronos(reason: str) -> dict[str, Any]:
    return {
        "chronos": None,
        "chronos_status": "FAILED",
        "chronos_dir": None,
        "chronos_conf": None,
        "chronos_conviction": None,
        "chronos_band_pct": None,
        "chronos2_1d": None,
        "chronos2_3d": None,
        "chronos2_5d": None,
        "forecastConviction": None,
        "forecastDecision": None,
        "forecastTier": None,
        "forecastReasons": [reason],
        "phase_c2_chronos_mode": "ride_along_logging_only",
    }


def blank_kronos(reason: str) -> dict[str, Any]:
    return {
        "kronos_status": "FAILED",
        "kronos_dir": None,
        "kronos_band_pct": None,
        "kronos_conviction": None,
        "kronos_1d": None,
        "kronos_3d": None,
        "kronos_5d": None,
        "kronos_error": reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(INPUT_PATH))
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--metadata", default=str(META_PATH))
    parser.add_argument("--chronos-url", default=CHRONOS_URL)
    parser.add_argument("--kronos-url", default=KRONOS_URL)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--n-samples", type=int, default=10)
    args = parser.parse_args()

    load_dotenv()
    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    tickers = []
    missing_ohlcv = []
    for row in rows:
        bars = row.get("ohlcv_60")
        if isinstance(bars, list) and bars:
            tickers.append({"symbol": row["symbol"], "ohlcv": bars})
        else:
            missing_ohlcv.append(row["symbol"])

    chronos_payload = {
        "horizons": [1, 3, 5],
        "models": ["chronos2"],
        "tickers": tickers,
        "options": {"use_covariates": False, "covariate_mode": "off", "use_events": False},
    }
    kronos_payload = {
        "horizons": [1, 3, 5],
        "models": ["kronos"],
        "n_samples": args.n_samples,
        "tickers": tickers,
    }

    skipped_empty = not tickers
    skip_warning = "Stage 5 skipped: no records contain ohlcv_60" if skipped_empty else None
    if skip_warning:
        print(skip_warning)

    chronos_started = time.perf_counter()
    chronos_error = None
    chronos_http_error = None
    if skipped_empty:
        chronos_response = {"forecasts": {}, "errors": []}
    else:
        try:
            api_key = os.environ.get("CHRONOS_FORECAST_API_KEY") or os.environ.get("FORECAST_API_KEY") or "dev-secret"
            chronos_response = post_json(
                args.chronos_url, chronos_payload, args.timeout, {"X-API-Key": api_key}
            )
        except Exception as exc:
            chronos_error = str(exc)
            chronos_http_error = exc if isinstance(exc, ForecastHttpError) else None
            chronos_response = {"forecasts": {}, "errors": [{"error": chronos_error}]}
    chronos_seconds = round(time.perf_counter() - chronos_started, 3)

    kronos_started = time.perf_counter()
    kronos_error = None
    kronos_http_error = None
    if skipped_empty:
        kronos_response = {"results": []}
    else:
        try:
            kronos_response = post_json(args.kronos_url, kronos_payload, args.timeout)
        except Exception as exc:
            kronos_error = str(exc)
            kronos_http_error = exc if isinstance(exc, ForecastHttpError) else None
            kronos_response = {"results": []}
    kronos_seconds = round(time.perf_counter() - kronos_started, 3)

    chronos_by_symbol = chronos_response.get("forecasts") or {}
    kronos_by_symbol = {
        item.get("symbol"): item
        for item in (kronos_response.get("results") or [])
        if item.get("symbol")
    }

    enriched = []
    for row in rows:
        symbol = row["symbol"]
        if symbol in missing_ohlcv:
            chronos = blank_chronos("missing stored ohlcv_60")
            kronos = blank_kronos("missing stored ohlcv_60")
        else:
            raw_chronos = chronos_by_symbol.get(symbol)
            chronos = chronos_fields(symbol, raw_chronos) if raw_chronos else blank_chronos(
                chronos_error or "missing Chronos result"
            )
            if chronos.get("chronos_dir") is None:
                chronos["chronos_status"] = "FAILED"
                chronos.setdefault("forecastReasons", []).append("Chronos result incomplete")
            chronos["chronos_conviction"] = chronos.get("chronos_conf")

            raw_kronos = kronos_by_symbol.get(symbol)
            if raw_kronos and raw_kronos.get("status") == "OK":
                kronos = {
                    "kronos_status": "OK",
                    "kronos_dir": normalized_dir(raw_kronos.get("kronos_dir")),
                    "kronos_band_pct": number(raw_kronos.get("kronos_band_pct")),
                    "kronos_conviction": number(raw_kronos.get("kronos_conviction")),
                    "kronos_1d": number(raw_kronos.get("kronos_1d")),
                    "kronos_3d": number(raw_kronos.get("kronos_3d")),
                    "kronos_5d": number(raw_kronos.get("kronos_5d")),
                    "kronos_error": None,
                }
            else:
                kronos = blank_kronos(
                    (raw_kronos or {}).get("error") or kronos_error or "missing Kronos result"
                )

        c_dir = normalized_dir(chronos.get("chronos_dir"))
        k_dir = normalized_dir(kronos.get("kronos_dir"))
        combined_dir, models_agree = combine(c_dir, k_dir)
        combined_band = average_available(
            chronos.get("chronos_band_pct"), kronos.get("kronos_band_pct")
        )
        enriched.append({
            **row,
            **chronos,
            **kronos,
            "combined_forecast_dir": combined_dir,
            "combined_dir": combined_dir,
            "models_agree": models_agree,
            "combined_band_pct": combined_band,
            "forecast_high_uncertainty": combined_band is not None and combined_band > 5.0,
            "forecast_mode": "ride_along_logging_and_confluence_only",
        })

    metadata = {
        "stage": "STAGE5_COMBINED_FORECAST",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "inputCount": len(rows),
        "outputCount": len(enriched),
        "tickersSent": len(tickers),
        "missingOhlcv": missing_ohlcv,
        "stage5_status": "SKIPPED" if skipped_empty else "OK",
        "stage5_warning": skip_warning,
        "chronos_status": "SKIPPED" if skipped_empty else ("FAILED" if chronos_error else "OK"),
        "kronos_status": "SKIPPED" if skipped_empty else ("FAILED" if kronos_error else "OK"),
        "chronos_error": chronos_error,
        "kronos_error": kronos_error,
        "chronos_error_status": chronos_http_error.status if chronos_http_error else None,
        "chronos_error_body": chronos_http_error.body if chronos_http_error else None,
        "kronos_error_status": kronos_http_error.status if kronos_http_error else None,
        "kronos_error_body": kronos_http_error.body if kronos_http_error else None,
        "chronos_inference_seconds": chronos_seconds,
        "kronos_inference_seconds": kronos_seconds,
        "total_forecast_seconds": round(chronos_seconds + kronos_seconds, 3),
        "n_samples": args.n_samples,
        "timeoutSecondsPerModel": args.timeout,
        "failOpen": True,
        "paperOnly": True,
        "combinedDirectionCounts": {},
    }
    for row in enriched:
        direction = row["combined_forecast_dir"]
        metadata["combinedDirectionCounts"][direction] = (
            metadata["combinedDirectionCounts"].get(direction, 0) + 1
        )

    Path(args.output).write_text(json.dumps(enriched, indent=2), encoding="utf-8")
    Path(args.metadata).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
