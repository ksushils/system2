#!/usr/bin/env python3
"""
C3 Stage 3 news safety kill-filter.

Runs immediately after the Stage 2 top-40 technical selection.
This is the one safety layer allowed to hard-remove a paper finalist from day
one when fresh news contains a hard landmine.

It does not rank, promote, resize, or add standing fundamentals. If FMP news
is unavailable for a ticker, the ticker is kept with NO_DATA.
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
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path.home() / "Downloads"
INPUT_PATH = ROOT / "stage7_clustered_survivors.json"
OUTPUT_PATH = ROOT / "stage3_news_safe_top40.json"
REJECTIONS_PATH = ROOT / "stage3_news_rejections.json"
META_PATH = ROOT / "stage3_news_metadata.json"
NEWS_CATALYST_PATH = ROOT / "data" / "news_catalyst.json"
FMP_BASE = "https://financialmodelingprep.com"


def load_news_catalyst_dangers(path: Path) -> dict[str, bool]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        results = data.get("results", {})
        return {
            str(k).upper(): bool(v.get("has_danger") or v.get("news_verdict") == "DANGER")
            for k, v in results.items()
            if isinstance(v, dict)
        }
    except Exception:
        return {}

LANdMINE_PATTERNS = {
    "offering": [
        r"\bpublic offering\b", r"\bregistered direct\b", r"\bat[- ]the[- ]market\b",
        r"\bATM offering\b", r"\bprivate placement\b", r"\bshare offering\b",
        r"\bsecondary offering\b", r"\bdilut(?:ion|ive|ed|es)\b",
    ],
    "halt": [r"\btrading halt\b", r"\bhalted\b", r"\bsuspended trading\b"],
    "going_concern": [r"\bgoing concern\b", r"\bsubstantial doubt\b"],
    "fraud_investigation": [
        r"\bfraud\b", r"\binvestigation\b", r"\bSEC investigation\b",
        r"\bDOJ\b", r"\bclass action\b", r"\bwhistleblower\b",
    ],
    "major_litigation": [
        r"\blawsuit\b", r"\blitigation\b", r"\bsued\b", r"\bsettlement\b",
        r"\bpatent infringement\b",
    ],
    "guidance_withdrawal": [
        r"\bwithdraw(?:s|n|ing)? guidance\b", r"\bguidance withdrawn\b",
        r"\bsuspends guidance\b", r"\bcuts guidance\b",
    ],
}

ANALYST_PATTERNS = [
    r"\bupgrade[ds]?\b", r"\bdowngrade[ds]?\b", r"\bprice target\b",
    r"\braises target\b", r"\blowers target\b", r"\binitiates coverage\b",
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
        if key and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip("\"'")


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
        sep = "&" if "?" in endpoint else "?"
        url = f"{FMP_BASE}/{endpoint}{sep}apikey={urllib.parse.quote(self.api_key)}"
        self.calls += 1
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url,
                    headers={"Accept": "application/json", "User-Agent": "system2-stage3-news-safety/1.0"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", "ignore"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                self.errors.append(f"{endpoint}: HTTP {exc.code}")
                return None
            except Exception as exc:
                if attempt < 2:
                    time.sleep(1 * (attempt + 1))
                    continue
                self.errors.append(f"{endpoint}: {exc}")
                return None
        return None


def parse_dt(row: dict) -> datetime | None:
    value = row.get("publishedDate") or row.get("date") or row.get("createdAt")
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def recent(row: dict, cutoff: datetime) -> bool:
    dt = parse_dt(row)
    return dt is None or dt >= cutoff


def row_text(row: dict) -> str:
    return " ".join(str(row.get(k) or "") for k in ["title", "text", "content", "site", "symbol"]).strip()


def match_patterns(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


def summarize(row: dict) -> str:
    title = str(row.get("title") or row.get("text") or row.get("content") or "news item").strip()
    return re.sub(r"\s+", " ", title)[:220]


def inspect_symbol(client: FmpClient, symbol: str, cutoff: datetime) -> dict:
    quoted = urllib.parse.quote(symbol)
    endpoint_groups = [
        [
            f"api/v3/stock_news?tickers={quoted}&limit=10",
            f"stable/news/stock?symbols={quoted}&limit=10",
        ],
        [
            f"api/v3/press-releases/{quoted}?limit=10",
            f"stable/news/press-releases?symbols={quoted}&limit=10",
        ],
    ]
    rows: list[dict] = []
    endpoint_errors = []
    endpoints_with_data = 0
    for endpoints in endpoint_groups:
        group_data = None
        group_endpoint = None
        for endpoint in endpoints:
            data = client.get(endpoint)
            if isinstance(data, list):
                group_data = data
                group_endpoint = endpoint
                break
            endpoint_errors.append(endpoint)
        if isinstance(group_data, list):
            endpoints_with_data += 1
            rows.extend({**row, "_endpoint": group_endpoint} for row in group_data if isinstance(row, dict))

    if endpoints_with_data == 0:
        return {
            "status": "NO_DATA",
            "remove": False,
            "remove_reason": None,
            "hard_landmine": None,
            "analyst_change": None,
            "news_items": [],
            "recent_items_checked": 0,
            "endpoint_errors": endpoint_errors,
        }

    recent_rows = [row for row in rows if recent(row, cutoff)]
    news_items = [
        {
            "title": summarize(row),
            "date": (parse_dt(row) or datetime.now(timezone.utc)).isoformat(),
            "publisher": row.get("publisher") or row.get("site"),
            "endpoint": row.get("_endpoint"),
            "url": row.get("url"),
        }
        for row in recent_rows
    ]
    landmines = []
    analyst_changes = []
    for row in recent_rows:
        text = row_text(row)
        for kind, patterns in LANdMINE_PATTERNS.items():
            if match_patterns(text, patterns):
                landmines.append({
                    "type": kind,
                    "summary": summarize(row),
                    "date": (parse_dt(row) or datetime.now(timezone.utc)).isoformat(),
                    "endpoint": row.get("_endpoint"),
                })
                break
        if match_patterns(text, ANALYST_PATTERNS):
            analyst_changes.append({
                "summary": summarize(row),
                "date": (parse_dt(row) or datetime.now(timezone.utc)).isoformat(),
                "endpoint": row.get("_endpoint"),
            })

    if landmines:
        first = landmines[0]
        return {
            "status": "FORCE_SKIP",
            "remove": True,
            "remove_reason": first["type"],
            "hard_landmine": first,
            "analyst_change": analyst_changes[0] if analyst_changes else None,
            "news_items": news_items,
            "recent_items_checked": len(recent_rows),
            "endpoint_errors": endpoint_errors,
        }

    return {
        "status": "PASS",
        "remove": False,
        "remove_reason": None,
        "hard_landmine": None,
        "analyst_change": analyst_changes[0] if analyst_changes else None,
        "news_items": news_items,
        "recent_items_checked": len(recent_rows),
        "endpoint_errors": endpoint_errors,
    }


def fetch_analyst_changes(client: FmpClient, symbols: set[str], cutoff: datetime) -> dict[str, dict]:
    out: dict[str, dict] = {}
    rows = client.get("stable/price-target-latest-news?page=0&limit=200")
    if not isinstance(rows, list):
        return out
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        if sym not in symbols or sym in out or not recent(row, cutoff):
            continue
        title = str(row.get("newsTitle") or row.get("title") or "").strip()
        out[sym] = {
            "summary": re.sub(r"\s+", " ", title)[:220],
            "date": (parse_dt(row) or datetime.now(timezone.utc)).isoformat(),
            "endpoint": "stable/price-target-latest-news",
            "publisher": row.get("newsPublisher") or row.get("publisher"),
            "analyst_company": row.get("analystCompany"),
            "url": row.get("newsURL") or row.get("url"),
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(INPUT_PATH))
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--rejections", default=str(REJECTIONS_PATH))
    parser.add_argument("--metadata", default=str(META_PATH))
    parser.add_argument("--lookback-hours", type=int, default=72)
    args = parser.parse_args()

    started = time.time()
    load_dotenv()
    finalists = json.loads(Path(args.input).read_text(encoding="utf-8"))
    client = FmpClient(load_fmp_key())
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.lookback_hours)
    finalist_symbols = {str((row.get("symbol") or row.get("ticker") or "")).upper() for row in finalists}
    analyst_by_symbol = fetch_analyst_changes(client, finalist_symbols, cutoff)
    news_catalyst_dangers = load_news_catalyst_dangers(NEWS_CATALYST_PATH)

    kept = []
    removed = []
    no_data = []
    analyst_change_count = 0

    for row in finalists:
        symbol = row.get("symbol") or row.get("ticker")
        result = inspect_symbol(client, symbol, cutoff)
        catalyst_danger = news_catalyst_dangers.get(str(symbol).upper(), False)
        enriched = {
            **row,
            "news_safety_status": result["status"],
            "news_safety_mode": "LIVE",
            "news_safety_checked_at": datetime.now(timezone.utc).isoformat(),
            "news_recent_items_checked": result["recent_items_checked"],
            "news_endpoint_errors": result["endpoint_errors"],
            "hard_landmine": result["hard_landmine"],
            "news_catalyst_danger": catalyst_danger,
            "analyst_change": analyst_by_symbol.get(symbol) or result["analyst_change"],
            "news_items": result.get("news_items") or [],
        }
        if result["analyst_change"]:
            analyst_change_count += 1
        if result["status"] == "NO_DATA":
            no_data.append(symbol)
        if result["remove"] or catalyst_danger:
            removed.append({
                **enriched,
                "stage3RejectReason": result["remove_reason"] if result["remove"] else "news_catalyst_danger",
                "stage3RejectDetail": result["hard_landmine"] if result["remove"] else {"type": "news_catalyst_danger", "summary": "news catalyst flagged danger"},
            })
        else:
            kept.append(enriched)

    reason_counts = Counter(r.get("stage3RejectReason") or "unknown" for r in removed)
    metadata = {
        "stage": "STAGE3_NEWS_SAFETY",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "mode": "LIVE",
        "inputCount": len(finalists),
        "outputCount": len(kept),
        "removedCount": len(removed),
        "reasonCounts": dict(reason_counts),
        "noDataCount": len(no_data),
        "noDataTickers": no_data,
        "analystChangeCount": analyst_change_count,
        "fmpCallCount": client.calls,
        "fmpErrorCount": len(client.errors),
        "fmpErrorsSample": client.errors[:20],
        "lookbackHours": args.lookback_hours,
        "failSafe": "NO_DATA keeps ticker; only explicit fresh hard landmines force-skip.",
        "selectionLogicChanged": "safety kill-filter only",
        "paperOnly": True,
        "runtimeSeconds": round(time.time() - started, 2),
    }

    Path(args.output).write_text(json.dumps(kept, indent=2), encoding="utf-8")
    Path(args.rejections).write_text(json.dumps(removed, indent=2), encoding="utf-8")
    Path(args.metadata).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps({
        "inputCount": metadata["inputCount"],
        "outputCount": metadata["outputCount"],
        "removedCount": metadata["removedCount"],
        "reasonCounts": metadata["reasonCounts"],
        "noDataCount": metadata["noDataCount"],
        "analystChangeCount": metadata["analystChangeCount"],
        "fmpCallCount": metadata["fmpCallCount"],
        "mode": "LIVE",
    }, indent=2))


if __name__ == "__main__":
    main()
