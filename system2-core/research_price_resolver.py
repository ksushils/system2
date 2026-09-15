#!/usr/bin/env python3
"""Canonical, provenance-rich price resolution for System2 research only."""

from __future__ import annotations

import glob
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RESEARCH_ROOT = ROOT / "data" / "research_telemetry"
NY = ZoneInfo("America/New_York")
_CACHE_FILE_INDEX: dict[str, list[str]] | None = None
_CORPORATE_ACTION_INDEX: dict[str, list[dict[str, Any]]] | None = None


def number(value: Any) -> float | None:
    try:
        value = float(value)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


@dataclass(frozen=True)
class ResolvedPrice:
    symbol: str
    market_date: str
    price: float | None
    field_type: str
    source_type: str
    source_file: str | None
    provider: str
    provider_timestamp: str | None
    session_type: str
    adjustment_basis: str
    quality_state: str
    fallback_level: int | None
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _symbol_from_cache(path: str) -> str:
    match = re.search(r"symbol[=_]([A-Za-z0-9.^-]+)", Path(path).name)
    return match.group(1).upper() if match else ""


def _cache_file_index() -> dict[str, list[str]]:
    global _CACHE_FILE_INDEX
    if _CACHE_FILE_INDEX is None:
        index: dict[str, list[str]] = {}
        for path in glob.glob(str(ROOT / "data/fmp_cache/*/*historical-price-eod*json")):
            symbol = _symbol_from_cache(path)
            if symbol:
                index.setdefault(symbol, []).append(path)
        _CACHE_FILE_INDEX = {symbol: sorted(paths) for symbol, paths in index.items()}
    return _CACHE_FILE_INDEX


def _corporate_action_index() -> dict[str, list[dict[str, Any]]]:
    global _CORPORATE_ACTION_INDEX
    if _CORPORATE_ACTION_INDEX is None:
        result: dict[str, list[dict[str, Any]]] = {}
        for pattern in ("*split*json", "*symbol-change*json", "*merger*json"):
            for raw_path in glob.glob(str(ROOT / "data/fmp_cache/*" / pattern)):
                payload = _read(Path(raw_path), [])
                rows = payload.get("data", []) if isinstance(payload, dict) else payload
                for row in rows if isinstance(rows, list) else []:
                    symbol = str(row.get("symbol") or row.get("ticker") or "").upper()
                    day = str(row.get("date") or row.get("effectiveDate") or "")[:10]
                    if symbol and day:
                        result.setdefault(symbol, []).append({"date": day, "source_file": raw_path, "type": row.get("type") or Path(raw_path).name})
        _CORPORATE_ACTION_INDEX = result
    return _CORPORATE_ACTION_INDEX


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return default


def _symbol_aliases(symbol: str) -> tuple[str, ...]:
    symbol = symbol.upper()
    aliases = [symbol]
    for candidate in (symbol.replace(".", "-"), symbol.replace("/", "-"), symbol.replace("-", ".")):
        if candidate not in aliases:
            aliases.append(candidate)
    return tuple(aliases)


def canonical_eod_records(symbols: set[str]) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Merge retained canonical EOD caches newest-per-date, never newest-file-only.

    A later empty or shortened provider response must not hide valid earlier
    point-in-time cache rows.
    """
    requested_by_alias = {alias: requested for requested in symbols for alias in _symbol_aliases(requested)}
    diagnostics: dict[str, dict[str, Any]] = {symbol: {"cache_files": 0, "nonempty_files": 0, "dates": set(), "aliases": _symbol_aliases(symbol)} for symbol in symbols}
    output: dict[str, dict[str, dict[str, Any]]] = {symbol: {} for symbol in symbols}
    cache_index = _cache_file_index()
    relevant = [(requested_by_alias[alias], alias, path) for alias in requested_by_alias for path in cache_index.get(alias, [])]
    for symbol, cache_symbol, path in relevant:
        diagnostics[symbol]["cache_files"] += 1
        payload = _read(Path(path), {})
        rows = payload.get("data", []) if isinstance(payload, dict) else payload
        if isinstance(rows, list) and rows:
            diagnostics[symbol]["nonempty_files"] += 1
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("date"):
                market_date = str(row["date"])[:10]
                diagnostics[symbol]["dates"].add(market_date)
                existing = output[symbol].get(market_date)
                if existing and str(existing.get("_source_file")) > path:
                    continue
                output[symbol][market_date] = {
                    **row,
                    "_source_file": path,
                    "_source_type": "CANONICAL_FMP_EOD",
                    "_provider": "FMP historical-price-eod/full",
                    "_source_symbol": cache_symbol,
                    "_adjustment_basis": str(row.get("adjustment_basis") or "UNKNOWN").upper(),
                }
    for item in diagnostics.values():
        item["dates"] = sorted(item["dates"])
    return output, diagnostics


def retained_daily_marks(symbols: set[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    output: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for path in sorted((RESEARCH_ROOT / "daily_marks").glob("challenger_daily_mark_*.json")):
        payload = _read(path, {}) or {}
        for row in payload.get("rows", []):
            symbol = str(row.get("symbol") or row.get("ticker") or "").upper()
            day = str(row.get("date") or "")[:10]
            if symbol in symbols and day:
                output.setdefault(symbol, {}).setdefault(day, []).append({**row, "_source_file": str(path)})
    return output


class ResearchPriceResolver:
    """Authority order: canonical EOD, proven completed mark, proven batch quote."""

    def __init__(self, symbols: set[str]):
        self.symbols = {s.upper() for s in symbols}
        self.eod, self.cache_diagnostics = canonical_eod_records(self.symbols)
        self.marks = retained_daily_marks(self.symbols)

    def _missing(self, symbol: str, day: str, field: str, reason: str, quality: str = "MISSING_PRICE") -> dict[str, Any]:
        return ResolvedPrice(symbol, day, None, field, "NONE", None, "NONE", None, "UNKNOWN", "UNKNOWN", quality, None, reason).to_dict()

    def resolve(self, symbol: str, market_date: str, field_type: str) -> dict[str, Any]:
        symbol, market_date = symbol.upper(), market_date[:10]
        key = "open" if field_type == "NEXT_OPEN" else "close"
        row = self.eod.get(symbol, {}).get(market_date)
        if row and number(row.get(key)) is not None:
            return ResolvedPrice(symbol, market_date, number(row[key]), field_type, row["_source_type"], row["_source_file"], row["_provider"], str(row.get("timestamp") or row.get("provider_timestamp") or "") or None, "COMPLETED_REGULAR", row["_adjustment_basis"], "CANONICAL", 1, None).to_dict()
        # A retained mark is usable only when its own metadata proves completion.
        for mark in reversed(self.marks.get(symbol, {}).get(market_date, [])):
            quality = str(mark.get("quality_state") or mark.get("quality") or "").upper()
            session = str(mark.get("session_type") or "").upper()
            timestamp = parse_timestamp(mark.get("provider_timestamp"))
            proven = quality in {"COMPLETED_SESSION", "CANONICAL"} and session in {"COMPLETED_REGULAR", "NORMAL", "HALF_DAY"} and timestamp
            if proven and number(mark.get(key)) is not None:
                basis = str(mark.get("adjustment_basis") or "UNKNOWN").upper()
                return ResolvedPrice(symbol, market_date, number(mark[key]), field_type, "IMMUTABLE_COMPLETED_OHLC", mark.get("_source_file"), str(mark.get("provider") or "UNKNOWN"), timestamp.isoformat(), session, basis, "VALIDATED_FALLBACK", 2, None).to_dict()
        # The recorder is a retained, timestamped Alpaca market-data artifact,
        # not a broker request. Its first 09:30 ET bar can authoritatively
        # recover NEXT_OPEN only; partial intraday files can never supply close.
        if field_type == "NEXT_OPEN":
            intraday_path = ROOT / "data" / "intraday_bars" / market_date / f"{symbol}.json"
            payload = _read(intraday_path, {})
            bars = payload.get("bars", []) if isinstance(payload, dict) else []
            for bar in bars if isinstance(bars, list) else []:
                stamp = parse_timestamp(bar.get("timestamp"))
                if stamp and stamp.astimezone(NY).date().isoformat() == market_date and stamp.astimezone(NY).time() == time(9, 30) and number(bar.get("open")) is not None:
                    return ResolvedPrice(symbol, market_date, number(bar["open"]), field_type, "IMMUTABLE_ALPACA_5MIN_OPEN", str(intraday_path), "Alpaca market-data recorder", stamp.isoformat(), "COMPLETED_REGULAR", "UNADJUSTED", "VALIDATED_FALLBACK", 2, None).to_dict()
        diagnostic = self.cache_diagnostics.get(symbol, {})
        if not diagnostic.get("cache_files"):
            reason = "SYMBOL_NOT_FOUND"
        elif not diagnostic.get("nonempty_files"):
            reason = "REQUEST_FAILED_OR_EMPTY_CACHE"
        elif market_date not in set(diagnostic.get("dates") or []):
            reason = "FMP_NO_HISTORICAL_PRICE_FOR_TARGET_DATE"
        else:
            reason = "PRICE_FIELD_MISSING_OR_INVALID"
        return self._missing(symbol, market_date, field_type, reason)

    def corporate_action_state(self, symbol: str, start_date: str, end_date: str) -> dict[str, Any]:
        """Conservative: explicit cached actions are flagged; unknown adjustment basis is retained."""
        actions = [row for row in _corporate_action_index().get(symbol.upper(), []) if start_date <= row["date"] <= end_date]
        return {"state": "CORPORATE_ACTION_UNRESOLVED" if actions else "NO_RETAINED_ACTION_FOUND", "actions": actions}


def validate_premarket_quote(symbol: str, quote: dict[str, Any], intended_date: str) -> dict[str, Any]:
    """Validate only the explicit preMarketPrice field; generic price is never promoted."""
    observed = quote.get("preMarketPrice")
    provider_ts = parse_timestamp(quote.get("timestamp"))
    generic = number(quote.get("price"))
    base = {
        "symbol": symbol.upper(), "market_date": intended_date, "field_type": "PREMARKET_PRICE",
        "source_type": "FMP_BATCH_QUOTE", "source_file": None, "provider": "FMP batch-quote",
        "provider_timestamp": provider_ts.isoformat() if provider_ts else None, "adjustment_basis": "UNADJUSTED",
        "fallback_level": None, "generic_price_retained": generic,
    }
    if not quote:
        return {**base, "price": None, "field_used": None, "session_type": "UNKNOWN", "quality_state": "SOURCE_ERROR", "reason": "QUOTE_ABSENT"}
    if number(observed) is None:
        reason = "PREMARKET_FIELD_ABSENT" if observed in (None, "") else "PREMARKET_FIELD_NONNUMERIC_OR_ZERO"
        return {**base, "price": None, "field_used": None, "session_type": "UNKNOWN", "quality_state": "NO_PREMARKET_TRADE", "reason": reason}
    if provider_ts is None:
        return {**base, "price": None, "field_used": "preMarketPrice", "session_type": "UNKNOWN", "quality_state": "UNKNOWN", "reason": "PROVIDER_TIMESTAMP_MISSING"}
    eastern = provider_ts.astimezone(NY)
    if eastern.date().isoformat() != intended_date:
        return {**base, "price": None, "field_used": "preMarketPrice", "session_type": "PREVIOUS_SESSION", "quality_state": "PREVIOUS_SESSION_STALE", "reason": "TIMESTAMP_DATE_MISMATCH"}
    if not (time(4, 0) <= eastern.time() < time(9, 30)):
        return {**base, "price": None, "field_used": "preMarketPrice", "session_type": "OUTSIDE_PREMARKET", "quality_state": "UNKNOWN", "reason": "TIMESTAMP_OUTSIDE_0400_0930_ET"}
    previous = number(quote.get("previousClose"))
    if previous is not None and abs(number(observed) - previous) < 1e-12:
        return {**base, "price": None, "field_used": "preMarketPrice", "session_type": "PREMARKET", "quality_state": "PREVIOUS_SESSION_STALE", "reason": "VALUE_EQUALS_PREVIOUS_CLOSE"}
    return {**base, "price": number(observed), "field_used": "preMarketPrice", "session_type": "PREMARKET", "quality_state": "PREMARKET_VALID", "reason": None}
