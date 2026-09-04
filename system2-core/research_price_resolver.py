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


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return default


def canonical_eod_records(symbols: set[str]) -> dict[str, dict[str, dict[str, Any]]]:
    """Load canonical EOD rows. Batch marks are deliberately not merged here."""
    chosen: dict[str, str] = {}
    for path in glob.glob(str(ROOT / "data/fmp_cache/*/*historical-price-eod*json")):
        symbol = _symbol_from_cache(path)
        if symbol in symbols and (symbol not in chosen or path > chosen[symbol]):
            chosen[symbol] = path
    output: dict[str, dict[str, dict[str, Any]]] = {}
    for symbol, raw_path in chosen.items():
        payload = _read(Path(raw_path), {})
        rows = payload.get("data", []) if isinstance(payload, dict) else payload
        by_date: dict[str, dict[str, Any]] = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("date"):
                by_date[str(row["date"])[:10]] = {
                    **row,
                    "_source_file": raw_path,
                    "_source_type": "CANONICAL_FMP_EOD",
                    "_provider": "FMP historical-price-eod/full",
                    "_adjustment_basis": str(row.get("adjustment_basis") or "UNKNOWN").upper(),
                }
        output[symbol] = by_date
    return output


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
        self.eod = canonical_eod_records(self.symbols)
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
        return self._missing(symbol, market_date, field_type, "NO_CANONICAL_OR_PROVEN_COMPLETED_SESSION_PRICE")

    def corporate_action_state(self, symbol: str, start_date: str, end_date: str) -> dict[str, Any]:
        """Conservative: explicit cached actions are flagged; unknown adjustment basis is retained."""
        actions = []
        patterns = ("*split*", "*symbol-change*", "*merger*")
        for pattern in patterns:
            for path in glob.glob(str(ROOT / f"data/fmp_cache/*/{pattern}{symbol}*json")):
                payload = _read(Path(path), [])
                rows = payload.get("data", []) if isinstance(payload, dict) else payload
                for row in rows if isinstance(rows, list) else []:
                    day = str(row.get("date") or row.get("effectiveDate") or "")[:10]
                    if start_date <= day <= end_date:
                        actions.append({"date": day, "source_file": path, "type": row.get("type") or Path(path).name})
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
