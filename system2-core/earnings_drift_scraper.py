#!/usr/bin/env python3
"""PEAD and analyst estimate revision scraper for System 2.

Uses FMP stable endpoints available on the current plan:
  - earnings-calendar for actual vs estimated EPS
  - analyst-estimates?period=quarterly for forward EPS estimate pressure
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parent
BASE = "https://financialmodelingprep.com/stable"


def load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()
FMP_KEY = os.getenv("FMP_API_KEY", "") or os.getenv("FMP_KEY", "")


def _json_get(endpoint: str, params: dict[str, Any]) -> Any:
    resp = requests.get(f"{BASE}/{endpoint}", params={**params, "apikey": FMP_KEY}, timeout=12)
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "body": resp.text[:200]}
    return resp.json()


def _date_value(row: dict[str, Any]) -> datetime | None:
    raw = str(row.get("date") or "")[:10]
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except Exception:
        return None


def get_recent_earnings_surprise(ticker: str) -> dict[str, Any] | None:
    """Get the most recent reported EPS surprise from earnings-calendar."""
    try:
        data = _json_get("earnings-calendar", {"symbol": ticker})
        if not isinstance(data, list):
            return None
        today = datetime.now()
        rows = []
        for row in data:
            if str(row.get("symbol") or "").upper() != ticker.upper():
                continue
            dt = _date_value(row)
            if not dt or dt > today:
                continue
            actual = row.get("epsActual")
            estimate = row.get("epsEstimated")
            if actual is None or estimate is None:
                continue
            rows.append((dt, row))
        if not rows:
            return None
        _, latest = sorted(rows, key=lambda x: x[0], reverse=True)[0]
        actual = float(latest.get("epsActual"))
        estimate = float(latest.get("epsEstimated"))
        surprise_pct = ((actual - estimate) / abs(estimate) * 100) if estimate else 0.0
        report_date = str(latest.get("date") or "")[:10]
        days_since = (today - datetime.strptime(report_date, "%Y-%m-%d")).days
        return {
            "actual_eps": actual,
            "estimate_eps": estimate,
            "surprise_pct": round(surprise_pct, 2),
            "report_date": report_date,
            "days_since_earnings": days_since,
            "is_beat": actual > estimate,
            "in_drift_window": days_since <= 15,
            "source": "fmp_earnings_calendar",
        }
    except Exception as exc:
        print(f"Surprise {ticker}: {exc}")
        return None


def get_estimate_revision_pressure(ticker: str) -> dict[str, Any] | None:
    """Measure forward quarterly EPS estimate pressure from FMP analyst estimates."""
    try:
        data = _json_get("analyst-estimates", {"symbol": ticker, "period": "quarterly", "limit": 4})
        if not isinstance(data, list) or len(data) < 2:
            return None
        today = datetime.now()
        rows = [r for r in data if _date_value(r) and _date_value(r) >= today]
        rows = sorted(rows or data, key=lambda r: str(r.get("date") or ""))
        if len(rows) < 2:
            return None
        recent = rows[0].get("estimatedEpsAvg", rows[0].get("epsAvg"))
        prior = rows[1].get("estimatedEpsAvg", rows[1].get("epsAvg"))
        if recent is None or prior in (None, 0):
            return None
        recent_f = float(recent)
        prior_f = float(prior)
        revision_pct = (recent_f - prior_f) / abs(prior_f) * 100
        return {
            "recent_estimate": recent_f,
            "prior_estimate": prior_f,
            "revision_pct": round(revision_pct, 2),
            "direction": "RISING" if revision_pct > 2 else "FALLING" if revision_pct < -2 else "STABLE",
            "source": "fmp_analyst_estimates_quarterly",
        }
    except Exception as exc:
        print(f"Revision {ticker}: {exc}")
        return None


def _load_stage_tickers() -> list[str]:
    for path in [
        ROOT / "stage2_surgical_strike_top40.json",
        ROOT / "stage2_confluence_ranked_top40.json",
    ]:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("ideas", data.get("finalists", []))
        tickers: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or item.get("symbol") or "").upper().strip()
            if ticker and ticker not in tickers:
                tickers.append(ticker)
        if tickers:
            return tickers
    return []


def run(tickers: list[str] | None = None) -> dict[str, Any]:
    tickers = tickers or _load_stage_tickers()
    if not tickers:
        print("No tickers found")
        return {}

    results: dict[str, Any] = {}
    fail_count = 0
    for idx, ticker in enumerate(tickers[:50]):
        surprise = get_recent_earnings_surprise(ticker)
        time.sleep(0.3)
        revision = get_estimate_revision_pressure(ticker)
        time.sleep(0.3)
        if surprise is None and revision is None:
            fail_count += 1
            if fail_count >= 5:
                print("Circuit breaker: 5 consecutive no-data tickers")
                break
            continue
        fail_count = 0
        results[ticker] = {
            "earnings_surprise": surprise,
            "estimate_revision": revision,
        }
        if (idx + 1) % 10 == 0:
            print(f"  {idx + 1}/{min(len(tickers), 50)}")

    output = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ticker_count": len(results),
        "in_drift_window": sum(
            1 for value in results.values()
            if value.get("earnings_surprise") and value["earnings_surprise"].get("in_drift_window")
        ),
        "endpoints": {
            "surprise": "earnings-calendar",
            "revision": "analyst-estimates?period=quarterly",
        },
        "tickers": results,
    }
    out = ROOT / "data" / "earnings_drift.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Earnings drift: {len(results)} tickers")
    print(f"In drift window: {output['in_drift_window']}")
    return output


if __name__ == "__main__":
    run()
