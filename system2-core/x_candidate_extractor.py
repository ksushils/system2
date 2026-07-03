#!/usr/bin/env python3
"""Lean trusted-handle X candidate discovery for the System 2 funnel."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
UNIVERSE_PATH = ROOT / "universe.json"
POOL_PATH = ROOT / "candidate_pool.json"
OUTPUT_PATH = ROOT / "x_candidates.json"
META_PATH = ROOT / "x_discovery_metadata.json"
ENDPOINT = "https://api.getxapi.com/twitter/tweet/advanced_search"
TRUSTED_HANDLES = [
    "unusual_whales",
    "KobeissiLetter",
    "markminervini",
    "TraderLion",
    "OptionsHawk",
]
MAX_CANDIDATES = 20


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def symbol_of(row: Any) -> str:
    value = row.get("symbol") or row.get("ticker") if isinstance(row, dict) else row
    return str(value or "").upper()


def tweet_rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("tweets", "data", "results"):
        if isinstance(payload.get(key), list):
            return [row for row in payload[key] if isinstance(row, dict)]
    return []


def tweet_text(row: dict) -> str:
    return str(row.get("text") or row.get("full_text") or "")


def author_handle(row: dict, fallback: str) -> str:
    author = row.get("author") or row.get("user") or {}
    value = (
        author.get("userName")
        or author.get("username")
        or author.get("screen_name")
        or row.get("author_username")
        or fallback
    )
    return str(value).lstrip("@")


def fetch_handle(handle: str, key: str) -> list[dict]:
    query = f"from:{handle} lang:en -is:retweet"
    url = ENDPOINT + "?" + urllib.parse.urlencode({"q": query, "product": "Latest"})
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return tweet_rows(json.loads(response.read().decode("utf-8", "ignore")))


def sentiment(texts: list[str]) -> int:
    joined = " ".join(texts).lower()
    positive = sum(joined.count(term) for term in (
        "breakout", "bullish", "upgrade", "beat", "strong", "buy", "higher",
    ))
    negative = sum(joined.count(term) for term in (
        "bearish", "downgrade", "miss", "weak", "sell", "lower", "risk",
    ))
    return max(0, min(100, 50 + positive * 5 - negative * 5))


def write_outputs(candidates: list[dict], metadata: dict) -> None:
    output = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "generated_at": metadata.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "source": "GetXAPI",
        "count": len(candidates),
        "candidates": candidates,
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    META_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def merge_candidate_pool(candidates: list[dict]) -> int:
    universe_rows = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    base = (
        json.loads(POOL_PATH.read_text(encoding="utf-8"))
        if POOL_PATH.exists()
        else [
            row if isinstance(row, dict) else {"symbol": row, "ticker": row, "source": "scanner"}
            for row in universe_rows
        ]
    )
    merged = {symbol_of(row): row for row in base if symbol_of(row)}
    for candidate in candidates:
        symbol = candidate["symbol"]
        old = merged.get(symbol, {})
        sources = set(old.get("all_sources") or [old.get("source") or "scanner"])
        sources.add("X")
        merged[symbol] = {
            **old,
            **candidate,
            "ticker": symbol,
            "source": "X",
            "all_sources": sorted(sources),
            "source_count": len(sources),
        }
    POOL_PATH.write_text(json.dumps(list(merged.values()), indent=2), encoding="utf-8")
    return len(merged)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--merge-only", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    if args.merge_only:
        saved = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else []
        candidates = saved.get("candidates", []) if isinstance(saved, dict) else saved
        pool_count = merge_candidate_pool(candidates)
        print(json.dumps({
            "status": "MERGED",
            "x_tickers_found": len(candidates),
            "candidate_pool_count": pool_count,
            "paper_only": True,
        }, indent=2))
        return

    key = os.environ.get("GETXAPI_KEY")
    started = datetime.now(timezone.utc)
    base_meta = {
        "created_at": started.isoformat(),
        "provider": "GetXAPI",
        "queries_planned": len(TRUSTED_HANDLES),
        "trusted_handles": TRUSTED_HANDLES,
        "minimum_unique_authors": 2,
        "paper_only": True,
    }
    if not key:
        metadata = {
            **base_meta,
            "status": "NO_KEY",
            "queries_completed": 0,
            "tweets_fetched": 0,
            "x_tickers_found": 0,
            "errors": ["GETXAPI_KEY missing"],
        }
        write_outputs([], metadata)
        print(json.dumps(metadata, indent=2))
        return

    grouped: dict[str, dict[str, Any]] = {}
    errors = []
    tweets_fetched = 0
    completed = 0
    for handle in TRUSTED_HANDLES:
        try:
            tweets = fetch_handle(handle, key)
            completed += 1
            tweets_fetched += len(tweets)
        except Exception as exc:
            errors.append(f"{handle}: {exc}")
            continue
        for tweet in tweets:
            text = tweet_text(tweet)
            author = author_handle(tweet, handle)
            for symbol in set(re.findall(r"\$([A-Z]{1,5})(?![A-Z])", text.upper())):
                group = grouped.setdefault(symbol, {"authors": set(), "posts": []})
                group["authors"].add(author.lower())
                group["posts"].append({"author": author, "text": text[:500]})

    candidates = []
    today = datetime.now(timezone.utc).date().isoformat()
    for symbol, group in grouped.items():
        if len(group["authors"]) < 2:
            continue
        texts = [post["text"] for post in group["posts"]]
        candidates.append({
            "symbol": symbol,
            "source": "X",
            "sub_type": "X",
            "catalyst_summary": (
                f"mentioned by {len(group['authors'])} trusted X authors "
                f"across {len(group['posts'])} posts"
            ),
            "post_count": len(group["posts"]),
            "sentiment_score": sentiment(texts),
            "catalyst_date": today,
        })
    candidates.sort(
        key=lambda row: (row["post_count"], row["sentiment_score"], row["symbol"]),
        reverse=True,
    )
    candidates = candidates[:MAX_CANDIDATES]
    pool_count = None if args.extract_only else merge_candidate_pool(candidates)
    metadata = {
        **base_meta,
        "status": "OK" if completed else "FAILED_CLEAN",
        "queries_completed": completed,
        "tweets_fetched": tweets_fetched,
        "x_tickers_found": len(candidates),
        "candidate_pool_count": pool_count,
        "symbols": [row["symbol"] for row in candidates],
        "errors": errors,
    }
    write_outputs(candidates, metadata)
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
