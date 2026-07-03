#!/usr/bin/env python3
"""
Finnhub free-tier signal scraper.

Provides:
  - Earnings surprise cross-check for PEAD
  - Analyst recommendation trends for Family C
  - FDA advisory calendar storage for data-driven binary-event gates
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


FINNHUB_KEY = os.getenv("FINNHUB_KEY", "")

try:
    import finnhub

    client = finnhub.Client(api_key=FINNHUB_KEY) if FINNHUB_KEY else None
except Exception as e:
    print(f"Finnhub init failed: {e}")
    client = None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _date_days_since(value: str) -> int:
    try:
        dt = datetime.strptime(value[:10], "%Y-%m-%d")
        return (datetime.now() - dt).days
    except Exception:
        return 999


def get_earnings_surprise(ticker: str) -> dict[str, Any] | None:
    """Latest Finnhub earnings surprise for PEAD cross-check."""
    if not client:
        return None
    try:
        data = client.company_earnings(ticker, limit=4)
        if not data:
            return None
        latest = data[0]
        actual = _safe_float(latest.get("actual"))
        estimate = _safe_float(latest.get("estimate"))
        period = str(latest.get("period") or "")[:10]
        if actual is None or estimate is None:
            return None

        surprise_pct = _safe_float(latest.get("surprisePercent"))
        if surprise_pct is None:
            surprise_pct = ((actual - estimate) / abs(estimate) * 100) if estimate else 0.0

        return {
            "actual": actual,
            "estimate": estimate,
            "surprise_pct": round(surprise_pct, 2),
            "period": period,
            "days_since": _date_days_since(period),
            "is_beat": actual > estimate,
            "source": "finnhub",
        }
    except Exception as e:
        print(f"Finnhub earnings {ticker}: {e}")
        return None


def get_recommendation_trend(ticker: str) -> dict[str, Any] | None:
    """Analyst recommendation trend from the latest two monthly snapshots."""
    if not client:
        return None
    try:
        data = client.recommendation_trends(ticker)
        if not data or len(data) < 2:
            return None
        now = data[0]
        prior = data[1]

        now_bull = int(now.get("strongBuy", 0) or 0) + int(now.get("buy", 0) or 0)
        now_bear = int(now.get("sell", 0) or 0) + int(now.get("strongSell", 0) or 0)
        prior_bull = int(prior.get("strongBuy", 0) or 0) + int(prior.get("buy", 0) or 0)
        total = now_bull + now_bear + int(now.get("hold", 0) or 0)
        net_score = now_bull - now_bear
        improving = now_bull > prior_bull

        if improving and net_score > 0:
            direction = "UPGRADING"
        elif now_bull < prior_bull:
            direction = "DOWNGRADING"
        else:
            direction = "STABLE"

        return {
            "period": now.get("period"),
            "strong_buy": int(now.get("strongBuy", 0) or 0),
            "buy": int(now.get("buy", 0) or 0),
            "hold": int(now.get("hold", 0) or 0),
            "sell": int(now.get("sell", 0) or 0),
            "strong_sell": int(now.get("strongSell", 0) or 0),
            "net_score": net_score,
            "bull_pct": round((now_bull / total * 100) if total else 0, 1),
            "improving": improving,
            "direction": direction,
            "source": "finnhub",
        }
    except Exception as e:
        print(f"Finnhub rec {ticker}: {e}")
        return None


def _fda_events_from_client() -> list[dict[str, Any]]:
    if not client:
        return []
    if hasattr(client, "fda_committee_meeting_calendar"):
        data = client.fda_committee_meeting_calendar()
        return data if isinstance(data, list) else []

    import requests

    url = "https://finnhub.io/api/v1/fda-advisory-committee-calendar"
    resp = requests.get(url, params={"token": FINNHUB_KEY}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def get_fda_calendar() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """
    Store the FDA calendar and map events by ticker when Finnhub supplies one.

    The current free endpoint returns broad FDA advisory events without a ticker
    field, so most runs will have calendar rows but zero ticker-mapped events.
    """
    try:
        data = _fda_events_from_client()
        events_by_ticker: dict[str, dict[str, Any]] = {}
        all_events: list[dict[str, Any]] = []

        for event in data:
            if not isinstance(event, dict):
                continue
            event_date = event.get("fromDate") or event.get("eventDate") or event.get("date") or ""
            description = event.get("eventDescription") or event.get("description") or "FDA advisory event"
            normalized = {
                "event_date": event_date,
                "description": description,
                "url": event.get("url"),
                "source": "finnhub",
            }
            all_events.append(normalized)

            ticker = event.get("ticker") or event.get("symbol")
            if ticker:
                events_by_ticker[str(ticker).upper()] = normalized
                continue

            # Only map explicit stock-like tags such as "(ABCD)" or "NASDAQ:ABCD".
            text_blob = f"{description} {event.get('url') or ''}"
            matches = set(re.findall(r"(?:NASDAQ|NYSE|AMEX):([A-Z]{1,5})|\(([A-Z]{1,5})\)", text_blob))
            for left, right in matches:
                candidate = (left or right or "").upper()
                if candidate and candidate not in {"FDA", "PDUFA", "ADCOM", "NDA", "BLA", "PDAC", "DSARM"}:
                    events_by_ticker[candidate] = normalized

        return events_by_ticker, all_events
    except Exception as e:
        print(f"Finnhub FDA calendar: {e}")
        return {}, []


def _load_stage_tickers() -> list[str]:
    for path in [
        ROOT / "stage2_surgical_strike_top40.json",
        ROOT / "stage2_confluence_ranked_top40.json",
    ]:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("ideas", data.get("finalists", []))
            tickers = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                ticker = item.get("ticker") or item.get("symbol")
                if ticker:
                    tickers.append(str(ticker).upper())
            return list(dict.fromkeys(tickers))
        except Exception as e:
            print(f"Finnhub ticker source {path.name}: {e}")
    return []


def run(tickers: list[str] | None = None) -> dict[str, Any]:
    if not client:
        print("No Finnhub client")
        return {}

    fda_by_ticker, fda_all = get_fda_calendar()
    print(f"FDA events upcoming: {len(fda_all)} total, {len(fda_by_ticker)} ticker-mapped")

    if tickers is None:
        tickers = _load_stage_tickers()
    tickers = [str(t).upper() for t in (tickers or []) if t]

    results: dict[str, Any] = {}
    failures = 0
    for i, ticker in enumerate(tickers[:50]):
        earn = get_earnings_surprise(ticker)
        time.sleep(1.1)
        rec = get_recommendation_trend(ticker)
        time.sleep(1.1)
        if earn is None and rec is None:
            failures += 1
        else:
            failures = 0
        if failures >= 5:
            print("Finnhub circuit breaker: 5 consecutive empty/error ticker reads")
            break

        results[ticker] = {
            "earnings": earn,
            "recommendation": rec,
            "fda_event": fda_by_ticker.get(ticker),
        }
        if (i + 1) % 10 == 0:
            print(f"  {i + 1} tickers")

    output = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ticker_count": len(results),
        "fda_calendar": fda_by_ticker,
        "fda_calendar_all": fda_all,
        "tickers": results,
    }
    out = DATA_DIR / "finnhub_signals.json"
    out.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Finnhub: {len(results)} tickers, {len(fda_all)} FDA events, {len(fda_by_ticker)} ticker-mapped")
    return output


if __name__ == "__main__":
    run()
