#!/usr/bin/env python3
"""
News catalyst scraper for System 2.

Uses two free sources:
  - NewsAPI.org for broad recent headline coverage
  - Alpha Vantage NEWS_SENTIMENT for finalist sentiment detail

Outputs /root/system2-core/data/news_catalyst.json. Paper-mode signal only.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_PATH = DATA_DIR / "news_catalyst.json"


def load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    with env_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


load_env()

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
ALPHAVANTAGE_KEY = os.getenv("ALPHAVANTAGE_KEY", "")
FMP_KEY = os.getenv("FMP_API_KEY", "")


POSITIVE_KEYWORDS = [
    "earnings beat", "revenue beat", "raised guidance", "upgrade",
    "price target raised", "buyback", "share repurchase", "contract win",
    "partnership", "fda approved", "acquisition", "record revenue",
    "dividend increase", "beat estimates", "outperform", "positive results",
    "strong demand", "new customer",
]

NEGATIVE_KEYWORDS = [
    "downgrade", "earnings miss", "revenue miss", "lowered guidance",
    "fda rejected", "recall", "investigation", "sec probe", "lawsuit",
    "bankruptcy", "default", "guidance cut", "miss estimates",
    "underperform", "sell rating", "fraud", "restatement", "layoffs",
    "plant closure",
]

DANGER_KEYWORDS = [
    "bankruptcy", "fraud", "sec investigation", "trading halt", "delisted",
    "default", "criminal charges", "going concern",
]


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def rows_from_artifact(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("ideas", "finalists", "candidates", "data", "tickers"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
    return []


def ticker_from_row(row: Any) -> str:
    if isinstance(row, str):
        return row.strip().upper()
    if isinstance(row, dict):
        return str(row.get("ticker") or row.get("symbol") or "").strip().upper()
    return ""


def company_from_row(row: Any) -> str | None:
    if isinstance(row, dict):
        return row.get("companyName") or row.get("company_name") or row.get("name")
    return None


def load_tickers() -> tuple[list[dict[str, str | None]], set[str]]:
    """
    Return up to 200 tickers, with finalists first and universe filling the rest.
    """
    ordered: list[dict[str, str | None]] = []
    seen: set[str] = set()
    finalist_tickers: set[str] = set()

    for rel in (
        "stage2_confluence_ranked_top40.json",
        "stage2_surgical_strike_top40.json",
        "stage6_council_enriched.json",
        "stage7_clustered_survivors.json",
    ):
        for row in rows_from_artifact(load_json(ROOT / rel, [])):
            ticker = ticker_from_row(row)
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            finalist_tickers.add(ticker)
            ordered.append({"ticker": ticker, "company_name": company_from_row(row)})

    universe = rows_from_artifact(load_json(ROOT / "universe.json", []))
    universe_rows = []
    for row in universe:
        ticker = ticker_from_row(row)
        if not ticker or ticker in seen:
            continue
        volume = 0.0
        if isinstance(row, dict):
            for key in ("volume", "averageVolume", "avg_volume"):
                try:
                    volume = max(volume, float(row.get(key) or 0))
                except Exception:
                    pass
        universe_rows.append((volume, row))
    universe_rows.sort(key=lambda item: item[0], reverse=True)

    for _, row in universe_rows:
        ticker = ticker_from_row(row)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        ordered.append({"ticker": ticker, "company_name": company_from_row(row)})
        if len(ordered) >= 200:
            break

    return ordered[:200], finalist_tickers


def fetch_newsapi(ticker: str, company_name: str | None = None) -> list[dict[str, str]]:
    """NewsAPI: search for ticker/company news over the last 3 days."""
    if not NEWSAPI_KEY:
        return []

    query = ticker
    if company_name:
        query = f'"{ticker}" OR "{company_name}"'
    from_date = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")

    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "from": from_date,
                "sortBy": "publishedAt",
                "language": "en",
                "pageSize": 5,
                "apiKey": NEWSAPI_KEY,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"NewsAPI {ticker}: HTTP {resp.status_code}")
            return []
        articles = resp.json().get("articles", [])
        return [
            {
                "title": a.get("title", ""),
                "description": a.get("description", ""),
                "source": (a.get("source") or {}).get("name", ""),
                "published": a.get("publishedAt", ""),
                "url": a.get("url", ""),
            }
            for a in articles
        ]
    except Exception as exc:
        print(f"NewsAPI {ticker}: {exc}")
        return []


def fetch_alphavantage_news(ticker: str) -> list[dict[str, Any]]:
    """Alpha Vantage: financial news with built-in sentiment."""
    if not ALPHAVANTAGE_KEY:
        return []

    try:
        resp = requests.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "NEWS_SENTIMENT",
                "tickers": ticker,
                "limit": 5,
                "apikey": ALPHAVANTAGE_KEY,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"AlphaVantage {ticker}: HTTP {resp.status_code}")
            return []
        data = resp.json()
        feed = data.get("feed", [])
        return [
            {
                "title": a.get("title", ""),
                "summary": a.get("summary", ""),
                "source": a.get("source", ""),
                "published": a.get("time_published", ""),
                "overall_sentiment_score": float(a.get("overall_sentiment_score", 0)),
                "overall_sentiment_label": a.get("overall_sentiment_label", "Neutral"),
                "ticker_sentiment": next(
                    (t for t in a.get("ticker_sentiment", []) if t.get("ticker") == ticker),
                    {},
                ),
            }
            for a in feed
        ]
    except Exception as exc:
        print(f"AlphaVantage {ticker}: {exc}")
        return []


def classify_news(articles: list[dict[str, Any]], av_articles: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return catalyst classification from combined news sources."""
    av_articles = av_articles or []
    all_text = " ".join(
        (
            str(a.get("title", ""))
            + " "
            + str(a.get("description", ""))
            + " "
            + str(a.get("summary", ""))
        ).lower()
        for a in (articles + av_articles)
    )

    has_positive = any(keyword in all_text for keyword in POSITIVE_KEYWORDS)
    has_negative = any(keyword in all_text for keyword in NEGATIVE_KEYWORDS)
    has_danger = any(keyword in all_text for keyword in DANGER_KEYWORDS)

    av_scores = [
        float(a.get("overall_sentiment_score", 0))
        for a in av_articles
        if a.get("overall_sentiment_score") is not None
    ]
    avg_sentiment = sum(av_scores) / len(av_scores) if av_scores else 0.0

    if has_danger:
        verdict = "DANGER"
    elif has_negative and not has_positive:
        verdict = "CAUTION"
    elif has_positive and avg_sentiment > 0.15:
        verdict = "POSITIVE_CATALYST"
    elif has_positive:
        verdict = "MILD_POSITIVE"
    elif avg_sentiment < -0.15:
        verdict = "MILD_NEGATIVE"
    else:
        verdict = "NEUTRAL"

    best_headline = ""
    if articles:
        best_headline = articles[0].get("title", "")
    elif av_articles:
        best_headline = av_articles[0].get("title", "")

    return {
        "news_verdict": verdict,
        "news_sentiment_score": round(avg_sentiment, 3),
        "has_positive_catalyst": has_positive,
        "has_negative_catalyst": has_negative,
        "has_danger": has_danger,
        "best_headline": best_headline[:200],
        "article_count": len(articles) + len(av_articles),
        "sources_used": (["newsapi"] if articles else []) + (["alphavantage"] if av_articles else []),
    }


def run() -> dict[str, Any]:
    tickers, finalist_tickers = load_tickers()
    results: dict[str, dict[str, Any]] = {}
    newsapi_calls = 0
    av_calls = 0

    for item in tickers:
        ticker = str(item["ticker"])
        news_articles: list[dict[str, Any]] = []
        av_articles: list[dict[str, Any]] = []

        if newsapi_calls < 95 and NEWSAPI_KEY:
            news_articles = fetch_newsapi(ticker, item.get("company_name"))
            newsapi_calls += 1
            time.sleep(0.2)

        if av_calls < 20 and ALPHAVANTAGE_KEY and ticker in finalist_tickers:
            av_articles = fetch_alphavantage_news(ticker)
            av_calls += 1
            time.sleep(0.5)

        if news_articles or av_articles:
            results[ticker] = classify_news(news_articles, av_articles)
        else:
            results[ticker] = {
                "news_verdict": "NO_DATA",
                "news_sentiment_score": 0,
                "article_count": 0,
                "sources_used": [],
            }

    output = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "newsapi_calls_used": newsapi_calls,
        "av_calls_used": av_calls,
        "tickers_covered": len(results),
        "positive_catalysts": sum(
            1 for value in results.values() if value.get("news_verdict") in {"POSITIVE_CATALYST", "MILD_POSITIVE"}
        ),
        "dangers_found": sum(1 for value in results.values() if value.get("news_verdict") == "DANGER"),
        "results": results,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")

    print(f"News scraper: {len(results)} tickers")
    print(f"NewsAPI calls: {newsapi_calls}")
    print(f"AV calls: {av_calls}")
    print(f"Positive catalysts: {output['positive_catalysts']}")
    print(f"Dangers found: {output['dangers_found']}")
    return output


if __name__ == "__main__":
    run()
