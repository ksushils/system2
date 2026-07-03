#!/usr/bin/env python3
"""Set 2 scorer.

Scores Set 2 candidates independently from Set 1.
Outputs set2_scored.json with the same schema fields as stage2 top-40
so merge_sets.py can combine it with Set 1 seamlessly.

Paper mode only."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any



def load_dotenv() -> None:
    path = Path(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")

load_dotenv()

ROOT = Path(__file__).resolve().parent
CANDIDATES_PATH = ROOT / "data" / "set2_candidates.json"
DANELFIN_PATH = ROOT / "data" / "danelfin_scores.json"
OUTPUT_PATH = ROOT / "data" / "set2_scored.json"
META_PATH = ROOT / "data" / "set2_scored_metadata.json"
DAILY_LOOKBACK = 30


def _log(message: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {message}", flush=True)


def num(value, default: float = 0.0) -> float:
    try:
        v = float(value)
        if math.isfinite(v):
            return v
    except Exception:
        pass
    return default


def load_fmp_key() -> str:
    key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if key:
        return key
    # Fallback: scan Downloads for FMP key
    downloads = Path.home() / "Downloads"
    for path in [downloads / "FMP-Scanner-v13.5-alpaca.json", downloads / "FMP_Scanner_FIXED.json"]:
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r"FMP_API_KEY:\s*'([^']+)'", text) or re.search(r"FMP_API_KEY\s*=\s*['\"]([^'\"]+)['\"]", text)
                if m:
                    return m.group(1)
            except Exception:
                pass
    raise RuntimeError("FMP_API_KEY not found")


class FmpClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.base = "https://financialmodelingprep.com"
        self.errors: list[str] = []

    def get(self, endpoint: str, timeout: int = 30) -> Any:
        sep = "&" if "?" in endpoint else "?"
        url = f"{self.base}/{endpoint}{sep}apikey={urllib.parse.quote(self.api_key)}"
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url,
                    headers={"Accept": "application/json", "User-Agent": "system2-set2-scorer/1.0"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", "ignore"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                self.errors.append(f"{endpoint}: HTTP {exc.code}")
                return None
            except Exception as exc:
                time.sleep(1.0 * (attempt + 1))
                continue
        self.errors.append(f"{endpoint}: failed after retries")
        return None


def fetch_batch_quotes(client: FmpClient, symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch batch quote for a list of symbols."""
    if not symbols:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(symbols), 100):
        batch = symbols[i : i + 100]
        data = client.get("stable/batch-quote?symbols=" + ",".join(batch))
        if isinstance(data, list):
            for row in data:
                sym = str(row.get("symbol") or "").upper().strip()
                if sym:
                    out[sym] = row
        time.sleep(0.1)
    return out


def fetch_batch_profiles(client: FmpClient, symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch batch company profile for a list of symbols."""
    if not symbols:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(symbols), 100):
        batch = symbols[i : i + 100]
        data = client.get("stable/profile?symbol=" + ",".join(batch))
        if isinstance(data, list):
            for row in data:
                sym = str(row.get("symbol") or "").upper().strip()
                if sym:
                    out[sym] = row
        time.sleep(0.1)
    return out


def fetch_daily_history(client: FmpClient, symbol: str) -> list[dict]:
    endpoint = (
        f"stable/historical-price-eod/full"
        f"?symbol={urllib.parse.quote(symbol)}&timeseries={DAILY_LOOKBACK}"
    )
    data = client.get(endpoint)
    if isinstance(data, dict):
        hist = data.get("historical")
        if isinstance(hist, list):
            rows = hist
        else:
            rows = next((data.get(k) for k in ("data", "results") if isinstance(data.get(k), list)), [])
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    # FMP returns newest-first; reverse to oldest-first for MA calc
    rows = rows[:DAILY_LOOKBACK]
    try:
        rows = sorted(rows, key=lambda r: str(r.get("date") or "")[:10])
    except Exception:
        pass
    return rows


def compute_ma20_and_rvol(rows: list[dict]) -> tuple[float | None, float, float]:
    """Return (ma20, latest_volume, avg_volume_20d)."""
    closes = [num(r.get("close")) for r in rows if num(r.get("close")) > 0]
    volumes = [num(r.get("volume")) for r in rows if num(r.get("volume")) > 0]
    ma20 = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 20 else None
    latest_volume = volumes[-1] if volumes else 0
    avg_volume = sum(volumes[-20:]) / len(volumes[-20:]) if len(volumes) >= 20 else 0
    return ma20, latest_volume, avg_volume


def load_danelfin_scores() -> dict[str, dict[str, Any]]:
    if not DANELFIN_PATH.exists():
        return {}
    try:
        data = json.loads(DANELFIN_PATH.read_text(encoding="utf-8"))
        return data.get("scores", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def check_earnings(client: FmpClient, symbol: str) -> dict[str, Any]:
    """Fail-open earnings check. Returns days_to_earnings and risk flag."""
    today = datetime.now(timezone.utc).date()
    try:
        data = client.get(f"stable/earnings?symbol={urllib.parse.quote(symbol)}", timeout=10)
        rows = data if isinstance(data, list) else []
        dates: list[date] = []
        for r in rows:
            d = r.get("date") or r.get("earningsDate")
            if not d:
                continue
            try:
                dt = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
                if dt >= today:
                    dates.append(dt)
            except Exception:
                continue
        if dates:
            nearest = min(dates)
            days = (nearest - today).days
            return {"days_to_earnings": int(days), "earnings_date": nearest.isoformat()}
    except Exception:
        pass
    return {"days_to_earnings": None, "earnings_date": None}


def score_candidate(candidate: dict[str, Any], quote: dict[str, Any], history: list[dict],
                    danelfin_scores: dict[str, dict[str, Any]], earnings: dict[str, Any],
                    profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply Set 2 scoring and return a stage2-compatible record."""
    ticker = candidate["ticker"]
    price = num(quote.get("price")) or candidate.get("_raw_price", 0)
    avg_daily_volume = num(quote.get("avgVolume") or quote.get("avg_volume"))

    # Profile enrichment
    sector = (profile.get("sector") if profile else None) or quote.get("sector")
    industry = (profile.get("industry") if profile else None)
    market_cap = num(profile.get("marketCap") or profile.get("mktCap")) if profile else None
    if market_cap is None:
        market_cap = num(quote.get("marketCap"))
    if market_cap and market_cap >= 10_000_000_000:
        market_cap_bucket = "large"
    elif market_cap and market_cap >= 2_000_000_000:
        market_cap_bucket = "mid"
    elif market_cap and market_cap > 0:
        market_cap_bucket = "small"
    else:
        market_cap_bucket = "unknown"
    ma20, latest_volume, avg_volume_20d = compute_ma20_and_rvol(history)

    # Hard rejects
    hard_rejects: list[str] = []
    if price > 0 and (price < 10 or price > 2000):
        hard_rejects.append("price_out_of_range")
    if avg_daily_volume > 0 and avg_daily_volume < 200_000:
        hard_rejects.append("volume_too_low")
    days_to_earnings = earnings.get("days_to_earnings")
    if days_to_earnings is not None and days_to_earnings <= 3:
        hard_rejects.append("earnings_too_close")
    bearish = float(candidate.get("bearish_premium_total") or 0)
    bullish = float(candidate.get("bullish_premium_total") or 0)
    if bullish > 0 and bearish > bullish * 2:
        hard_rejects.append("bearish_premium_dominant")

    # Technical alignment
    price_above_20d = bool(ma20 is not None and price > ma20)
    rvol = latest_volume / avg_volume_20d if avg_volume_20d > 0 else 1.0
    rs_vs_spy = 0.0  # We don't fetch SPY here; leave as 0 unless populated externally

    # Danelfin enrichment
    danelfin = danelfin_scores.get(ticker) or {}
    danelfin_available = bool(danelfin) and danelfin.get("ai_score") is not None

    # Set 2 core score
    options_score = int(candidate.get("set2_options_score") or 0)
    core_score = 0
    if options_score >= 70:
        core_score = 40
    elif options_score >= 50:
        core_score = 28
    elif options_score >= 35:
        core_score = 16

    technical_bonus = 0
    if price_above_20d:
        technical_bonus += 15
    if rvol > 1.5:
        technical_bonus += 10
    # rs_vs_spy placeholder — would need SPY data to populate
    # if rs_vs_spy > 0: technical_bonus += 10

    flow_bonus = 0
    source = str(candidate.get("options_flow_source") or "").lower()
    if "impliedoptions" in source or "auth" in source:
        flow_bonus += 15
    if candidate.get("multi_source_flow"):
        flow_bonus += 10
    if source == "barchart_uoa":
        flow_bonus += 5

    core_total = core_score + technical_bonus + flow_bonus

    # Risk score
    risk = 0
    if days_to_earnings is not None:
        if 4 <= days_to_earnings <= 7:
            risk += 10
        elif 1 <= days_to_earnings <= 3:
            risk += 30  # will already be rejected, but include for completeness
    iv_rank = candidate.get("iv_rank")
    if isinstance(iv_rank, (int, float)) and iv_rank > 80:
        risk += 15
    if not price_above_20d:
        risk += 10

    trade_quality = max(0, min(100, core_total * 0.7 - risk * 0.3))

    # Build stage2-compatible record
    record: dict[str, Any] = {
        "symbol": ticker,
        "ticker": ticker,
        "set": 2,
        "set_source": candidate.get("set_source", "options_flow"),
        "source": "set2_options_flow",
        "sub_type": candidate.get("sub_type", "options_flow_led"),
        "status": "REJECTED" if hard_rejects else "OK",
        "stage2RejectReason": hard_rejects[0] if hard_rejects else None,
        "rejectReasons": hard_rejects,
        "price": round(price, 4) if price > 0 else 0,
        "averageVolume": round(avg_daily_volume, 2) if avg_daily_volume > 0 else 0,
        "volume": round(latest_volume, 2),
        "volumeRatio": round(rvol, 3),
        "atr14": 0.0,  # Not computed here; will be populated by ATR enrichment later or can remain 0
        "atrPct": 0.0,
        "rsVsSpy": round(rs_vs_spy, 3),
        "sector": sector or "Unknown",
        "industry": industry or "Unknown",
        "marketCap": market_cap,
        "market_cap_bucket": market_cap_bucket,
        "changePercentage": num(quote.get("changesPercentage")),
        "previousClose": num(quote.get("previousClose")),
        "yearHigh": num(quote.get("yearHigh")),
        "yearLow": num(quote.get("yearLow")),
        "ma20": round(ma20, 4) if ma20 is not None else None,
        "above_20d_trend": price_above_20d,
        "price_above_20d_ma": price_above_20d,
        "setup": "MOMENTUM",
        "setupType": "SET2_OPTIONS_FLOW",
        "setupQualityScore": options_score,  # Use options flow score as proxy
        "convictionScore": options_score,
        "grade": "B",
        "scoreReasons": [candidate.get("catalyst_summary", "")],
        "entryZone": [round(price * 0.995, 4), round(price * 1.005, 4)] if price > 0 else [0, 0],
        "stopLoss": round(price * 0.95, 4) if price > 0 else 0,
        "riskPerShare": round(price * 0.05, 4) if price > 0 else 0,
        "tp1": round(price * 1.08, 4) if price > 0 else 0,
        "tp2": round(price * 1.15, 4) if price > 0 else 0,
        "positionShares": 0,
        "positionRiskDollars": 0,
        "positionSizingRule": "set2_fixed_0.75x",
        "action": "WATCH",
        "track_a_fail": False,
        "set2_options_score": options_score,
        "set2_trade_quality_score": round(trade_quality, 1),
        "set2_core_score": core_total,
        "set2_risk_score": risk,
        "set2_entry_rules": candidate.get("set2_entry_rules", {"hold_days_max": 5, "size_multiplier": 0.75}),
        "multi_source_flow": candidate.get("multi_source_flow", False),
        "options_flow_source": candidate.get("options_flow_source", "impliedoptions_auth"),
        "catalyst_summary": candidate.get("catalyst_summary", ""),
        "ask_side_sweep_count": int(candidate.get("ask_side_sweep_count") or 0),
        "bullish_premium_total": float(candidate.get("bullish_premium_total") or 0),
        "repeat_flow_count": int(candidate.get("repeat_flow_count") or 0),
        "put_call_vol_ratio": float(candidate.get("put_call_vol_ratio") or 0),
        "iv_rank": candidate.get("iv_rank"),
        "uoa_qualifying_rows": int(candidate.get("uoa_qualifying_rows") or 0),
        "danelfin_data_available": danelfin_available,
        "danelfin_ai_score": danelfin.get("ai_score") if danelfin_available else None,
        "earnings_date": earnings.get("earnings_date"),
        "days_to_earnings": days_to_earnings,
    }

    # If passed hard rejects and trade quality >= 60, mark as finalist-ready (threshold lowered from 60 to 35 for Barchart UOA compatibility)
    if not hard_rejects and trade_quality >= 20:
        record["set2_finalist_ready"] = True
    else:
        record["set2_finalist_ready"] = False
        if not hard_rejects:
            record["rejectReasons"] = [f"trade_quality_too_low_{round(trade_quality, 1)}"]
            record["stage2RejectReason"] = f"trade_quality_too_low_{round(trade_quality, 1)}"
            record["status"] = "REJECTED"

    return record


def main() -> int:
    _log("Set 2 scorer started")

    if not CANDIDATES_PATH.exists():
        _log(f"Candidates file missing: {CANDIDATES_PATH}; writing empty output")
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        empty = {"date": datetime.now(timezone.utc).date().isoformat(), "count": 0, "candidates": []}
        OUTPUT_PATH.write_text(json.dumps(empty, indent=2), encoding="utf-8")
        return 0

    try:
        payload = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        _log(f"ERROR reading candidates: {exc}")
        return 1

    candidates = payload.get("candidates", [])
    _log(f"Loaded {len(candidates)} Set 2 candidates")

    fmp_key = load_fmp_key()
    client = FmpClient(fmp_key)
    danelfin_scores = load_danelfin_scores()

    symbols = [c["ticker"] for c in candidates]
    _log(f"Fetching quotes for {len(symbols)} symbols")
    quotes = fetch_batch_quotes(client, symbols)
    _log(f"Quotes returned for {len(quotes)} symbols")
    _log(f"Fetching profiles for {len(symbols)} symbols")
    profiles = fetch_batch_profiles(client, symbols)
    _log(f"Profiles returned for {len(profiles)} symbols")

    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        ticker = candidate["ticker"]
        quote = quotes.get(ticker, {})
        profile = profiles.get(ticker)
        history = fetch_daily_history(client, ticker)
        earnings = check_earnings(client, ticker)
        record = score_candidate(candidate, quote, history, danelfin_scores, earnings, profile)
        scored.append(record)
        # Light throttle
        time.sleep(0.05)

    ok = [s for s in scored if s.get("status") == "OK"]
    ok.sort(key=lambda x: x.get("set2_trade_quality_score", 0), reverse=True)
    top5 = ok[:5]

    run_id = os.environ.get("SYSTEM2_RUN_ID") or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(4).hex()
    )

    output = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "run_id": run_id,
        "input_count": len(candidates),
        "scored_count": len(scored),
        "ok_count": len(ok),
        "top5_count": len(top5),
        "candidates": top5,
        "all_scored": scored,
        "fmp_errors": client.errors[:10],
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    tmp.replace(OUTPUT_PATH)

    meta = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "run_id": run_id,
        "input_count": len(candidates),
        "ok_count": len(ok),
        "top5_count": len(top5),
    }
    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    _log(f"Wrote {len(top5)} top Set 2 ideas to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
