#!/usr/bin/env python3
"""Unified social sentiment scraper for System 2.

Supports two modes:
  1. DISCOVERY (default): scans the universe for high-conviction social
     momentum and writes data/social_discovery.json.
  2. ENRICHMENT (--enrich FILE): fetches StockTwits + ApeWisdom + GetXAPI
     sentiment for every ticker in the supplied candidate file and writes
     data/social_sentiment.json so scoring_engine.py can compute a unified
     social confirmation score.

Uses free public endpoints:
  - StockTwits public symbol stream
  - ApeWisdom aggregate Reddit + 4chan /biz mention rankings
  - GetXAPI advanced search (if GETXAPI_KEY is available)
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
UNIVERSE_PATH = ROOT / "universe.json"
DISCOVERY_PATH = ROOT / "data" / "social_discovery.json"
DISCOVERY_META_PATH = ROOT / "data" / "social_discovery_metadata.json"
SENTIMENT_PATH = ROOT / "data" / "social_sentiment.json"
SENTIMENT_META_PATH = ROOT / "data" / "social_sentiment_metadata.json"
APEWISDOM_SENTIMENT_PATH = ROOT / "data" / "apewisdom_sentiment.json"
LOG_DIR = ROOT / "logs"

MAX_TICKERS = 100
ENRICH_MAX_TICKERS = 60
REQUEST_DELAY = 0.25

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

BULLISH_WORDS = [
    "bullish", "moon", "rocket", "calls", "long", "buy", "undervalued", "breakout",
    "strong", "higher", "upgrade", "beat",
]
BEARISH_WORDS = [
    "bearish", "crash", "puts", "short", "sell", "overvalued", "dump",
    "weak", "lower", "downgrade", "miss",
]

_apewisdom_cache: dict[str, dict[str, Any]] | None = None


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


def load_universe() -> list[str]:
    if not UNIVERSE_PATH.exists():
        return []
    data = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [str(t).upper().strip() for t in data if t]
    if isinstance(data, dict) and "tickers" in data:
        return [str(t).upper().strip() for t in data["tickers"] if t]
    return []


def load_tickers_from_file(path: Path) -> list[str]:
    """Extract tickers from a System 2 stage file (list of dicts or dict with ideas)."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict):
        rows = (
            data.get("ideas")
            or data.get("candidates")
            or data.get("results")
            or []
        )
        rows = [r for r in rows if isinstance(r, dict)]

    tickers: list[str] = []
    for row in rows:
        sym = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
        if sym and sym not in tickers:
            tickers.append(sym)
    return tickers


def fetch_stocktwits(ticker: str) -> dict[str, Any] | None:
    """Fetch recent StockTwits message stream for a ticker."""
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{urllib.parse.quote(ticker)}.json"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 429):
            return None
        return {"error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"error": str(exc)}

    messages = data.get("messages") or []
    if not messages:
        return None

    bullish = 0
    bearish = 0
    neutral = 0
    for msg in messages:
        sent = msg.get("entities", {}).get("sentiment")
        sentiment = sent.get("basic") if isinstance(sent, dict) else ""
        if sentiment == "Bullish":
            bullish += 1
        elif sentiment == "Bearish":
            bearish += 1
        else:
            neutral += 1

    total = bullish + bearish + neutral
    bull_pct = (bullish / total * 100) if total > 0 else 50.0
    return {
        "message_count": len(messages),
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "bull_pct": round(bull_pct, 1),
    }


def fetch_apewisdom(universe: set[str] | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """ApeWisdom API — no auth required.

    Returns top mentioned stocks across all Reddit trading subreddits + 4chan.
    Key signal: rank_24h_ago vs current rank.
    """
    results: dict[str, dict[str, Any]] = {}
    try:
        for page in range(1, 3):  # 2 pages = ~200 stocks
            resp = requests.get(
                f"https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            for item in data.get("results", []):
                ticker = str(item.get("ticker", "")).upper().strip()
                if not ticker or "." in ticker or "-" in ticker or len(ticker) > 5:
                    continue
                if universe is not None and ticker not in universe:
                    continue
                rank_now = item.get("rank", 999)
                rank_24h = item.get("rank_24h_ago", 999)
                mentions = item.get("mentions", 0)
                mentions_24h = item.get("mentions_24h_ago", 0)
                upvotes = item.get("upvotes", 0)

                rank_improvement = (rank_24h or 999) - (rank_now or 999)
                mention_spike = (
                    mentions / max(mentions_24h, 1)
                    if mentions_24h and mentions_24h > 0
                    else 0.0
                )

                results[ticker] = {
                    "source": "apewisdom",
                    "rank": rank_now,
                    "rank_now": rank_now,
                    "rank_24h_ago": rank_24h,
                    "rank_improvement": rank_improvement,
                    "mentions": mentions,
                    "mentions_24h_ago": mentions_24h,
                    "mention_spike_ratio": round(mention_spike, 2),
                    "upvotes": upvotes,
                    "is_spiking": bool(rank_improvement > 50 or mention_spike > 3.0),
                }
            time.sleep(0.5)

        spiking = {k: v for k, v in results.items() if v["is_spiking"]}
        print(f"ApeWisdom: {len(results)} stocks, {len(spiking)} spiking")
        return results, spiking
    except Exception as e:
        print(f"ApeWisdom error: {e}")
        return {}, {}


def get_apewisdom_data(force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Load ApeWisdom once per process and cache."""
    global _apewisdom_cache
    if _apewisdom_cache is None or force_refresh:
        universe = set(load_universe())
        _apewisdom_cache, _ = fetch_apewisdom(universe=universe)
    return _apewisdom_cache


def save_apewisdom_sentiment() -> dict[str, Any]:
    """Fetch ApeWisdom and write the full snapshot to disk."""
    started = datetime.now(timezone.utc)
    universe = set(load_universe())
    results, spiking = fetch_apewisdom(universe=universe)
    top_spikes = sorted(
        [({"ticker": k, **v}) for k, v in spiking.items()],
        key=lambda x: x.get("rank_improvement", 0),
        reverse=True,
    )[:20]
    output = {
        "date": started.date().isoformat(),
        "generated_at": started.isoformat(),
        "source": "apewisdom",
        "total_tracked": len(results),
        "spiking_count": len(spiking),
        "stocks": results,
        "top_spikes": top_spikes,
    }
    APEWISDOM_SENTIMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    APEWISDOM_SENTIMENT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Wrote ApeWisdom snapshot: {len(results)} tracked, {len(spiking)} spiking")
    return output


def fetch_reddit(ticker: str) -> dict[str, Any] | None:
    """Search recent WSB / stocks posts mentioning the ticker (fallback)."""
    query = f"${ticker} OR {ticker}"
    subs = ["wallstreetbets", "stocks"]
    all_posts: list[dict] = []
    for sub in subs:
        url = (
            f"https://www.reddit.com/r/{sub}/search.json?"
            + urllib.parse.urlencode(
                {"q": query, "restrict_sr": "1", "sort": "new", "limit": "25"}
            )
        )
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "system2-social-discovery/1.0 (by /u/system2bot)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception:
            continue
        posts = data.get("data", {}).get("children", [])
        for post in posts:
            p = post.get("data", {})
            title = str(p.get("title", ""))
            score = int(p.get("score") or 0)
            all_posts.append({"title": title, "score": score})

    if not all_posts:
        return None

    text = " ".join(p["title"].lower() for p in all_posts)
    bull_hits = sum(text.count(w) for w in BULLISH_WORDS)
    bear_hits = sum(text.count(w) for w in BEARISH_WORDS)
    total_hits = bull_hits + bear_hits
    bull_pct = (bull_hits / total_hits * 100) if total_hits > 0 else 50.0
    return {
        "high_quality_posts": len(all_posts),
        "bull_pct": round(bull_pct, 1),
    }


def fetch_getxapi(ticker: str) -> dict[str, Any] | None:
    """Search GetXAPI for recent ticker mentions."""
    key = os.environ.get("GETXAPI_KEY")
    if not key:
        return None
    url = (
        "https://api.getxapi.com/twitter/tweet/advanced_search?"
        + urllib.parse.urlencode(
            {"q": f"${ticker} lang:en -is:retweet", "product": "Latest"}
        )
    )
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return None

    tweets: list[dict] = []
    if isinstance(data, list):
        tweets = data
    elif isinstance(data, dict):
        for k in ("tweets", "data", "results"):
            if isinstance(data.get(k), list):
                tweets = data[k]
                break

    if not tweets:
        return None

    texts = [str(t.get("text") or t.get("full_text") or "").lower() for t in tweets]
    joined = " ".join(texts)
    bull_hits = sum(joined.count(w) for w in BULLISH_WORDS)
    bear_hits = sum(joined.count(w) for w in BEARISH_WORDS)
    total = bull_hits + bear_hits
    bull_pct = (bull_hits / total * 100) if total > 0 else 50.0
    return {
        "tweet_count": len(tweets),
        "bull_pct": round(bull_pct, 1),
    }


def fetch_unified_social(ticker: str) -> dict[str, Any]:
    """Fetch all three sources for a single ticker and return a unified record."""
    st = fetch_stocktwits(ticker)
    time.sleep(REQUEST_DELAY)

    ape_data = get_apewisdom_data()
    ape = ape_data.get(ticker)
    if not ape:
        # Fallback to direct Reddit search only if ApeWisdom has no data
        rd = fetch_reddit(ticker)
        time.sleep(REQUEST_DELAY)
    else:
        rd = None

    gx = fetch_getxapi(ticker)

    sources_used: list[str] = []
    if st and not st.get("error"):
        sources_used.append("stocktwits")
    if ape:
        sources_used.append("apewisdom")
    elif rd:
        sources_used.append("reddit")
    if gx:
        sources_used.append("getxapi")

    scores = []
    weights = []
    if st and not st.get("error") and st.get("message_count", 0) > 0:
        scores.append(st["bull_pct"])
        weights.append(min(10, max(1, st["message_count"] / 5)))
    if ape:
        # Higher weight for spiking rank improvement; neutral-ish if just ranked
        ape_bull_pct = min(100.0, max(20.0, 50.0 + ape.get("rank_improvement", 0) * 0.5))
        scores.append(ape_bull_pct)
        weights.append(min(8, max(1, abs(ape.get("rank_improvement", 0)) / 20)))
    elif rd and rd.get("high_quality_posts", 0) > 0:
        scores.append(rd["bull_pct"])
        weights.append(min(5, max(0.5, rd["high_quality_posts"] / 2)))
    if gx and gx.get("tweet_count", 0) > 0:
        scores.append(gx["bull_pct"])
        weights.append(min(5, max(0.5, gx["tweet_count"] / 2)))

    if scores:
        social_score = round(
            sum(s * w for s, w in zip(scores, weights)) / sum(weights), 1
        )
        social_bull_pct = round(sum(scores) / len(scores), 1)
    else:
        social_score = 0.0
        social_bull_pct = 0.0

    return {
        "ticker": ticker,
        "social_score": social_score,
        "social_sources": sources_used,
        "social_bull_pct": social_bull_pct,
        "stocktwits_bull_pct": st.get("bull_pct", 0.0) if st and not st.get("error") else 0.0,
        "stocktwits_message_count": st.get("message_count", 0) if st and not st.get("error") else 0,
        "apewisdom_rank": ape.get("rank") if ape else None,
        "apewisdom_rank_now": ape.get("rank_now") if ape else None,
        "apewisdom_rank_24h_ago": ape.get("rank_24h_ago") if ape else None,
        "apewisdom_rank_improvement": ape.get("rank_improvement") if ape else None,
        "apewisdom_mentions": ape.get("mentions") if ape else None,
        "apewisdom_mentions_24h_ago": ape.get("mentions_24h_ago") if ape else None,
        "apewisdom_mention_spike_ratio": ape.get("mention_spike_ratio") if ape else None,
        "apewisdom_upvotes": ape.get("upvotes") if ape else None,
        "apewisdom_is_spiking": ape.get("is_spiking") if ape else False,
        "reddit_bull_pct": rd.get("bull_pct", 0.0) if rd else 0.0,
        "reddit_high_quality_posts": rd.get("high_quality_posts", 0) if rd else 0,
        "getxapi_bull_pct": gx.get("bull_pct", 0.0) if gx else 0.0,
        "getxapi_tweet_count": gx.get("tweet_count", 0) if gx else 0,
        "getxapi_active": bool(gx),
    }


def get_social_discovery_candidates() -> list[dict[str, Any]]:
    """Return tickers with strong social signal that bypass the technical gate."""
    universe = load_universe()
    if not universe:
        return []

    # Prime ApeWisdom cache and persist snapshot
    ape_output = save_apewisdom_sentiment()
    ape_data = ape_output.get("stocks", {})

    ticker_list = universe[:MAX_TICKERS]
    discovery_candidates: list[dict[str, Any]] = []

    for ticker in ticker_list:
        if not ticker:
            continue
        st = fetch_stocktwits(ticker)
        time.sleep(REQUEST_DELAY)

        if not st or st.get("error"):
            continue

        is_strong = st.get("bull_pct", 0) > 70 and st.get("message_count", 0) > 10
        if not is_strong:
            continue

        ape = ape_data.get(ticker)
        rd = fetch_reddit(ticker) if not ape else None
        time.sleep(REQUEST_DELAY)
        gx = fetch_getxapi(ticker)

        sources_used: list[str] = ["stocktwits"]
        if ape:
            sources_used.append("apewisdom")
        elif rd:
            sources_used.append("reddit")
        if gx:
            sources_used.append("getxapi")

        discovery_candidates.append({
            "ticker": ticker,
            "symbol": ticker,
            "source": "social_discovery",
            "sub_type": "social_momentum",
            "catalyst_summary": (
                f"StockTwits {st['bull_pct']:.0f}% "
                f"bullish ({st['message_count']} messages)"
            ),
            "social_score": st.get("bull_pct", 0),
            "social_sources": sources_used,
            "social_bull_pct": st.get("bull_pct", 0),
            "stocktwits_bull_pct": st.get("bull_pct", 0),
            "stocktwits_message_count": st.get("message_count", 0),
            "apewisdom_rank": ape.get("rank") if ape else None,
            "apewisdom_rank_now": ape.get("rank_now") if ape else None,
            "apewisdom_rank_improvement": ape.get("rank_improvement") if ape else None,
            "apewisdom_mentions": ape.get("mentions") if ape else None,
            "apewisdom_is_spiking": ape.get("is_spiking") if ape else False,
            "reddit_bull_pct": rd.get("bull_pct", 0) if rd else 0,
            "reddit_high_quality_posts": rd.get("high_quality_posts", 0) if rd else 0,
            "getxapi_bull_pct": gx.get("bull_pct", 0) if gx else 0.0,
            "getxapi_tweet_count": gx.get("tweet_count", 0) if gx else 0,
            "getxapi_active": bool(gx),
            "bypass_technical": True,
            "bypass_reason": "strong_social_signal",
        })

    # Add ApeWisdom-only spikes that bypass on rank improvement
    for ticker, ape in ape_data.items():
        if ticker in {c["ticker"] for c in discovery_candidates}:
            continue
        if ape.get("rank_improvement", 0) > 100 and ape.get("mentions", 0) > 10:
            discovery_candidates.append({
                "ticker": ticker,
                "symbol": ticker,
                "source": "social_discovery",
                "sub_type": "apewisdom_spike",
                "catalyst_summary": (
                    f"ApeWisdom rank jumped {ape['rank_improvement']} places "
                    f"to {ape['rank']} with {ape['mentions']} mentions"
                ),
                "social_score": min(100.0, max(50.0, 50.0 + ape["rank_improvement"] * 0.3)),
                "social_sources": ["apewisdom"],
                "social_bull_pct": 70.0,
                "apewisdom_rank": ape.get("rank"),
                "apewisdom_rank_now": ape.get("rank_now"),
                "apewisdom_rank_improvement": ape.get("rank_improvement"),
                "apewisdom_mentions": ape.get("mentions"),
                "apewisdom_mentions_24h_ago": ape.get("mentions_24h_ago"),
                "apewisdom_mention_spike_ratio": ape.get("mention_spike_ratio"),
                "apewisdom_upvotes": ape.get("upvotes"),
                "apewisdom_is_spiking": ape.get("is_spiking"),
                "bypass_technical": True,
                "bypass_reason": "apewisdom_spike",
            })

    return discovery_candidates


def enrich_candidates(input_path: Path, max_tickers: int = ENRICH_MAX_TICKERS) -> dict[str, Any]:
    """Enrich a list of candidates with unified social sentiment."""
    started = datetime.now(timezone.utc)
    tickers = load_tickers_from_file(input_path)[:max_tickers]
    if not tickers:
        return {
            "date": started.date().isoformat(),
            "generated_at": started.isoformat(),
            "input": str(input_path),
            "count": 0,
            "tickers": {},
        }

    # Ensure ApeWisdom snapshot is on disk and cached for enrichment
    save_apewisdom_sentiment()

    enriched: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for i, ticker in enumerate(tickers):
        try:
            enriched[ticker] = fetch_unified_social(ticker)
        except Exception as exc:
            errors.append(f"{ticker}: {exc}")
            enriched[ticker] = {
                "ticker": ticker,
                "social_score": 0.0,
                "social_sources": [],
                "error": str(exc),
            }
        if i < len(tickers) - 1:
            time.sleep(REQUEST_DELAY)

    today = started.date().isoformat()
    return {
        "date": today,
        "generated_at": started.isoformat(),
        "input": str(input_path),
        "count": len(enriched),
        "sources_considered": ["stocktwits", "apewisdom", "reddit", "getxapi"],
        "errors": errors,
        "tickers": enriched,
    }


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    load_dotenv()
    started = datetime.now(timezone.utc)

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--enrich",
        metavar="INPUT_FILE",
        help="Path to a System 2 stage JSON file to enrich with social sentiment",
    )
    parser.add_argument(
        "--output",
        default=str(SENTIMENT_PATH),
        help="Output path for enrichment mode (default: data/social_sentiment.json)",
    )
    parser.add_argument(
        "--max-tickers",
        type=int,
        default=ENRICH_MAX_TICKERS,
        help="Maximum tickers to enrich",
    )
    args = parser.parse_args()

    if args.enrich:
        input_path = Path(args.enrich)
        output_path = Path(args.output)
        output = enrich_candidates(input_path, max_tickers=args.max_tickers)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

        metadata = {
            "stage": "SOCIAL_SENTIMENT",
            "createdAt": started.isoformat(),
            "candidateCount": output["count"],
            "input": str(input_path),
            "output": str(output_path),
            "paper_only": True,
        }
        SENTIMENT_META_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        active_tickers = [
            t for t, v in output["tickers"].items() if v.get("social_score", 0) > 0
        ]
        print(json.dumps({
            "social_sentiment_enriched": output["count"],
            "active_tickers": active_tickers,
            "sample": {k: output["tickers"][k] for k in active_tickers[:3]},
        }, indent=2))
        return

    # Discovery mode
    candidates = get_social_discovery_candidates()

    today = started.date().isoformat()
    output = {
        "date": today,
        "generated_at": started.isoformat(),
        "count": len(candidates),
        "candidates": candidates,
    }
    DISCOVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERY_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")

    metadata = {
        "stage": "SOCIAL_DISCOVERY",
        "createdAt": started.isoformat(),
        "candidateCount": len(candidates),
        "paper_only": True,
    }
    DISCOVERY_META_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(json.dumps({
        "social_discovery_candidates": len(candidates),
        "apewisdom_snapshot": str(APEWISDOM_SENTIMENT_PATH),
        "top3": candidates[:3],
    }, indent=2))


if __name__ == "__main__":
    main()
