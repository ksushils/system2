#!/usr/bin/env python3
"""Immutable, non-trading telemetry for prospective System2 research."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research_telemetry_common import (
    NY, RESEARCH_ROOT, is_market_session, next_market_session, read_json,
    run_directory, utc_now, write_immutable,
)

ROOT = Path(__file__).resolve().parent
SECTOR_ETFS = {
    "Technology": "XLK", "Communication Services": "XLC", "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP", "Financial Services": "XLF", "Healthcare": "XLV",
    "Industrials": "XLI", "Energy": "XLE", "Utilities": "XLU", "Real Estate": "XLRE",
    "Basic Materials": "XLB",
}


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


def symbols(rows: Any) -> list[str]:
    if not isinstance(rows, list):
        return []
    found = set()
    for row in rows:
        value = (row.get("ticker") or row.get("symbol")) if isinstance(row, dict) else row
        if value:
            found.add(str(value).upper())
    return sorted(found)


def latest_run_dir(session: str) -> Path | None:
    base = RESEARCH_ROOT / session
    candidates = [p for p in base.iterdir() if p.is_dir()] if base.exists() else []
    return sorted(candidates)[-1] if candidates else None


def latest_artifact(session: str, name: str) -> Path | None:
    base = RESEARCH_ROOT / session
    matches = sorted(base.glob(f"*/{name}")) if base.exists() else []
    return matches[-1] if matches else None


def finalize_nightly() -> dict[str, Any]:
    timing = next_market_session()
    directory = latest_run_dir(timing["trading_session"]) or run_directory(timing["trading_session"])
    universe = read_json(ROOT / "universe.json", [])
    candidate_pool = read_json(ROOT / "candidate_pool.json", [])
    stage1 = read_json(ROOT / "stage1_survivors.json", [])
    stage2 = read_json(ROOT / "stage2_surgical_strike_top40.json", [])
    finalists = read_json(ROOT / "stage7_clustered_survivors.json", [])
    provenance_path = latest_artifact(timing["trading_session"], "universe_provenance.json")
    provenance = read_json(provenance_path or Path("/missing"), {})
    base_vectors = provenance.get("source_lineage", {}) if isinstance(provenance, dict) else {}
    overlay: dict[str, set[str]] = {}
    for row in candidate_pool if isinstance(candidate_pool, list) else []:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or row.get("symbol") or "").upper()
        values = set(str(x) for x in (row.get("catalyst_sources") or []))
        if row.get("source") and row.get("source") != "scanner":
            values.add(str(row["source"]))
        overlay[ticker] = values
    stage1_set, stage2_set, finalist_set = set(symbols(stage1)), set(symbols(stage2)), set(symbols(finalists))
    lineage = {}
    for ticker in set(symbols(universe)) | set(symbols(candidate_pool)):
        base = base_vectors.get(ticker, {})
        source_set = set(base.get("sources_present") or []) | overlay.get(ticker, set())
        lineage[ticker] = {
            "sources_present": sorted(source_set),
            "primary_assigned_source": base.get("primary_assigned_source"),
            "base_source": base.get("base_source"),
            "catalyst_sources": sorted(overlay.get(ticker, set())),
            "enrichment_sources": ["stage2"] if ticker in stage2_set else [],
            "stage1_member": ticker in stage1_set,
            "stage2_member": ticker in stage2_set,
            "finalist_member": ticker in finalist_set,
        }
    payload = {
        "schema_version": 1,
        "research_only": True,
        "non_trading": True,
        "created_at": utc_now().isoformat(),
        "pipeline_completed_at": datetime.fromtimestamp((ROOT / "stage7_clustered_survivors.json").stat().st_mtime, timezone.utc).isoformat(),
        **timing,
        "production_counts": {"universe": len(universe), "candidate_pool": len(candidate_pool), "stage1": len(stage1), "stage2": len(stage2), "finalists": len(finalists)},
        "source_lineage": lineage,
        "artifact_references": {name: str(latest_artifact(timing["trading_session"], name)) if latest_artifact(timing["trading_session"], name) else None for name in ("universe_provenance.json", "catalyst_pre_cap.json")},
    }
    path = directory / "funnel_membership.json"
    if path.exists():
        raise FileExistsError(f"immutable artifact already exists: {path}")
    write_immutable(path, payload)
    return {"ok": True, "path": str(path), "counts": payload["production_counts"]}


def datum(value: Any, observed_at: str, provider: str, event_time: str | None = None, error: str | None = None, stale: bool = False) -> dict[str, Any]:
    if error:
        quality = "SOURCE_ERROR"
    elif value is None:
        quality = "MISSING"
    elif stale:
        quality = "STALE"
    else:
        quality = "FRESH"
    return {"value": value, "quality": quality, "observed_at": observed_at, "provider": provider, "source_event_time": event_time, "missing_reason": error if value is None else None}


def get_json(endpoint: str, key: str) -> Any:
    separator = "&" if "?" in endpoint else "?"
    url = f"https://financialmodelingprep.com/stable/{endpoint}{separator}apikey={urllib.parse.quote(key)}"
    request = urllib.request.Request(url, headers={"User-Agent": "system2-research-telemetry/1.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8", "ignore"))


def quote_return(quotes: dict[str, dict], ticker: str) -> float | None:
    quote = quotes.get(ticker, {})
    price = quote.get("preMarketPrice") or quote.get("price")
    prior = quote.get("previousClose")
    return ((float(price) / float(prior)) - 1) * 100 if price and prior else None


def shadow_premarket(fixture: bool = False, output_root: Path | None = None, at: datetime | None = None) -> dict[str, Any]:
    now = at or utc_now(); ny_now = now.astimezone(NY); session = ny_now.date()
    if not is_market_session(session) and not fixture:
        return {"ok": True, "skipped": True, "reason": "NYSE_CLOSED", "session": session.isoformat()}
    session_text = session.isoformat()
    source_dir = latest_run_dir(session_text)
    finalists = read_json(ROOT / "stage7_clustered_survivors.json", [])
    stage2 = read_json(ROOT / "stage2_surgical_strike_top40.json", [])
    ranked = sorted([x for x in stage2 if isinstance(x, dict)], key=lambda x: float(x.get("setup_score") or 0), reverse=True)[:50]
    by_ticker = {str(x.get("ticker") or x.get("symbol") or "").upper(): x for x in ranked}
    for row in finalists if isinstance(finalists, list) else []:
        if isinstance(row, dict):
            by_ticker[str(row.get("ticker") or row.get("symbol") or "").upper()] = row
    target_symbols = sorted(t for t in by_ticker if t)
    market_symbols = sorted(set(["SPY", "QQQ", "^VIX"] + list(SECTOR_ETFS.values())))
    observed = now.isoformat(); quotes: dict[str, dict] = {}; source_error = None
    if fixture:
        for i, ticker in enumerate(target_symbols + market_symbols):
            quotes[ticker] = {"symbol": ticker, "price": 100 + i, "previousClose": 99 + i, "preMarketPrice": 100 + i, "preMarketVolume": None}
    else:
        load_dotenv(); key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
        if not key:
            source_error = "FMP_KEY_MISSING"
        else:
            try:
                raw = get_json("batch-quote?symbols=" + urllib.parse.quote(",".join(target_symbols + market_symbols)), key)
                quotes = {str(x.get("symbol") or "").upper(): x for x in raw if isinstance(x, dict)} if isinstance(raw, list) else {}
            except Exception as exc:
                source_error = f"FMP_BATCH_QUOTE_ERROR:{type(exc).__name__}"
    pipeline_completed = None
    membership = read_json((source_dir / "funnel_membership.json") if source_dir else Path("/missing"), {})
    if isinstance(membership, dict):
        pipeline_completed = membership.get("pipeline_completed_at")
    rows = []
    finalist_set = set(symbols(finalists))
    for ticker in target_symbols:
        q = quotes.get(ticker, {}); price = q.get("preMarketPrice") or q.get("price"); prior = q.get("previousClose")
        gap = ((float(price) / float(prior)) - 1) * 100 if price and prior else None
        sector = by_ticker[ticker].get("sector"); sector_etf = SECTOR_ETFS.get(sector); spy = quotes.get("SPY", {}); sec = quotes.get(sector_etf, {}) if sector_etf else {}
        spy_gap = quote_return(quotes, "SPY")
        qqq_gap = quote_return(quotes, "QQQ")
        sec_gap = ((float(sec.get("preMarketPrice") or sec.get("price")) / float(sec.get("previousClose"))) - 1) * 100 if (sec.get("preMarketPrice") or sec.get("price")) and sec.get("previousClose") else None
        rows.append({
            "ticker": ticker, "research_set": "FINALIST" if ticker in finalist_set else "STAGE2_HIGH_RANKED", "research_only": True,
            "premarket_bid": datum(q.get("bid"), observed, "FMP", error=source_error),
            "premarket_ask": datum(q.get("ask"), observed, "FMP", error=source_error),
            "premarket_last": datum(price, observed, "FMP", error=source_error),
            "premarket_gap_pct": datum(round(gap, 4) if gap is not None else None, observed, "FMP", error=source_error),
            "premarket_high": datum(q.get("preMarketHigh"), observed, "FMP", error=source_error),
            "premarket_low": datum(q.get("preMarketLow"), observed, "FMP", error=source_error),
            "premarket_volume": datum(q.get("preMarketVolume"), observed, "FMP", error=source_error),
            "spread_bps": datum(((float(q["ask"])-float(q["bid"]))/((float(q["ask"])+float(q["bid"]))/2)*10000) if q.get("ask") and q.get("bid") else None, observed, "FMP", error=source_error),
            "new_company_news": datum(None, observed, "FMP", error="NOT_FETCHED_BATCH_COST_GUARD"),
            "new_guidance_event": datum(None, observed, "FMP", error="NOT_FETCHED_BATCH_COST_GUARD"),
            "new_analyst_action": datum(None, observed, "FMP", error="NOT_FETCHED_BATCH_COST_GUARD"),
            "new_earnings_information": datum(None, observed, "FMP", error="NOT_FETCHED_BATCH_COST_GUARD"),
            "spy_premarket_return_pct": datum(round(spy_gap, 4) if spy_gap is not None else None, observed, "FMP", error=source_error),
            "qqq_premarket_return_pct": datum(round(qqq_gap, 4) if qqq_gap is not None else None, observed, "FMP", error=source_error),
            "vix_current": datum((quotes.get("^VIX") or {}).get("price"), observed, "FMP", error=source_error),
            "sector_etf": sector_etf,
            "sector_premarket_return_pct": datum(round(sec_gap, 4) if sec_gap is not None else None, observed, "FMP", error=source_error),
            "relative_to_spy_pct": datum(round(gap-spy_gap, 4) if gap is not None and spy_gap is not None else None, observed, "derived:FMP", error=source_error),
            "relative_to_sector_pct": datum(round(gap-sec_gap, 4) if gap is not None and sec_gap is not None else None, observed, "derived:FMP", error=source_error),
        })
    timing = next_market_session(now)
    payload = {"schema_version": 1, "research_only": True, "non_trading": True, "session": session_text, "observed_at": observed, "target_time": "09:15 America/New_York", "pipeline_completed_at": pipeline_completed, **timing, "source_error": source_error, "rows": rows}
    if output_root:
        directory = output_root / session_text / "fixture"
    else:
        directory = source_dir or run_directory(session_text)
    path = directory / f"premarket_0915_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    write_immutable(path, payload)
    return {"ok": True, "path": str(path), "rows": len(rows), "source_error": source_error}


def self_test() -> dict[str, Any]:
    import tempfile
    from datetime import datetime
    with tempfile.TemporaryDirectory(prefix="system2-research-fixture-") as temp:
        winter = next_market_session(datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc))
        summer = next_market_session(datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc))
        fixture = shadow_premarket(True, Path(temp), datetime(2026, 9, 2, 13, 15, tzinfo=timezone.utc))
        return {"ok": True, "winter_open": winter["next_session_open"], "summer_open": summer["next_session_open"], "fixture": fixture, "broker_modules_imported": False}


def apply_retention(days: int = 1095) -> dict[str, Any]:
    """Remove only dated research sessions older than the measured three-year policy."""
    cutoff = utc_now().date().toordinal() - days
    removed = []
    if RESEARCH_ROOT.exists():
        for path in RESEARCH_ROOT.iterdir():
            if not path.is_dir():
                continue
            try:
                session = datetime.strptime(path.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if session.toordinal() < cutoff:
                shutil.rmtree(path)
                removed.append(str(path))
    return {"ok": True, "retention_days": days, "removed": removed}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("finalize-nightly", "shadow-premarket", "self-test", "retention"))
    args = parser.parse_args()
    result = finalize_nightly() if args.command == "finalize-nightly" else shadow_premarket() if args.command == "shadow-premarket" else apply_retention() if args.command == "retention" else self_test()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
