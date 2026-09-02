#!/usr/bin/env python3
"""
System 2 multi-source catalyst discovery.

This is a top-of-funnel DISCOVERY source only. It emits tagged candidates that
must still pass Stage 1 liquidity, Stage 2 Surgical Strike scoring, and every
existing downstream guard. It never auto-promotes a ticker into the morning
list and never changes strategy/risk logic.

Paid specialist event APIs can plug in at collect_all_catalysts() later
(for example LevelFields or Benzinga), after FMP-sourced catalysts prove edge
in the scoring loop.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import fmp_bandwidth


ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path.home() / "Downloads"
UNIVERSE_PATH = ROOT / "universe.json"
CATALYST_PATH = ROOT / "catalyst_candidates.json"
CANDIDATE_POOL_PATH = ROOT / "candidate_pool.json"
META_PATH = ROOT / "catalyst_discovery_metadata.json"

FMP_BASE = "https://financialmodelingprep.com"
DEFAULT_LOOKBACK_HOURS = 72
DEFAULT_LIMIT = 30
NEWS_PAGES = 4
NEWS_LIMIT = 100

CORPORATE_KEYWORDS = [
    "buyback", "share repurchase", "repurchase authorization", "dividend increase",
    "raises dividend", "dividend hike", "merger", "acquisition", "to acquire",
    "definitive agreement", "major contract", "contract award", "awarded contract",
    "strategic partnership", "expanded partnership",
]

FDA_KEYWORDS = [
    "fda", "approval", "approved", "pdufa", "phase 2", "phase ii", "phase 3",
    "phase iii", "trial results", "trial readout", "clinical data", "topline",
    "nda", "bla", "complete response letter", "crl",
]

ANALYST_KEYWORDS = [
    "upgrade", "downgrade", "price target raised", "price target lowered",
    "raises price target", "lowers price target", "initiated with", "initiates",
    "reiterates", "maintains",
]

ANALYST_STRONG_PHRASES = [
    "upgrade", "downgrade", "price target raised", "price target lowered",
    "raises price target", "lowers price target", "initiated with", "initiates with",
    "reiterates rating", "maintains rating",
]

BIOTECH_WORDS = [
    "biotech", "biopharma", "pharmaceutical", "therapeutics", "oncology",
    "clinical", "drug", "vaccine", "medical", "healthcare",
]


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


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


def as_num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def clean_symbol(symbol: Any) -> str:
    return re.sub(r"[^A-Z0-9.]", "", str(symbol or "").upper()).strip(".")


class FmpClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.calls = 0
        self.errors: list[str] = []
        self.calls_by_type: dict[str, int] = defaultdict(int)
        self.errors_by_type: dict[str, list[str]] = defaultdict(list)

    def get(self, endpoint: str, signal_type: str, timeout: int = 30):
        sep = "&" if "?" in endpoint else "?"
        url = f"{FMP_BASE}/{endpoint}{sep}apikey={urllib.parse.quote(self.api_key)}"
        self.calls += 1
        self.calls_by_type[signal_type] += 1
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url,
                    headers={"Accept": "application/json", "User-Agent": "system2-catalyst-discovery/1.0"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    fmp_bandwidth.record(
                        endpoint,
                        len(raw),
                        status=getattr(resp, "status", None),
                        source="catalyst_discovery",
                    )
                    return json.loads(raw.decode("utf-8", "ignore"))
            except urllib.error.HTTPError as exc:
                fmp_bandwidth.record(endpoint, 0, status=exc.code, source="catalyst_discovery")
                if exc.code == 429 and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                msg = f"{endpoint}: HTTP {exc.code}"
                self.errors.append(msg)
                self.errors_by_type[signal_type].append(msg)
                return None
            except Exception as exc:
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                msg = f"{endpoint}: {exc}"
                self.errors.append(msg)
                self.errors_by_type[signal_type].append(msg)
                return None
        return None


def record(symbol: str, sub_type: str, summary: str, dt: datetime | None, source_endpoint: str, score: float = 1.0, extra: dict | None = None) -> dict:
    return {
        "ticker": clean_symbol(symbol),
        "symbol": clean_symbol(symbol),
        "source": "catalyst",
        "sub_type": sub_type,
        "sub_types": [sub_type],
        "catalyst_summary": summary[:240],
        "catalyst_date": (dt or datetime.now(timezone.utc)).date().isoformat(),
        "catalyst_datetime": (dt or datetime.now(timezone.utc)).isoformat(),
        "catalyst_score": round(score, 3),
        "catalyst_sources": [source_endpoint],
        **(extra or {}),
    }


def is_recent(dt: datetime | None, cutoff: datetime) -> bool:
    return bool(dt and dt >= cutoff)


def text_has(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keywords)


def fetch_news(client: FmpClient, signal_type: str) -> list[dict]:
    rows: list[dict] = []
    for page in range(NEWS_PAGES):
        data = client.get(f"stable/news/stock-latest?page={page}&limit={NEWS_LIMIT}", signal_type)
        if isinstance(data, list):
            rows.extend(data)
    return rows


def fetch_press_releases(client: FmpClient, signal_type: str) -> list[dict]:
    rows: list[dict] = []
    for page in range(2):
        data = client.get(f"stable/news/press-releases-latest?page={page}&limit={NEWS_LIMIT}", signal_type)
        if isinstance(data, list):
            rows.extend(data)
    return rows


def earnings_surprises(client: FmpClient, cutoff: datetime, universe: set[str]) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=5)).isoformat()
    end = today.isoformat()
    data = client.get(f"stable/earnings-calendar?from={start}&to={end}", "earnings")
    out: list[dict] = []
    if not isinstance(data, list):
        return out
    for row in data:
        symbol = clean_symbol(row.get("symbol"))
        dt = parse_dt(row.get("date") or row.get("lastUpdated"))
        if not symbol or (universe and symbol not in universe) or not is_recent(dt, cutoff):
            continue
        actual = as_num(row.get("epsActual"))
        estimate = as_num(row.get("epsEstimated"))
        if actual is None or estimate in (None, 0):
            continue
        surprise_pct = ((actual - estimate) / abs(estimate)) * 100
        if abs(surprise_pct) < 5:
            continue
        direction = "beat" if surprise_pct > 0 else "miss"
        out.append(record(
            symbol,
            "earnings",
            f"EPS {direction} {surprise_pct:+.1f}% ({actual:g} vs {estimate:g})",
            dt,
            "stable/earnings-calendar",
            score=min(5.0, abs(surprise_pct) / 10.0),
            extra={"earnings_surprise_pct": round(surprise_pct, 2), "earnings_actual": actual, "earnings_estimate": estimate},
        ))
    return sorted(out, key=lambda r: (r["catalyst_score"], r["catalyst_datetime"]), reverse=True)


def analyst_changes(client: FmpClient, cutoff: datetime, universe: set[str]) -> list[dict]:
    out: list[dict] = []
    # Current FMP stable market-wide price-target feed is the cleanest cheap
    # analyst-change source available on this plan. The legacy
    # /api/v4/upgrades-downgrades-rss-feed route is forbidden for this key.
    data = []
    for page in range(3):
        rows = client.get(f"stable/price-target-latest-news?page={page}&limit=100", "analyst")
        if isinstance(rows, list):
            data.extend(rows)
    for row in data:
        symbol = clean_symbol(row.get("symbol"))
        dt = parse_dt(row.get("publishedDate") or row.get("date"))
        title = str(row.get("newsTitle") or row.get("title") or "")
        text = f"{title} {row.get('text') or ''}"
        if not symbol or (universe and symbol not in universe) or not is_recent(dt, cutoff):
            continue
        if not text_has(text, ANALYST_KEYWORDS):
            continue
        score = 2.0 if "upgrade" in text.lower() or "raised" in text.lower() or "raises" in text.lower() else 1.0
        out.append(record(
            symbol,
            "analyst",
            title,
            dt,
            "stable/price-target-latest-news",
            score=score,
            extra={
                "news_url": row.get("newsURL") or row.get("url"),
                "publisher": row.get("newsPublisher") or row.get("publisher"),
                "analyst_company": row.get("analystCompany"),
                "price_target": row.get("priceTarget"),
                "price_when_posted": row.get("priceWhenPosted"),
            },
        ))
    if out:
        return out

    # Fallback only: market-wide stock news can catch "upgrade/downgrade"
    # headlines if the cleaner price-target feed is empty.
    for row in fetch_news(client, "analyst"):
        symbol = clean_symbol(row.get("symbol"))
        dt = parse_dt(row.get("publishedDate") or row.get("date"))
        title = str(row.get("title") or "")
        if not symbol or (universe and symbol not in universe) or not is_recent(dt, cutoff):
            continue
        if not text_has(title, ANALYST_KEYWORDS):
            continue
        score = 2.0 if "upgrade" in title.lower() or "raised" in title.lower() or "raises" in title.lower() else 1.0
        out.append(record(symbol, "analyst", title, dt, "stable/news/stock-latest", score=score, extra={"news_url": row.get("url"), "publisher": row.get("publisher")}))
    return out


def insider_activity(client: FmpClient, cutoff: datetime, universe: set[str]) -> list[dict]:
    trades: list[dict] = []
    for page in range(3):
        data = client.get(f"stable/insider-trading/latest?page={page}&limit=100", "insider")
        if isinstance(data, list):
            trades.extend(data)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in trades:
        symbol = clean_symbol(row.get("symbol"))
        dt = parse_dt(row.get("transactionDate") or row.get("filingDate"))
        acq = str(row.get("acquisitionOrDisposition") or "").upper()
        tx_type = str(row.get("transactionType") or "").lower()
        shares = as_num(row.get("securitiesTransacted"), 0) or 0
        price = as_num(row.get("price"), 0) or 0
        if not symbol or (universe and symbol not in universe) or not is_recent(dt, cutoff):
            continue
        if acq not in {"A", "BUY"} and "purchase" not in tx_type:
            continue
        if shares <= 0 or price <= 0:
            continue
        grouped[symbol].append(row)

    out = []
    for symbol, rows in grouped.items():
        total_value = sum((as_num(r.get("securitiesTransacted"), 0) or 0) * (as_num(r.get("price"), 0) or 0) for r in rows)
        latest = max((parse_dt(r.get("transactionDate") or r.get("filingDate")) for r in rows), default=None)
        cluster = len({str(r.get("reportingName") or "") for r in rows})
        summary = f"{cluster} insider buy(s), approx ${total_value:,.0f} total"
        out.append(record(symbol, "insider", summary, latest, "stable/insider-trading/latest", score=min(5.0, cluster + total_value / 500_000), extra={"insider_buy_count": len(rows), "insider_buy_value": round(total_value, 2)}))
    return sorted(out, key=lambda r: r["catalyst_score"], reverse=True)


def corporate_events(client: FmpClient, cutoff: datetime, universe: set[str]) -> list[dict]:
    out: list[dict] = []
    for endpoint, rows in [
        ("stable/news/stock-latest", fetch_news(client, "corporate")),
        ("stable/news/press-releases-latest", fetch_press_releases(client, "corporate")),
    ]:
        for row in rows:
            symbol = clean_symbol(row.get("symbol"))
            dt = parse_dt(row.get("publishedDate") or row.get("date"))
            title = str(row.get("title") or "")
            text = f"{title} {row.get('text') or ''}"
            if not symbol or (universe and symbol not in universe) or not is_recent(dt, cutoff):
                continue
            if text_has(title, CORPORATE_KEYWORDS):
                out.append(record(symbol, "corporate", title, dt, endpoint, score=1.5, extra={"news_url": row.get("url"), "publisher": row.get("publisher")}))
    return out


def fda_biotech(client: FmpClient, cutoff: datetime, universe: set[str]) -> list[dict]:
    out: list[dict] = []
    for endpoint, rows in [
        ("stable/news/stock-latest", fetch_news(client, "fda")),
        ("stable/news/press-releases-latest", fetch_press_releases(client, "fda")),
    ]:
        for row in rows:
            symbol = clean_symbol(row.get("symbol"))
            dt = parse_dt(row.get("publishedDate") or row.get("date"))
            title = str(row.get("title") or "")
            text = f"{title} {row.get('text') or ''}"
            if not symbol or (universe and symbol not in universe) or not is_recent(dt, cutoff):
                continue
            if text_has(text, FDA_KEYWORDS) and (text_has(text, BIOTECH_WORDS) or "fda" in text.lower()):
                out.append(record(symbol, "fda", f"{title} [binary biotech/FDA risk]", dt, endpoint, score=2.0, extra={"news_url": row.get("url"), "publisher": row.get("publisher"), "binary_event_risk": True}))
    return out



def load_finviz_candidates(today: str, universe: set[str]) -> list[dict]:
    """Load Finviz new-highs / unusual-volume signals as bypass candidates."""
    path = ROOT / "data" / "finviz_signals.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if data.get("date") != today:
        return []

    candidates: list[dict] = []
    existing = set()

    for ticker in data.get("new_highs", [])[:50]:
        ticker = clean_symbol(ticker)
        if not ticker or ticker in existing:
            continue
        existing.add(ticker)
        candidates.append(record(
            ticker,
            "finviz_new_high",
            "Breaking to new 52wk high",
            datetime.now(timezone.utc),
            "finviz_screener",
            score=5.0,
            extra={
                "bypasses_technical": True,
                "bypass_reason": "finviz_new_52wk_high",
                "catalyst_quality_score": 50,
            },
        ))

    for ticker in data.get("unusual_vol", [])[:50]:
        ticker = clean_symbol(ticker)
        if not ticker or ticker in existing:
            continue
        existing.add(ticker)
        candidates.append(record(
            ticker,
            "finviz_unusual_volume",
            "Unusual volume on Finviz screener",
            datetime.now(timezone.utc),
            "finviz_screener",
            score=3.5,
            extra={
                "bypasses_technical": True,
                "bypass_reason": "finviz_unusual_volume",
                "catalyst_quality_score": 35,
            },
        ))

    # Only bypass tickers that are NOT already in the base universe
    candidates = [c for c in candidates if c["symbol"] not in universe]
    if candidates:
        print(f"Finviz bypass candidates: {len(candidates)}")
    return candidates

def load_bypass_candidates(today: str) -> list[dict]:
    """Load social, insider, and congressional bypass candidates.

    These tickers bypass the B2/B3 technical gate and enter as Set 3
    catalyst candidates. Only fresh files dated today are used.
    """
    bypass_sources = [
        ("data/social_discovery.json", "social_discovery"),
        ("data/insider_discovery.json", "insider_discovery"),
        ("data/congress_discovery.json", "congress_discovery"),
        ("data/danelfin_discovery.json", "danelfin_discovery"),
        ("data/universe_expansion.json", "continuous_discovery"),
    ]
    candidates: list[dict] = []
    for filename, default_source in bypass_sources:
        full_path = ROOT / filename
        if not full_path.exists():
            continue
        try:
            bp_data = json.loads(full_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if bp_data.get("date") != today:
            continue
        raw_candidates = bp_data.get("candidates", [])
        if default_source == "continuous_discovery":
            raw_candidates = bp_data.get("additions", [])
        for c in raw_candidates:
            if not isinstance(c, dict):
                continue
            ticker = clean_symbol(c.get("ticker") or c.get("symbol"))
            if not ticker:
                continue
            record = {
                "ticker": ticker,
                "symbol": ticker,
                "source": c.get("source", default_source),
                "sub_type": c.get("sub_type", "bypass"),
                "sub_types": [c.get("sub_type", "bypass")],
                "catalyst_summary": str(c.get("catalyst_summary", ""))[:240],
                "catalyst_date": today,
                "catalyst_datetime": datetime.now(timezone.utc).isoformat(),
                "catalyst_score": 5.0,
                "catalyst_sources": [c.get("source", default_source)],
                "bypasses_technical": True,
                "bypass_reason": c.get("bypass_reason", ""),
                "bypass_technical": c.get("bypass_technical", True),
                "vetted": c.get("vetted", False),
            }
            # Preserve extra fields for downstream scoring / dashboard
            for key in (
                "social_score", "stocktwits_bull_pct", "stocktwits_message_count",
                "reddit_bull_pct", "reddit_high_quality_posts",
                "getxapi_bull_pct", "getxapi_tweet_count",
                "insider_buy_value", "insider_buy_count", "insider_buy_qty", "unique_insiders",
                "congress_buy_count", "congress_politician_count", "congress_total_amount",
                "congress_signal", "politicians", "chambers",
                "aiscore", "low_risk", "fundamental", "technical", "sentiment",
                "sources_detected", "added_reason", "detected_at", "vetted",
            ):
                if key in c:
                    record[key] = c[key]

            if default_source == "continuous_discovery":
                record["sub_type"] = c.get("sub_type", "system1_confluence")
                record["sub_types"] = [record["sub_type"]]
                record["catalyst_sources"] = ["continuous_discovery"]
                record["bypasses_technical"] = False
                record["bypass_technical"] = False
                record["bypass_reason"] = "system1_confluence_unvetted"
                record["catalyst_summary"] = str(
                    c.get("catalyst_summary")
                    or c.get("added_reason")
                    or "System 1 continuous discovery confluence"
                )[:240]

            # Highest-conviction Danelfin names bypass the technical gate entirely
            if default_source == "danelfin_discovery":
                aiscore = float(c.get("aiscore") or 0)
                low_risk = float(c.get("low_risk") or 0)
                if aiscore < 9 or low_risk < 7:
                    continue
                record["bypass_reason"] = "danelfin_highest_conviction"
                record["catalyst_summary"] = f"Danelfin AI {aiscore:.0f}/10, Low Risk {low_risk:.0f}/10"

            candidates.append(record)
            print(f"Bypass candidate: {ticker} ({record.get('bypass_reason')})")
    return candidates


def dedupe(records: list[dict], limit: int) -> list[dict]:
    by_symbol: dict[str, dict] = {}
    for rec in sorted(records, key=lambda r: (r.get("catalyst_score", 0), r.get("catalyst_datetime", "")), reverse=True):
        symbol = rec["symbol"]
        if symbol not in by_symbol:
            by_symbol[symbol] = rec
            continue
        existing = by_symbol[symbol]
        if rec["sub_type"] not in existing["sub_types"]:
            existing["sub_types"].append(rec["sub_type"])
        existing["sub_type"] = "|".join(existing["sub_types"])
        existing["catalyst_summary"] = "; ".join(dict.fromkeys([existing["catalyst_summary"], rec["catalyst_summary"]]))[:360]
        existing["catalyst_sources"] = sorted(set(existing.get("catalyst_sources", []) + rec.get("catalyst_sources", [])))
        existing["catalyst_score"] = round(existing.get("catalyst_score", 0) + rec.get("catalyst_score", 0), 3)
        if rec.get("catalyst_datetime", "") > existing.get("catalyst_datetime", ""):
            existing["catalyst_date"] = rec["catalyst_date"]
            existing["catalyst_datetime"] = rec["catalyst_datetime"]
    ranked = sorted(by_symbol.values(), key=lambda r: (r.get("catalyst_score", 0), r.get("catalyst_datetime", "")), reverse=True)
    return ranked[:limit]


def build_candidate_pool(universe_rows: list[Any], catalysts: list[dict]) -> list[dict]:
    pool: dict[str, dict] = {}
    for row in universe_rows:
        symbol = clean_symbol(row.get("symbol") or row.get("ticker")) if isinstance(row, dict) else clean_symbol(row)
        if not symbol:
            continue
        base = {**row} if isinstance(row, dict) else {"symbol": symbol}
        base.setdefault("symbol", symbol)
        base.setdefault("ticker", symbol)
        base.setdefault("source", "scanner")
        pool[symbol] = base
    for cat in catalysts:
        symbol = cat["symbol"]
        pool[symbol] = {**pool.get(symbol, {"symbol": symbol, "ticker": symbol}), **cat}
    return sorted(pool.values(), key=lambda r: (r.get("source") != "catalyst", r["symbol"]))


def collect_all_catalysts(client: FmpClient, cutoff: datetime, universe: set[str]) -> tuple[dict[str, list[dict]], list[dict]]:
    by_type: dict[str, list[dict]] = {}
    collectors = {
        "earnings": earnings_surprises,
        "analyst": analyst_changes,
        "insider": insider_activity,
        "corporate": corporate_events,
        "fda": fda_biotech,
    }
    for name, fn in collectors.items():
        try:
            by_type[name] = fn(client, cutoff, universe)
        except Exception as exc:
            client.errors.append(f"{name}: {exc}")
            client.errors_by_type[name].append(str(exc))
            by_type[name] = []
    all_records = [item for rows in by_type.values() for item in rows]
    return by_type, all_records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    started = time.time()
    load_dotenv()
    api_key = load_fmp_key()
    universe_rows = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    universe = {
        clean_symbol(row.get("symbol") or row.get("ticker")) if isinstance(row, dict) else clean_symbol(row)
        for row in universe_rows
    }
    universe.discard("")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.lookback_hours)
    run_date = datetime.now(timezone.utc).date().isoformat()
    client = FmpClient(api_key)

    by_type, all_records = collect_all_catalysts(client, cutoff, universe)

    # Merge bypass candidates from social / insider / congressional / finviz discovery
    bypass_candidates = load_bypass_candidates(run_date)
    finviz_candidates = load_finviz_candidates(run_date, universe)
    if finviz_candidates:
        bypass_candidates.extend(finviz_candidates)
        by_type["finviz"] = finviz_candidates
    if bypass_candidates:
        existing_tickers = {clean_symbol(r.get("symbol") or r.get("ticker")) for r in all_records}
        for c in bypass_candidates:
            if c["symbol"] not in existing_tickers:
                all_records.append(c)
        by_type["bypass"] = bypass_candidates

    catalysts = dedupe(all_records, args.limit)
    candidate_pool = build_candidate_pool(universe_rows, catalysts)

    # Additive pre-cap research ledger. Never changes the production ranking/output.
    if not args.dry_run:
        try:
            from research_provenance import write_catalyst_pre_cap
            write_catalyst_pre_cap(all_records, catalysts, universe, args.limit)
        except Exception as exc:
            print(f"Research pre-cap ledger failed (production unaffected): {type(exc).__name__}: {exc}")

    metadata = {
        "stage": "CatalystDiscovery",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "lookbackHours": args.lookback_hours,
        "universeCount": len(universe),
        "rawCatalystCounts": {k: len(v) for k, v in by_type.items()},
        "dedupedCatalystCount": len(catalysts),
        "candidatePoolCount": len(candidate_pool),
        "bypassCandidateCount": len(bypass_candidates),
        "fmpCallCount": client.calls,
        "fmpCallsByType": dict(client.calls_by_type),
        "fmpErrorCount": len(client.errors),
        "fmpErrorsByType": {k: v[:5] for k, v in client.errors_by_type.items()},
        "runtimeSeconds": round(time.time() - started, 2),
        "rideAlongOnly": True,
        "selectionLogicChanged": False,
    }

    if not args.dry_run:
        CATALYST_PATH.write_text(json.dumps(catalysts, indent=2), encoding="utf-8")
        CANDIDATE_POOL_PATH.write_text(json.dumps(candidate_pool, indent=2), encoding="utf-8")
        META_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(json.dumps({
        "dedupedCatalystCount": len(catalysts),
        "candidatePoolCount": len(candidate_pool),
        "bypassCandidateCount": len(bypass_candidates),
        "rawCatalystCounts": metadata["rawCatalystCounts"],
        "fmpCallCount": client.calls,
        "errorsByType": metadata["fmpErrorsByType"],
        "sampleByType": {k: v[:5] for k, v in by_type.items()},
        "topCatalysts": catalysts[:10],
    }, indent=2))


if __name__ == "__main__":
    main()
