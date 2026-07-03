#!/usr/bin/env python3
"""Shared FMP API client used across System 2 pipeline stages.

Provides a simple get() function that matches the interface expected
by fmp_news_analyst_signals.py and other enrichment stages.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FMP_BASE = "https://financialmodelingprep.com"


def _load_fmp_key() -> str:
    env_key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if env_key:
        return env_key.strip()

    downloads = Path.home() / "Downloads"
    for path in [
        downloads / "FMP-Scanner-v13.5-alpaca.json",
        downloads / "FMP_Scanner_FIXED.json",
        downloads / "universe_builder.py",
    ]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"FMP_API_KEY:\s*'([^']+)'", text)
        if match:
            return match.group(1)
        match = re.search(r"FMP_API_KEY\s*=\s*['\"]([^'\"]+)['\"]", text)
        if match:
            return match.group(1)
    raise RuntimeError("FMP API key not found. Set FMP_API_KEY.")


def get(endpoint: str, params: dict[str, str] | None = None, api_key: str | None = None):
    """
    GET an FMP API endpoint.

    Args:
        endpoint: FMP endpoint path, e.g. 'stable/stock-news'
        params: Optional query parameters dict
        api_key: Optional API key (loads from env/files if omitted)

    Returns:
        Parsed JSON (usually list or dict) or None on failure.
    """
    key = (api_key or "").strip() or _load_fmp_key()

    query = urllib.parse.urlencode(params or {})
    sep = "&" if "?" in endpoint else "?"
    url = f"{FMP_BASE}/{endpoint}{sep}apikey={urllib.parse.quote(key)}"
    if query:
        url += f"&{query}"

    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "system2-fmp-api/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                time.sleep(2.0 * (attempt + 1))
                continue
            return {"error": f"HTTP {exc.code}"}
        except Exception as exc:
            if attempt < 3:
                time.sleep(1.0 * (attempt + 1))
                continue
            return {"error": str(exc)}
    return {"error": "max retries exceeded"}
