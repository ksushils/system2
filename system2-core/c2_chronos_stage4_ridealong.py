#!/usr/bin/env python3
"""
Stage 4 Chronos ride-along enrichment.

Reads Stage 2 top-40, fetches recent OHLCV from FMP, calls the live forecasting
service, and attaches Chronos fields for logging only.

Important: this does not reject, reorder, resize, or re-rank anything. The
output preserves the exact Stage 2 order and exists only so downstream paper
logs can measure whether Chronos helped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import fmp_cache


ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path.home() / "Downloads"
INPUT_PATH = ROOT / "stage2_surgical_strike_top40.json"
OUTPUT_PATH = ROOT / "stage4_chronos_enriched_top40.json"
META_PATH = ROOT / "stage4_chronos_metadata.json"
FORECAST_URL = os.environ.get("CHRONOS_FORECAST_URL", "http://72.62.134.167:8000/forecast")
FORECAST_API_KEY = os.environ.get("CHRONOS_FORECAST_API_KEY") or os.environ.get("FORECAST_API_KEY") or "dev-secret"
FMP_BASE = "https://financialmodelingprep.com"


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_fmp_key() -> str:
    env_key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if env_key:
        return env_key.strip()
    for path in [DOWNLOADS / "FMP-Scanner-v13.5-alpaca.json", DOWNLOADS / "FMP_Scanner_FIXED.json"]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"FMP_API_KEY:\s*'([^']+)'", text)
        if match:
            return match.group(1)
    raise RuntimeError("FMP API key not found. Set FMP_API_KEY.")


class FmpClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.calls = 0
        self.errors: list[str] = []

    def get(self, endpoint: str, timeout: int = 30):
        use_daily_cache = endpoint.startswith("stable/historical-price-eod/full")
        if use_daily_cache:
            cached = fmp_cache.get_daily(endpoint)
            if cached is not None:
                return cached
        sep = "&" if "?" in endpoint else "?"
        url = f"{FMP_BASE}/{endpoint}{sep}apikey={urllib.parse.quote(self.api_key)}"
        self.calls += 1
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "system2-chronos-stage4/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", "ignore"))
                    if use_daily_cache:
                        fmp_cache.set_daily(endpoint, data)
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                self.errors.append(f"{endpoint}: HTTP {exc.code}")
                return None
            except Exception as exc:
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                self.errors.append(f"{endpoint}: {exc}")
                return None
        return None


def as_num(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def fetch_ohlcv(client: FmpClient, symbol: str, lookback_days: int) -> list[dict]:
    today = date.today()
    start = today - timedelta(days=lookback_days)
    data = client.get(f"stable/historical-price-eod/full?symbol={urllib.parse.quote(symbol)}&from={start.isoformat()}&to={today.isoformat()}")
    if not isinstance(data, list):
        return []
    bars = []
    for row in sorted(data, key=lambda r: str(r.get("date", ""))):
        if not row.get("date"):
            continue
        close = as_num(row.get("close"))
        if close <= 0:
            continue
        bars.append({
            "date": str(row["date"])[:10],
            "open": as_num(row.get("open")),
            "high": as_num(row.get("high")),
            "low": as_num(row.get("low")),
            "close": close,
            "volume": as_num(row.get("volume")),
        })
    return bars


def setup_ohlcv(setup: dict, lookback_days: int) -> list[dict]:
    rows = setup.get("ohlcv_60") or setup.get("ohlcv")
    if not isinstance(rows, list):
        return []
    bars = []
    cutoff = date.today() - timedelta(days=lookback_days)
    for row in sorted(rows, key=lambda r: str(r.get("date", ""))):
        if not isinstance(row, dict) or not row.get("date"):
            continue
        try:
            bar_date = datetime.strptime(str(row["date"])[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if bar_date < cutoff:
            continue
        close = as_num(row.get("close"))
        if close <= 0:
            continue
        bars.append({
            "date": bar_date.isoformat(),
            "open": as_num(row.get("open")),
            "high": as_num(row.get("high")),
            "low": as_num(row.get("low")),
            "close": close,
            "volume": as_num(row.get("volume")),
        })
    return bars


def post_forecast(url: str, payload: dict, api_key: str, timeout: int = 1200) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": api_key, "User-Agent": "system2-chronos-stage4/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def pick_horizon(forecast: dict) -> dict | None:
    horizons = ((forecast or {}).get("chronos2") or {}).get("horizons") or {}
    return horizons.get("5") or horizons.get(5) or horizons.get("3") or horizons.get(3) or horizons.get("1") or horizons.get(1)


def chronos_fields(symbol: str, forecast: dict | None) -> dict:
    forecast = forecast or {}
    h = pick_horizon(forecast) or {}
    chronos2 = (forecast.get("chronos2") or {})
    horizons = chronos2.get("horizons") or {}
    complete = all([
        chronos2.get("status") or forecast.get("status"),
        h.get("direction"),
        h.get("cone_width_pct") is not None,
        horizons.get("1") or horizons.get(1),
        horizons.get("3") or horizons.get(3),
        horizons.get("5") or horizons.get(5),
        forecast.get("conviction_score") is not None,
    ])
    if not complete:
        return {
            "chronos": forecast or None,
            "chronos_status": None,
            "chronos_dir": None,
            "chronos_conf": None,
            "chronos_band_pct": None,
            "chronos2_1d": None,
            "chronos2_3d": None,
            "chronos2_5d": None,
            "forecastConviction": None,
            "forecastDecision": None,
            "forecastTier": None,
            "forecastReasons": [],
            "phase_c2_chronos_mode": "ride_along_logging_only",
        }
    return {
        "chronos": forecast,
        "chronos_status": chronos2.get("status") or forecast.get("status"),
        "chronos_dir": h.get("direction"),
        "chronos_conf": forecast.get("conviction_score"),
        "chronos_band_pct": h.get("cone_width_pct"),
        "chronos2_1d": horizons.get("1") or horizons.get(1),
        "chronos2_3d": horizons.get("3") or horizons.get(3),
        "chronos2_5d": horizons.get("5") or horizons.get(5),
        "forecastConviction": forecast.get("conviction_score"),
        "forecastDecision": (forecast.get("decision_overlay") or {}).get("decision"),
        "forecastTier": (forecast.get("decision_overlay") or {}).get("tier"),
        "forecastReasons": (forecast.get("decision_overlay") or {}).get("reasons") or [],
        "phase_c2_chronos_mode": "ride_along_logging_only",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(INPUT_PATH))
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--metadata", default=str(META_PATH))
    parser.add_argument("--forecast-url", default=FORECAST_URL)
    parser.add_argument("--lookback-days", type=int, default=60)
    parser.add_argument("--allow-failure", action="store_true")
    args = parser.parse_args()

    started = time.time()
    load_dotenv()
    forecast_url = os.environ.get("CHRONOS_FORECAST_URL") or args.forecast_url
    forecast_api_key = (
        os.environ.get("CHRONOS_FORECAST_API_KEY")
        or os.environ.get("FORECAST_API_KEY")
        or FORECAST_API_KEY
    )
    api_key = load_fmp_key()
    fmp = FmpClient(api_key)
    setups = json.loads(Path(args.input).read_text(encoding="utf-8"))

    tickers = []
    forecast_tickers = []
    missing_ohlcv = []
    reused_stage2_ohlcv = []
    for setup in setups:
        symbol = setup["symbol"]
        tickers.append(symbol)
        bars = setup_ohlcv(setup, args.lookback_days)
        if bars:
            reused_stage2_ohlcv.append(symbol)
        else:
            bars = fetch_ohlcv(fmp, symbol, args.lookback_days)
        if not bars:
            missing_ohlcv.append(symbol)
            continue
        forecast_tickers.append({
            "symbol": symbol,
            "sector": setup.get("sector"),
            "etf": (setup.get("cluster") or {}).get("etf"),
            "ohlcv": bars,
        })

    payload = {
        "horizons": [1, 3, 5, 10],
        "models": ["chronos2"],
        "tickers": forecast_tickers,
        "options": {"use_covariates": False, "covariate_mode": "off", "use_events": False},
    }

    forecast_response = {"forecasts": {}, "errors": []}
    forecast_error = None
    if forecast_tickers:
        try:
            forecast_response = post_forecast(forecast_url, payload, forecast_api_key)
        except Exception as exc:
            forecast_error = str(exc)
            if not args.allow_failure:
                raise

    forecasts = forecast_response.get("forecasts") or {}
    enriched = []
    for setup in setups:
        symbol = setup["symbol"]
        enriched.append({**setup, **chronos_fields(symbol, forecasts.get(symbol))})

    metadata = {
        "stage": "C2",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "inputCount": len(setups),
        "outputCount": len(enriched),
        "forecastUrl": forecast_url,
        "lookbackDays": args.lookback_days,
        "fmpOhlcvCallCount": fmp.calls,
        "stage2OhlcvReuseCount": len(reused_stage2_ohlcv),
        "stage2OhlcvReuseSymbols": reused_stage2_ohlcv,
        "fmpErrorCount": len(fmp.errors),
        "fmpErrorsSample": fmp.errors[:20],
        "forecastTickerCount": len(forecast_tickers),
        "missingOhlcv": missing_ohlcv,
        "forecastError": forecast_error,
        "forecastErrors": forecast_response.get("errors") or [],
        "modelVersions": forecast_response.get("model_versions") or {},
        "runtimeSeconds": round(time.time() - started, 2),
        "loggingOnly": True,
        "selectionInfluence": "none",
        "orderPreserved": [s["symbol"] for s in setups] == [s["symbol"] for s in enriched],
        "notes": [
            "Chronos fields are attached after Stage 2 for logging only.",
            "No sorting, rejection, rank change, or risk sizing change occurs in this stage.",
            "B4 still receives the same 40 names in the same order.",
        ],
    }

    Path(args.output).write_text(json.dumps(enriched, indent=2), encoding="utf-8")
    Path(args.metadata).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps({
        "inputCount": metadata["inputCount"],
        "outputCount": metadata["outputCount"],
        "forecastTickerCount": metadata["forecastTickerCount"],
        "fmpOhlcvCallCount": metadata["fmpOhlcvCallCount"],
        "forecastError": forecast_error,
        "forecastErrors": metadata["forecastErrors"][:5],
        "chronosPopulatedCount": sum(1 for row in enriched if row.get("chronos_dir")),
        "orderPreserved": metadata["orderPreserved"],
        "loggingOnly": True,
        "selectionInfluence": "none",
    }, indent=2))


if __name__ == "__main__":
    main()
