#!/usr/bin/env python3
"""
Standalone System 2 regime checker.

This is Step 2 preview-only until explicitly wired into the nightly runner.
It reads FMP credentials from .env, fetches two daily bars for SPY/QQQ/^VIX,
and classifies the market regime without changing trade selection.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
import fmp_cache


ROOT = Path(__file__).resolve().parent
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


def fmp_key() -> str:
    load_dotenv()
    key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if not key:
        raise RuntimeError("FMP_API_KEY not found in environment or .env")
    return key


def get_json(url: str, *, allow_http_error: bool = False) -> Any:
    response = requests.get(url, timeout=25)
    if allow_http_error and response.status_code >= 400:
        return []
    response.raise_for_status()
    return response.json()


def get_cached_json(cache_id: str, url: str, *, allow_http_error: bool = False) -> Any:
    cached = fmp_cache.get_daily(cache_id)
    if cached is not None:
        return cached
    data = get_json(url, allow_http_error=allow_http_error)
    if data is not None:
        fmp_cache.set_daily(cache_id, data)
    return data


def daily_bars(symbol: str, api_key: str) -> list[dict[str, Any]]:
    encoded_symbol = symbol.replace("^", "%5E")
    legacy_url = (
        f"{FMP_BASE}/stable/historical-price-full/{encoded_symbol}"
        f"?timeseries=2&apikey={api_key}"
    )
    data = get_cached_json(f"regime:{symbol}:legacy_daily_2", legacy_url, allow_http_error=True)
    if isinstance(data, dict) and isinstance(data.get("historical"), list):
        return data["historical"][:2]
    if isinstance(data, list) and len(data) >= 2:
        return data[:2]

    eod_url = (
        f"{FMP_BASE}/stable/historical-price-eod/full"
        f"?symbol={encoded_symbol}&apikey={api_key}"
    )
    data = get_cached_json(f"regime:{symbol}:eod_daily_2", eod_url)
    if isinstance(data, list) and len(data) >= 2:
        return data[:2]
    if isinstance(data, dict) and isinstance(data.get("historical"), list):
        return data["historical"][:2]
    raise RuntimeError(f"Could not fetch two daily bars for {symbol}")


def latest_vix_bar(api_key: str) -> dict[str, Any]:
    url = (
        f"{FMP_BASE}/stable/historical-price-eod/full"
        f"?symbol=%5EVIX&timeseries=1&apikey={api_key}"
    )
    data = get_cached_json("regime:^VIX:eod_latest", url)
    rows = data if isinstance(data, list) else data.get("historical", [])
    if not rows:
        raise RuntimeError("Could not fetch latest VIX daily bar")
    return rows[0]


def pct_change(today: float, previous: float) -> float:
    if previous == 0:
        raise ValueError("previous value cannot be zero")
    return ((today - previous) / previous) * 100.0


def classify_vix(vix_current: float) -> tuple[str, str, float]:
    if vix_current > 30:
        return "RISK_OFF", "VIX > 30", 0.0
    if vix_current >= 20:
        return "CAUTION", "VIX between 20 and 30", 0.5
    return "NORMAL", "VIX below 20", 1.0


def check_regime() -> dict[str, Any]:
    api_key = fmp_key()
    spy = daily_bars("SPY", api_key)
    qqq = daily_bars("QQQ", api_key)
    vix = daily_bars("^VIX", api_key)

    spy_today, spy_prev = spy[0], spy[1]
    qqq_today, qqq_prev = qqq[0], qqq[1]
    vix_today, vix_prev = latest_vix_bar(api_key), vix[1]

    spy_1d_pct = pct_change(float(spy_today["close"]), float(spy_prev["close"]))
    qqq_1d_pct = pct_change(float(qqq_today["close"]), float(qqq_prev["close"]))
    vix_current = float(vix_today["close"])
    vix_1d_chg = pct_change(vix_current, float(vix_prev["close"]))

    regime, reason, position_size_multiplier = classify_vix(vix_current)

    return {
        "regime": regime,
        "verdict": regime,
        "reason": reason,
        "spy_1d_pct": round(spy_1d_pct, 3),
        "qqq_1d_pct": round(qqq_1d_pct, 3),
        "vix_current": round(vix_current, 3),
        "vix_1d_chg": round(vix_1d_chg, 3),
        "position_size_multiplier": position_size_multiplier,
        "source_dates": {
            "SPY": {"today": spy_today.get("date"), "previous": spy_prev.get("date")},
            "QQQ": {"today": qqq_today.get("date"), "previous": qqq_prev.get("date")},
            "VIX": {"today": vix_today.get("date"), "previous": vix_prev.get("date")},
        },
        "mode": "vix_threshold_preview",
    }


def main() -> None:
    print(json.dumps(check_regime(), indent=2))


if __name__ == "__main__":
    main()
