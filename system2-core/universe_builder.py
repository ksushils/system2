#!/usr/bin/env python3
"""
UNIVERSE BUILDER - Surgical Strike System 2
===========================================
Builds the broad overnight candidate universe for Stage 1.

Target shape:
  1. S&P 500
  2. Nasdaq 100
  3. Dow 30
  4. Russell 1000, or a large-cap screener fallback
  5. Liquid mid-cap screener pad

The downstream funnel is unchanged. Stage 1 still performs the real cheap
filter. This builder only controls how many clean, exchange-listed common
stocks enter the top of the funnel.

FMP endpoint note:
  Legacy /api/v3 endpoints now return 403 for this key. Prefer /stable/
  endpoints, with fallback aliases because FMP has used both underscore and
  hyphen spellings across accounts.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

import fmp_bandwidth


ROOT = Path(__file__).resolve().parent
FMP = "https://financialmodelingprep.com/stable"
FMP_API_KEY = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY") or "PASTE_YOUR_FMP_KEY_HERE"

ODD_TICKERS_TO_CHECK = [
    "AXIA", "AMRZ", "SOLS", "STRC", "SUNB", "WSE",
    "MAIR", "FPS", "FDXF", "MICC", "PSKY", "SGI",
]


def load_config() -> dict[str, Any]:
    for path in [ROOT / "system2-config.json", ROOT / "config.json"]:
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


CONFIG = load_config()
UNIVERSE_CONFIG = CONFIG.get("universe", {})

TARGET_SIZE = int(UNIVERSE_CONFIG.get("target_size", 1500))
MIN_MARKET_CAP = int(UNIVERSE_CONFIG.get("min_market_cap", 1_000_000_000))
MIN_AVG_VOLUME = int(UNIVERSE_CONFIG.get("min_avg_volume", 500_000))
EXCLUDE_PRICE_BELOW = float(UNIVERSE_CONFIG.get("exclude_price_below", 5.0))
SCREENER_LIMIT = int(UNIVERSE_CONFIG.get("screener_limit", 3000))
LIQUID_MIDCAP_PAD_MODE = str(UNIVERSE_CONFIG.get("liquid_midcap_pad_mode", "always")).lower()
OPTIONS_EXPANSION_ENABLED = bool(UNIVERSE_CONFIG.get("options_expansion_enabled", True))
IMPLIED_OPTIONS_DISCOVERY_ENABLED = bool(UNIVERSE_CONFIG.get("implied_options_discovery_enabled", True))


def get(endpoint: str, params: dict[str, Any] | None = None) -> tuple[Any | None, int | None, str]:
    query = {"apikey": FMP_API_KEY}
    if params:
        query.update(params)
    url = f"{FMP}/{endpoint}"
    try:
        r = requests.get(url, params=query, timeout=45, headers={"User-Agent": "system2-universe-builder/2.0"})
        fmp_bandwidth.record(
            endpoint,
            len(r.content or b""),
            status=r.status_code,
            source="universe_builder",
        )
        if r.status_code == 200:
            return r.json(), r.status_code, endpoint
        print(f"  ! {r.status_code} on /stable/{endpoint}")
        return None, r.status_code, endpoint
    except Exception as exc:
        print(f"  ! error on /stable/{endpoint}: {exc}")
        return None, None, endpoint


def get_first(endpoints: list[str], params: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], str | None, list[dict[str, Any]]]:
    attempts = []
    for endpoint in endpoints:
        data, status, used = get(endpoint, params)
        attempts.append({"endpoint": endpoint, "status": status, "rows": len(data) if isinstance(data, list) else 0})
        if isinstance(data, list) and data:
            return data, used, attempts
    return [], None, attempts


def clean_ticker(sym: Any) -> str | None:
    if not sym:
        return None
    s = str(sym).strip().upper()
    if any(c in s for c in [".", "-", "/", " "]):
        return None
    if not (1 <= len(s) <= 5):
        return None
    if not s.isalpha():
        return None
    return s


def is_common_stock_name(name: Any) -> bool:
    text = str(name or "")
    if not text:
        return True
    bad = re.compile(
        r"(ETF|ETN|TRUST|FUND|PREFERRED|PREF|WARRANT|RIGHT|UNIT|"
        r"NOTES|DEPOSITARY|ADR HEDGED|INCOME|BOND|TREASURY)",
        re.I,
    )
    return not bad.search(text)


def row_symbol(row: dict[str, Any]) -> str | None:
    return clean_ticker(row.get("symbol") or row.get("ticker"))


def row_exchange_ok(row: dict[str, Any]) -> bool:
    raw = str(row.get("exchangeShortName") or row.get("exchange") or row.get("exchangeName") or "").upper()
    if not raw:
        return True
    return raw in {"NYSE", "NASDAQ"}


def row_quality_ok(row: dict[str, Any], strict_screener: bool = False) -> bool:
    symbol = row_symbol(row)
    if not symbol:
        return False
    if row.get("isEtf") or row.get("isFund"):
        return False
    if row.get("isActivelyTrading") is False:
        return False
    if not row_exchange_ok(row):
        return False
    if not is_common_stock_name(row.get("companyName") or row.get("name")):
        return False
    if strict_screener:
        price = num(row.get("price"))
        volume = num(row.get("volume") or row.get("averageVolume") or row.get("avgVolume"))
        market_cap = num(row.get("marketCap"))
        if price is not None and price < EXCLUDE_PRICE_BELOW:
            return False
        if volume is not None and volume < MIN_AVG_VOLUME:
            return False
        if market_cap is not None and market_cap < MIN_MARKET_CAP:
            return False
    return True


def num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def fetch_constituents(label: str, endpoints: list[str]) -> dict[str, Any]:
    rows, used, attempts = get_first(endpoints)
    cleaned = []
    rejected = 0
    for row in rows:
        if not isinstance(row, dict) or not row_quality_ok(row):
            rejected += 1
            continue
        symbol = row_symbol(row)
        if symbol:
            cleaned.append({"symbol": symbol, "companyName": row.get("companyName") or row.get("name"), "source": label})
    return {"label": label, "rows": rows, "used": used, "attempts": attempts, "cleaned": cleaned, "rejected": rejected}


def fetch_screener(label: str, large_cap: bool = False) -> dict[str, Any]:
    params = {
        "marketCapMoreThan": 2_000_000_000 if large_cap else MIN_MARKET_CAP,
        "volumeMoreThan": MIN_AVG_VOLUME,
        "priceMoreThan": EXCLUDE_PRICE_BELOW,
        "exchange": "NYSE,NASDAQ",
        "isActivelyTrading": "true",
        "limit": SCREENER_LIMIT,
    }
    rows, used, attempts = get_first(["stock-screener", "company-screener"], params)
    rows = sorted(
        [r for r in rows if isinstance(r, dict)],
        key=lambda r: num(r.get("marketCap")) or 0,
        reverse=True,
    )
    cleaned = []
    rejected = 0
    for row in rows:
        if not row_quality_ok(row, strict_screener=True):
            rejected += 1
            continue
        symbol = row_symbol(row)
        if symbol:
            cleaned.append({"symbol": symbol, "companyName": row.get("companyName") or row.get("name"), "source": label})
    return {"label": label, "rows": rows, "used": used, "attempts": attempts, "cleaned": cleaned, "rejected": rejected}


def add_layer(
    universe: list[str],
    seen: set[str],
    names: dict[str, str],
    sources: dict[str, str],
    layer: dict[str, Any],
    limit: int = TARGET_SIZE,
) -> int:
    added = 0
    for row in layer["cleaned"]:
        if len(universe) >= limit:
            break
        symbol = row["symbol"]
        if symbol in seen:
            continue
        seen.add(symbol)
        universe.append(symbol)
        names[symbol] = row.get("companyName") or names.get(symbol) or ""
        sources[symbol] = layer["label"]
        added += 1
    return added


def _clean_expansion_symbol(sym: Any) -> str | None:
    if not sym:
        return None
    s = str(sym).upper().strip()
    if not (1 <= len(s) <= 5 and s.isalpha()):
        return None
    return s


def _merge_discovery_expansion(
    seen: set[str],
    universe: list[str],
    names: dict[str, str],
    sources: dict[str, str],
    path: Path,
    source_label: str,
    score_key: str | None = None,
    min_score: float | None = None,
) -> list[str]:
    """Merge fresh discovery candidates into the universe.

    Candidates are skipped if already present, if the ticker is not clean,
    or if an optional score floor is not met.
    """
    added: list[str] = []
    if not path.exists():
        return added
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        today = datetime.now(timezone.utc).date().isoformat()
        if data.get("date") != today and data.get("run_date") != today:
            return added
        for candidate in data.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            symbol = _clean_expansion_symbol(candidate.get("ticker") or candidate.get("symbol"))
            if not symbol or symbol in seen:
                continue
            if score_key is not None and min_score is not None:
                score = 0.0
                try:
                    score = float(candidate.get(score_key) or 0)
                except (TypeError, ValueError):
                    pass
                if score < min_score:
                    continue
            seen.add(symbol)
            universe.append(symbol)
            names[symbol] = str(candidate.get("catalyst_summary", "") or names.get(symbol, ""))[:120]
            sources[symbol] = source_label
            added.append(symbol)
    except Exception as exc:
        print(f"  ! {source_label} expansion merge error: {exc}")
    return added


def build_universe() -> tuple[list[str], dict[str, Any]]:
    print("Fetching universe sources from FMP...")
    layers = []
    layers.append(fetch_constituents("sp500", ["sp500_constituent", "sp500-constituent"]))
    layers.append(fetch_constituents("nasdaq100", ["nasdaq_constituent", "nasdaq-constituent"]))
    layers.append(fetch_constituents("dow30", ["dowjones_constituent", "dowjones-constituent"]))

    russell = fetch_constituents("russell1000", ["russell1000_constituent", "russell1000-constituent"])
    if not russell["cleaned"]:
        fallback = fetch_screener("russell1000_screener_fallback", large_cap=True)
        fallback["russellEndpointFailed"] = True
        layers.append(fallback)
    else:
        layers.append(russell)

    if LIQUID_MIDCAP_PAD_MODE == "always":
        layers.append(fetch_screener("liquid_midcap_pad", large_cap=False))

    universe: list[str] = []
    seen: set[str] = set()
    names: dict[str, str] = {}
    sources: dict[str, str] = {}
    source_counts: dict[str, int] = {}

    for layer in layers:
        added = add_layer(universe, seen, names, sources, layer)
        source_counts[layer["label"]] = added

    if LIQUID_MIDCAP_PAD_MODE == "fallback_only" and len(universe) < TARGET_SIZE:
        pad = fetch_screener("liquid_midcap_pad", large_cap=False)
        layers.append(pad)
        source_counts[pad["label"]] = add_layer(universe, seen, names, sources, pad)

    sorted_universe = sorted(universe)[:TARGET_SIZE]

    # Merge options-flow / social expansion tickers (if fresh)
    expansion_path = ROOT / "data" / "options_universe_expansion.json"
    expansion_tickers = []
    if OPTIONS_EXPANSION_ENABLED and expansion_path.exists():
        try:
            exp_data = json.loads(expansion_path.read_text(encoding="utf-8"))
            if exp_data.get("date") == datetime.now(timezone.utc).date().isoformat():
                for add in exp_data.get("additions", []):
                    sym = str(add.get("ticker", "")).upper().strip()
                    if sym and sym not in seen and 1 <= len(sym) <= 5 and sym.isalpha():
                        seen.add(sym)
                        sorted_universe.append(sym)
                        names[sym] = add.get("added_reason", "")
                        sources[sym] = add.get("source", "options_expansion")
                        expansion_tickers.append(sym)
        except Exception as exc:
            print(f"  ! options expansion merge error: {exc}")

    # Merge discovery expansion sources (Danelfin + ImpliedOptions)
    danelfin_expansion_tickers = _merge_discovery_expansion(
        seen, sorted_universe, names, sources,
        ROOT / "data" / "danelfin_discovery.json",
        "danelfin",
        score_key="aiscore",
        min_score=8.0,
    )
    implied_options_expansion_tickers = []
    if IMPLIED_OPTIONS_DISCOVERY_ENABLED:
        implied_options_expansion_tickers = _merge_discovery_expansion(
            seen, sorted_universe, names, sources,
            ROOT / "data" / "implied_options_discovery.json",
            "implied_options_discovery",
        )

    if danelfin_expansion_tickers:
        print(f"Danelfin added {len(danelfin_expansion_tickers)} stocks to universe")
    if implied_options_expansion_tickers:
        print(f"ImpliedOptions added {len(implied_options_expansion_tickers)} stocks to universe")

    sorted_universe = sorted(sorted_universe)
    metadata = {
        "targetSize": TARGET_SIZE,
        "finalCount": len(sorted_universe),
        "optionsExpansionCount": len(expansion_tickers),
        "optionsExpansionTickers": expansion_tickers[:50],
        "danelfinExpansionCount": len(danelfin_expansion_tickers),
        "danelfinExpansionTickers": danelfin_expansion_tickers[:50],
        "impliedOptionsExpansionCount": len(implied_options_expansion_tickers),
        "impliedOptionsExpansionTickers": implied_options_expansion_tickers[:50],
        "sourceFetchCountsBeforeDedup": {
            layer["label"]: len(layer["rows"]) for layer in layers
        },
        "sourceCleanCountsBeforeDedup": {
            layer["label"]: len(layer["cleaned"]) for layer in layers
        },
        "sourceAddedCountsAfterDedup": source_counts,
        "indexAddedCount": sum(source_counts.get(k, 0) for k in ["sp500", "nasdaq100", "dow30", "russell1000", "russell1000_screener_fallback"]),
        "screenerPadAddedCount": source_counts.get("liquid_midcap_pad", 0),
        "sourceAttempts": {layer["label"]: layer["attempts"] for layer in layers},
        "usedEndpoints": {layer["label"]: layer["used"] for layer in layers},
        "qualityGates": {
            "cleanTicker": "pure alpha, 1-5 chars; no dots/dashes/slashes/spaces",
            "excluded": "preferreds, warrants, units, ETFs, funds, OTC/pink sheets",
            "exchange": "NYSE or NASDAQ",
            "padMarketCapMoreThan": MIN_MARKET_CAP,
            "padVolumeMoreThan": MIN_AVG_VOLUME,
            "padPriceMoreThan": EXCLUDE_PRICE_BELOW,
        },
        "companyNames": {sym: names.get(sym, "") for sym in sorted_universe},
        "sourceBySymbol": {sym: sources.get(sym, "") for sym in sorted_universe},
        "oddTickerCheck": {sym: names.get(sym, "NOT_IN_UNIVERSE") for sym in ODD_TICKERS_TO_CHECK},
        "first30": sorted_universe[:30],
        "last30": sorted_universe[-30:],
    }
    return sorted_universe, metadata


def main() -> None:
    if FMP_API_KEY == "PASTE_YOUR_FMP_KEY_HERE":
        print("Set FMP_API_KEY first.")
        sys.exit(1)

    universe, metadata = build_universe()
    (ROOT / "universe.json").write_text(json.dumps(universe, indent=2), encoding="utf-8")
    (ROOT / "universe.metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"FINAL UNIVERSE: {len(universe)} tickers")
    print("Written to: universe.json")
    print("=" * 60)
    print(json.dumps({
        "targetSize": metadata["targetSize"],
        "finalCount": metadata["finalCount"],
        "sourceFetchCountsBeforeDedup": metadata["sourceFetchCountsBeforeDedup"],
        "sourceAddedCountsAfterDedup": metadata["sourceAddedCountsAfterDedup"],
        "indexAddedCount": metadata["indexAddedCount"],
        "screenerPadAddedCount": metadata["screenerPadAddedCount"],
        "optionsExpansionCount": metadata["optionsExpansionCount"],
        "danelfinExpansionCount": metadata["danelfinExpansionCount"],
        "danelfinExpansionTickers": metadata["danelfinExpansionTickers"],
        "impliedOptionsExpansionCount": metadata["impliedOptionsExpansionCount"],
        "impliedOptionsExpansionTickers": metadata["impliedOptionsExpansionTickers"],
        "usedEndpoints": metadata["usedEndpoints"],
        "oddTickerCheck": metadata["oddTickerCheck"],
        "first30": metadata["first30"],
        "last30": metadata["last30"],
    }, indent=2))


if __name__ == "__main__":
    main()
