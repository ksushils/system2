#!/usr/bin/env python3
"""Lean GetXAPI discovery feed for the System 2 top-of-funnel candidate pool."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UNIVERSE = ROOT / "universe.json"
POOL = ROOT / "candidate_pool.json"
OUTPUT = ROOT / "x_candidates.json"
META = ROOT / "x_discovery_metadata.json"
ENDPOINT = "https://api.getxapi.com/twitter/tweet/advanced_search"


def load_dotenv():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def symbol_of(item):
    value = (item.get("symbol") or item.get("ticker")) if isinstance(item, dict) else item
    return str(value or "").upper()


def main():
    load_dotenv()
    key = os.getenv("GETXAPI_KEY")
    if not key:
        raise RuntimeError("GETXAPI_KEY missing")
    raw_universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    universe = {symbol_of(x) for x in raw_universe}
    query = '(from:unusual_whales OR from:Benzinga OR from:DeItaone OR from:StockMKTNewz OR from:IBDinvestors OR from:BreakoutStocks) (stock OR shares OR breakout OR upgrade OR catalyst OR watch) lang:en -is:retweet'
    url = ENDPOINT + "?" + urllib.parse.urlencode({"q": query, "product": "Latest"})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    tweets = data.get("tweets") or []
    by_symbol = {}
    for tweet in tweets:
        text = str(tweet.get("text") or "")
        for symbol in re.findall(r"\$([A-Z]{1,5})(?![A-Z])", text.upper()):
            if symbol not in universe:
                continue
            row = by_symbol.setdefault(symbol, {
                "symbol": symbol, "ticker": symbol, "source": "X",
                "x_reason": "trusted financial account mention",
                "x_mentions": 0, "x_authors": [], "x_sample_posts": [],
            })
            row["x_mentions"] += 1
            author = str((tweet.get("author") or {}).get("userName") or "unknown")
            if author not in row["x_authors"]:
                row["x_authors"].append(author)
            if len(row["x_sample_posts"]) < 3:
                row["x_sample_posts"].append({"author": author, "text": text[:280], "url": tweet.get("url")})
    candidates = sorted(by_symbol.values(), key=lambda row: row["x_mentions"], reverse=True)[:15]
    base = json.loads(POOL.read_text(encoding="utf-8")) if POOL.exists() else [
        x if isinstance(x, dict) else {"symbol": x, "ticker": x, "source": "scanner"} for x in raw_universe
    ]
    merged = {symbol_of(x): x for x in base}
    for row in candidates:
        old = merged.get(row["symbol"], {})
        sources = set(old.get("all_sources") or [old.get("source") or "scanner"])
        sources.add("X")
        merged[row["symbol"]] = {**old, **row, "all_sources": sorted(sources), "source_count": len(sources)}
    POOL.write_text(json.dumps(list(merged.values()), indent=2), encoding="utf-8")
    generated_at = datetime.now(timezone.utc).isoformat()
    OUTPUT.write_text(json.dumps({
        "date": generated_at[:10],
        "generated_at": generated_at,
        "source": "GetXAPI",
        "count": len(candidates),
        "candidates": candidates,
    }, indent=2), encoding="utf-8")
    meta = {
        "created_at": generated_at, "provider": "GetXAPI",
        "tweets_fetched": len(tweets), "candidates": len(candidates),
        "symbols": [row["symbol"] for row in candidates], "candidate_pool_count": len(merged),
        "paper_only": True,
    }
    META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
