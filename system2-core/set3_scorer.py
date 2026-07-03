#!/usr/bin/env python3
"""Set 3 Catalyst Scorer.

Scores catalyst-driven candidates from catalyst_discovery.py.
Reads catalyst_candidates.json, enriches with FMP batch quotes,
applies directional bias filtering, and outputs stage2-compatible
records to data/set3_scored.json.

STAGED / PREVIEW ONLY — not wired into pipeline until Set 2 proven.
"""

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CANDIDATES_PATH = ROOT / "catalyst_candidates.json"
OUTPUT_PATH = ROOT / "data" / "set3_scored.json"
META_PATH = ROOT / "data" / "set3_scored_metadata.json"

BULLISH_KEYWORDS = {
    "upgrade": 3, "upgraded": 3, "raised": 2, "raises": 2, "beat": 2, "beats": 2,
    "outperform": 1, "overweight": 1, "buy": 1, "strong buy": 1,
    "price target raised": 2, "pt raised": 2, "reiterates buy": 1,
    "initiated with buy": 1, "initiated with overweight": 1,
    "approval": 2, "approved": 2, "clearance": 1, "partnership": 1,
    "contract award": 1, "buyback": 1, "dividend increase": 1,
    "merger": 1, "acquisition": 1, "to acquire": 1,
}

BEARISH_KEYWORDS = {
    "downgrade": -3, "downgraded": -3, "lowered": -2, "lowers": -2, "miss": -2, "misses": -2,
    "underperform": -1, "underweight": -1, "sell": -1, "strong sell": -1,
    "price target lowered": -2, "pt lowered": -2, "reiterates sell": -1,
    "initiated with sell": -1, "initiated with underweight": -1,
    "fda rejection": -3, "recall": -2, "warning": -1, "probe": -1,
    "investigation": -1, "lawsuit": -1, "bankruptcy": -3,
}


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
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_fmp_key() -> str:
    key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if key:
        return key.strip()
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
                    headers={"Accept": "application/json", "User-Agent": "system2-set3-scorer/1.0"},
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
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                self.errors.append(f"{endpoint}: {exc}")
                return None
        return None


def fetch_batch_quotes(client: FmpClient, symbols: list[str]) -> dict[str, dict[str, Any]]:
    quotes: dict[str, dict[str, Any]] = {}
    for i in range(0, len(symbols), 100):
        batch = symbols[i:i + 100]
        data = client.get("stable/batch-quote?symbols=" + ",".join(batch))
        if isinstance(data, list):
            for row in data:
                sym = str(row.get("symbol") or "").upper().strip()
                if sym:
                    quotes[sym] = row
    return quotes


def fetch_earnings_calendar(client: FmpClient, symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch upcoming earnings dates for symbols."""
    result: dict[str, dict[str, Any]] = {}
    today = datetime.now(timezone.utc).date().isoformat()
    data = client.get(f"stable/earnings-calendar?from={today}&to={today}&limit=5000")
    if isinstance(data, list):
        for row in data:
            sym = str(row.get("symbol") or "").upper().strip()
            if sym in symbols:
                result[sym] = row
    return result


def compute_directional_bias(candidate: dict[str, Any]) -> tuple[str, float]:
    """Return (bias, confidence) where bias is BULLISH/BEARISH/NEUTRAL."""
    summary = str(candidate.get("catalyst_summary", "")).lower()
    sub_types = candidate.get("sub_types", [candidate.get("sub_type", "")])

    bullish_hits = sum(w for kw, w in BULLISH_KEYWORDS.items() if kw in summary)
    bearish_hits = sum(abs(w) for kw, w in BEARISH_KEYWORDS.items() if kw in summary)

    type_bonus = 0
    if "earnings" in sub_types:
        if "beat" in summary:
            type_bonus += 2
        if "miss" in summary:
            type_bonus -= 2
    if "insider" in sub_types:
        type_bonus += 1
    if "fda" in sub_types:
        if "approval" in summary or "approved" in summary:
            type_bonus += 2
        if "rejection" in summary or "crl" in summary:
            type_bonus -= 2

    net = bullish_hits - bearish_hits + type_bonus

    if net >= 1:
        return "BULLISH", min(1.0, 0.5 + net * 0.1)
    elif net <= -1:
        return "BEARISH", min(1.0, 0.5 + abs(net) * 0.1)
    return "NEUTRAL", 0.3


def score_candidate(candidate: dict[str, Any], quote: dict[str, Any],
                    earnings: dict[str, Any] | None) -> dict[str, Any]:
    """Apply Set 3 scoring and return a stage2-compatible record."""
    ticker = candidate["ticker"]
    price = num(quote.get("price"))
    volume = num(quote.get("volume"))

    bias, bias_confidence = compute_directional_bias(candidate)

    hard_rejects: list[str] = []
    if price > 0 and (price < 10 or price > 2000):
        hard_rejects.append("price_out_of_range")
    if volume > 0 and volume < 200_000:
        hard_rejects.append("volume_too_low")
    if bias == "BEARISH":
        hard_rejects.append("bearish_catalyst")

    price_avg_50 = num(quote.get("priceAvg50"))
    price_above_50d = bool(price_avg_50 is not None and price > 0 and price > price_avg_50)
    price_avg_200 = num(quote.get("priceAvg200"))
    price_above_200d = bool(price_avg_200 is not None and price > 0 and price > price_avg_200)
    change_pct = num(quote.get("changePercentage"))
    market_cap = num(quote.get("marketCap"))

    if market_cap and market_cap >= 10_000_000_000:
        market_cap_bucket = "large"
    elif market_cap and market_cap >= 2_000_000_000:
        market_cap_bucket = "mid"
    elif market_cap and market_cap > 0:
        market_cap_bucket = "small"
    else:
        market_cap_bucket = "unknown"

    catalyst_score = num(candidate.get("catalyst_score"), 0)
    core_score = 0
    if catalyst_score >= 8:
        core_score = 40
    elif catalyst_score >= 5:
        core_score = 30
    elif catalyst_score >= 3:
        core_score = 20
    elif catalyst_score >= 1:
        core_score = 10

    technical_bonus = 0
    if price_above_50d:
        technical_bonus += 12
    if price_above_200d:
        technical_bonus += 8
    if change_pct > 2:
        technical_bonus += 8
    elif change_pct > 0:
        technical_bonus += 4

    sub_types = candidate.get("sub_types", [])
    multi_bonus = 0
    if len(sub_types) >= 2:
        multi_bonus += 10
    elif len(sub_types) >= 1:
        multi_bonus += 5

    bias_bonus = int(bias_confidence * 10)

    core_total = core_score + technical_bonus + multi_bonus + bias_bonus

    risk = 0
    if earnings:
        risk += 10
    if not price_above_50d:
        risk += 8
    if change_pct < -2:
        risk += 10

    trade_quality = max(0, min(100, core_total * 0.7 - risk * 0.3))

    if trade_quality >= 70:
        grade = "A"
    elif trade_quality >= 55:
        grade = "B"
    elif trade_quality >= 40:
        grade = "C"
    else:
        grade = "D"

    record: dict[str, Any] = {
        "symbol": ticker,
        "ticker": ticker,
        "set": 3,
        "set_source": "catalyst_discovery",
        "source": "set3_catalyst",
        "sub_type": "|".join(sub_types) if isinstance(sub_types, list) else str(candidate.get("sub_type", "catalyst")),
        "status": "REJECTED" if hard_rejects else "OK",
        "stage2RejectReason": hard_rejects[0] if hard_rejects else None,
        "rejectReasons": hard_rejects,
        "price": round(price, 4) if price > 0 else 0,
        "averageVolume": 0,
        "volume": round(volume, 2),
        "volumeRatio": 1.0,
        "atr14": 0.0,
        "atrPct": 0.0,
        "rsVsSpy": 0.0,
        "sector": "Unknown",
        "industry": "Unknown",
        "marketCap": market_cap,
        "market_cap_bucket": market_cap_bucket,
        "changePercentage": round(change_pct, 3) if change_pct != 0 else None,
        "previousClose": num(quote.get("previousClose")),
        "yearHigh": num(quote.get("yearHigh")),
        "yearLow": num(quote.get("yearLow")),
        "ma20": round(price_avg_50, 4) if price_avg_50 is not None else None,
        "above_20d_trend": price_above_50d,
        "price_above_20d_ma": price_above_50d,
        "setup": "CATALYST",
        "setupType": "SET3_CATALYST",
        "setupQualityScore": round(catalyst_score * 10, 1),
        "convictionScore": round(trade_quality, 1),
        "grade": grade,
        "scoreReasons": [str(candidate.get("catalyst_summary", ""))[:200]],
        "entryZone": [round(price * 0.995, 4), round(price * 1.005, 4)] if price > 0 else [0, 0],
        "stopLoss": round(price * 0.95, 4) if price > 0 else 0,
        "riskPerShare": round(price * 0.05, 4) if price > 0 else 0,
        "tp1": round(price * 1.08, 4) if price > 0 else 0,
        "tp2": round(price * 1.15, 4) if price > 0 else 0,
        "positionShares": 0,
        "positionRiskDollars": 0,
        "positionSizingRule": "set3_catalyst_0.75x",
        "action": "WATCH" if not hard_rejects else "SKIP",
        "track_a_fail": False,
        "set3_catalyst_score": round(catalyst_score, 2),
        "set3_trade_quality_score": round(trade_quality, 1),
        "set3_core_score": core_total,
        "set3_risk_score": risk,
        "set3_directional_bias": bias,
        "set3_bias_confidence": round(bias_confidence, 2),
        "set3_entry_rules": {"hold_days_max": 5, "size_multiplier": 0.75},
        "catalyst_summary": str(candidate.get("catalyst_summary", ""))[:240],
        "catalyst_date": candidate.get("catalyst_date"),
        "catalyst_datetime": candidate.get("catalyst_datetime"),
        "catalyst_sub_types": sub_types,
        "catalyst_sources": candidate.get("catalyst_sources", []),
        "catalyst_score": round(catalyst_score, 2),
        "price_target": candidate.get("price_target"),
        "price_when_posted": candidate.get("price_when_posted"),
        "analyst_company": candidate.get("analyst_company"),
        "news_url": candidate.get("news_url"),
        "publisher": candidate.get("publisher"),
        "earnings_surprise_pct": candidate.get("earnings_surprise_pct"),
        "earnings_actual": candidate.get("earnings_actual"),
        "earnings_estimate": candidate.get("earnings_estimate"),
        "insider_buy_count": candidate.get("insider_buy_count"),
        "insider_buy_value": candidate.get("insider_buy_value"),
        "bypasses_technical": bool(candidate.get("bypasses_technical") or candidate.get("bypass_technical")),
        "bypass_technical": bool(candidate.get("bypass_technical")),
        "bypass_reason": candidate.get("bypass_reason", ""),
        "set3_finalist_ready": not bool(hard_rejects) and trade_quality >= 50,
    }
    return record


def main() -> int:
    _log("Set 3 scorer started (STAGED/PREVIEW)")

    if not CANDIDATES_PATH.exists():
        _log(f"ERROR: candidates file missing: {CANDIDATES_PATH}")
        return 1

    candidates = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    if not isinstance(candidates, list):
        _log("ERROR: candidates file is not a list")
        return 1

    _log(f"Loaded {len(candidates)} catalyst candidates")

    load_dotenv()
    api_key = load_fmp_key()
    client = FmpClient(api_key)

    symbols = [str(c.get("symbol") or c.get("ticker") or "").upper().strip() for c in candidates]
    symbols = [s for s in symbols if s]

    _log(f"Fetching batch quotes for {len(symbols)} symbols")
    quotes = fetch_batch_quotes(client, symbols)
    _log(f"Quotes returned for {len(quotes)} symbols")

    _log("Fetching earnings calendar for upcoming dates")
    earnings_map = fetch_earnings_calendar(client, symbols)
    _log(f"Earnings calendar data for {len(earnings_map)} symbols")

    all_scored: list[dict[str, Any]] = []
    ok_records: list[dict[str, Any]] = []
    bias_counts = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}
    reject_reason_counts: dict[str, int] = {}

    for candidate in candidates:
        sym = str(candidate.get("symbol") or candidate.get("ticker") or "").upper().strip()
        quote = quotes.get(sym, {})
        earnings = earnings_map.get(sym)
        record = score_candidate(candidate, quote, earnings)
        bias_counts[record["set3_directional_bias"]] += 1
        all_scored.append(record)
        if record["status"] == "OK":
            ok_records.append(record)
        for reason in record["rejectReasons"]:
            reject_reason_counts[reason] = reject_reason_counts.get(reason, 0) + 1

    all_scored.sort(key=lambda r: r.get("set3_trade_quality_score", 0), reverse=True)
    ok_records.sort(key=lambda r: r.get("set3_trade_quality_score", 0), reverse=True)

    run_id = os.environ.get("SYSTEM2_RUN_ID") or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(4).hex()
    )

    payload = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "run_id": run_id,
        "input_count": len(candidates),
        "scored_count": len(all_scored),
        "ok_count": len(ok_records),
        "top5_count": len(ok_records),
        "candidates": ok_records,
        "all_scored": all_scored,
        "fmp_errors": client.errors,
        "bias_distribution": bias_counts,
        "reject_reason_counts": reject_reason_counts,
        "staged_preview": True,
        "set2_threshold_gate": "Set 2 has 0 resolved ideas (need >=5). Set 3 remains staged.",
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _log(f"Wrote {len(ok_records)} OK / {len(all_scored)} total to {OUTPUT_PATH}")

    metadata = {
        "stage": "SET3_CATALYST_SCORER",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "input_count": len(candidates),
        "ok_count": len(ok_records),
        "bias_distribution": bias_counts,
        "reject_reason_counts": reject_reason_counts,
        "fmp_errors": client.errors,
        "staged_preview": True,
    }
    META_PATH.write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")

    print(json.dumps({
        "stage": "set3_scorer",
        "ok_count": len(ok_records),
        "total_count": len(all_scored),
        "bias_distribution": bias_counts,
        "reject_reasons": reject_reason_counts,
        "fmp_errors": len(client.errors),
        "staged_preview": True,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
