#!/usr/bin/env python3
"""
System 2 pre-market gap check with zone replanning (Prompt 3 of 5).

Preview/dry-run by default. Paper mode only: updates idea records and
optional Telegram alerts, never places trades.

New in Prompt 3:
  - Gap classification: GAPPED_ABOVE, GAPPED_BELOW, IN_ZONE
  - Replan logic:
      * ADVERSE gap > 1.5 ATR → DEMOTE to WATCH, plan_valid=False
      * FAVOURABLE gap > 1.0 ATR → replan from pre-market price
      * IN_ZONE → no replan needed
  - Stores replan fields on idea record
  - Telegram alerts for both adverse and favourable gaps
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parent
FUND_DB = Path(os.environ.get("FUND_DB_PATH", "/root/fund-system/data/fund.json"))
FMP_BASE = "https://financialmodelingprep.com"


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip("\"'")


def fmp_key() -> str:
    load_dotenv()
    key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if not key:
        raise RuntimeError("FMP_API_KEY not found")
    return key


def fetch_json(url: str) -> Any:
    r = requests.get(url, timeout=25)
    if r.status_code >= 400:
        return []
    return r.json()


def quote_for(symbol: str, api_key: str) -> dict[str, Any]:
    quote_url = f"{FMP_BASE}/stable/quote?symbol={symbol}&apikey={api_key}"
    q = fetch_json(quote_url)
    if isinstance(q, list) and q:
        return {"source": "quote_fallback", "raw": q[0]}
    if isinstance(q, dict):
        return {"source": "quote_fallback", "raw": q}
    return {"source": "NO_DATA", "raw": {}}


def price_from_quote(q: dict[str, Any]) -> float | None:
    raw = q.get("raw") or {}
    for key in ("preMarketPrice", "postMarketPrice", "price"):
        value = raw.get(key)
        if value is not None:
            try:
                price = float(value)
                if price > 0:
                    return price
            except (TypeError, ValueError):
                pass
    return None


def idea_atr(idea: dict[str, Any]) -> float | None:
    for key in ("atr14", "atr_daily", "daily_atr14", "atr", "risk_per_share"):
        value = idea.get(key)
        try:
            number = float(value)
            if number > 0:
                return number
        except (TypeError, ValueError):
            pass
    entry = idea.get("entry")
    stop = idea.get("stop")
    try:
        risk = abs(float(entry) - float(stop))
        return risk if risk > 0 else None
    except (TypeError, ValueError):
        return None


def entry_zone_bounds(idea: dict[str, Any]) -> tuple[float | None, float | None]:
    """Return (low, high) from entryZone array or single entry."""
    ez = idea.get("entryZone")
    if isinstance(ez, list) and len(ez) >= 2:
        try:
            low = float(ez[0])
            high = float(ez[1])
            if low > 0 and high > 0:
                return (min(low, high), max(low, high))
        except (TypeError, ValueError):
            pass
    entry = idea.get("entry")
    try:
        e = float(entry)
        if e > 0:
            return (e, e)
    except (TypeError, ValueError):
        pass
    return (None, None)


def send_telegram(text: str) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return {"sent": False, "reason": "missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"}
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=25,
    )
    return {"sent": r.ok, "status": r.status_code}


def classify_and_replan(
    idea: dict[str, Any],
    price: float | None,
    source: str,
    adverse_atr_threshold: float = 1.5,
    favourable_atr_threshold: float = 1.0,
) -> dict[str, Any]:
    ticker = str(idea.get("ticker") or "").upper()
    atr = idea_atr(idea)
    entry_low, entry_high = entry_zone_bounds(idea)
    entry_mid = ((entry_low or 0) + (entry_high or 0)) / 2 if entry_low and entry_high else (entry_low or entry_high or 0)

    out = {
        "ticker": ticker,
        "date": idea.get("date"),
        "pre_market_checked_at": None,
        "pre_market_price": price,
        "pre_market_price_source": source,
        "pre_market_gap_pct": None,
        "pre_market_gap_atr_multiple": None,
        "pre_market_gap_threshold_atr": adverse_atr_threshold,
        "pre_market_gap_adverse": False,
        "pre_market_gap_favourable": False,
        "premarket_gap_error": None,
        "pre_market_gap_error": None,
        # Prompt 3 replan fields
        "entry_zone_low": entry_low,
        "entry_zone_high": entry_high,
        "entry_zone_mid": round(entry_mid, 4) if entry_mid else None,
        "gap_pct": None,
        "gap_atr_units": None,
        "gap_direction": None,
        "replan_type": None,
        "plan_valid": None,
        "replan_reason": None,
        "replanned_entry_zone_low": None,
        "replanned_entry_zone_high": None,
        "replanned_stop": None,
        "replanned_tp1": None,
        "replanned_tp2": None,
        "replanned_rr": None,
        "replanned_at": None,
        "alert": None,
    }

    if not price or price <= 0:
        out["alert"] = f"{ticker}: no pre-market/quote price"
        out["premarket_gap_error"] = out["alert"]
        out["pre_market_gap_error"] = out["alert"]
        out["replan_type"] = "NO_DATA"
        out["plan_valid"] = False
        return out

    if not entry_low or not entry_high:
        out["alert"] = f"{ticker}: missing entry zone"
        out["premarket_gap_error"] = out["alert"]
        out["pre_market_gap_error"] = out["alert"]
        out["replan_type"] = "NO_DATA"
        out["plan_valid"] = False
        return out

    if not atr or atr <= 0:
        out["alert"] = f"{ticker}: missing ATR for gap rule"
        out["premarket_gap_error"] = out["alert"]
        out["pre_market_gap_error"] = out["alert"]
        out["replan_type"] = "NO_DATA"
        out["plan_valid"] = False
        return out

    gap_pct = ((price - entry_mid) / entry_mid) * 100
    gap_dollars = abs(price - entry_mid)
    gap_atr_units = gap_dollars / atr

    out["gap_pct"] = round(gap_pct, 2)
    out["gap_atr_units"] = round(gap_atr_units, 2)
    out["pre_market_gap_pct"] = out["gap_pct"]
    out["pre_market_gap_atr_multiple"] = out["gap_atr_units"]

    # Gap direction classification
    if price > entry_high * 1.005:
        out["gap_direction"] = "GAPPED_ABOVE"
    elif price < entry_low * 0.995:
        out["gap_direction"] = "GAPPED_BELOW"
    else:
        out["gap_direction"] = "IN_ZONE"

    # Replan logic
    if out["gap_direction"] == "GAPPED_BELOW" and gap_atr_units > adverse_atr_threshold:
        out["replan_type"] = "ADVERSE_GAP"
        out["plan_valid"] = False
        out["replan_reason"] = (
            f"Gapped {gap_pct:.1f}% below zone ({gap_atr_units:.1f}x ATR) — do not enter"
        )
        out["pre_market_gap_adverse"] = True
        out["alert"] = (
            f"⚠️ {ticker} ADVERSE GAP {gap_pct:.1f}% — WATCH ONLY\n"
            f"Price ${price:.2f} vs zone ${entry_low:.2f}-${entry_high:.2f} ({gap_atr_units:.1f}x ATR)\n"
            f"Do not enter at current price."
        )

    elif out["gap_direction"] == "GAPPED_ABOVE" and gap_atr_units > favourable_atr_threshold:
        # Replan from pre-market price
        new_entry_low = price * 0.997
        new_entry_high = price * 1.003
        new_entry_mid = price
        new_stop = price - (1.5 * atr)
        new_tp1 = new_entry_mid + (3.0 * (new_entry_mid - new_stop))
        new_tp2 = new_entry_mid + (4.0 * (new_entry_mid - new_stop))
        new_rr = round((new_tp1 - new_entry_mid) / (new_entry_mid - new_stop), 2) if (new_entry_mid - new_stop) > 0 else None

        out["replan_type"] = "FAVOURABLE_GAP_REPLAN"
        out["plan_valid"] = True
        out["replan_reason"] = (
            f"Gapped {gap_pct:.1f}% above zone ({gap_atr_units:.1f}x ATR) — replanned from pre-market price"
        )
        out["replanned_entry_zone_low"] = round(new_entry_low, 2)
        out["replanned_entry_zone_high"] = round(new_entry_high, 2)
        out["replanned_stop"] = round(new_stop, 2)
        out["replanned_tp1"] = round(new_tp1, 2)
        out["replanned_tp2"] = round(new_tp2, 2)
        out["replanned_rr"] = new_rr
        out["replanned_at"] = datetime.now(timezone.utc).isoformat()
        out["pre_market_gap_favourable"] = True
        out["alert"] = (
            f"⚡ {ticker} REPLANNED after {gap_pct:.1f}% gap up.\n"
            f"New zone: ${new_entry_low:.2f}-${new_entry_high:.2f}\n"
            f"New stop: ${new_stop:.2f} | New TP1: ${new_tp1:.2f} | R:R {new_rr}"
        )

    elif out["gap_direction"] == "IN_ZONE":
        out["replan_type"] = "NONE"
        out["plan_valid"] = True
        out["replan_reason"] = "Price within entry zone — no replan needed"
        out["alert"] = None

    else:
        # Gap direction is GAPPED_ABOVE or GAPPED_BELOW but below threshold
        out["replan_type"] = "NONE"
        out["plan_valid"] = True
        out["replan_reason"] = (
            f"Gap {gap_pct:.1f}% ({gap_atr_units:.1f}x ATR) within threshold — no replan needed"
        )
        out["alert"] = None

    return out


def run(apply: bool = False, send_alerts: bool = False) -> dict[str, Any]:
    api_key = fmp_key()
    now = datetime.now(timezone.utc)
    db = json.loads(FUND_DB.read_text(encoding="utf-8"))
    ideas = db.get("ideas", [])
    open_ideas = [
        i for i in ideas
        if i.get("paper_status") == "OPEN"
        and i.get("paper") is not False
        and i.get("actual_entry_price") is None  # not yet entered
    ]

    results = []
    alerts = []
    replanned = []
    adverse = []
    in_zone = []
    no_data = []

    for idea in open_ideas:
        ticker = str(idea.get("ticker") or "").upper()
        q = quote_for(ticker, api_key)
        price = price_from_quote(q)
        row = classify_and_replan(idea, price, q.get("source") or "NO_DATA")
        row["pre_market_checked_at"] = now.isoformat()
        results.append(row)

        if apply:
            # Preserve original fields that classify_and_replan may not overwrite
            update = {
                "pre_market_checked_at": row["pre_market_checked_at"],
                "pre_market_price": row["pre_market_price"],
                "pre_market_price_source": row["pre_market_price_source"],
                "pre_market_gap_pct": row["pre_market_gap_pct"],
                "pre_market_gap_atr_multiple": row["pre_market_gap_atr_multiple"],
                "pre_market_gap_threshold_atr": row["pre_market_gap_threshold_atr"],
                "pre_market_gap_adverse": row["pre_market_gap_adverse"],
                "pre_market_gap_favourable": row["pre_market_gap_favourable"],
                "premarket_gap_error": row["premarket_gap_error"],
                "pre_market_gap_error": row["pre_market_gap_error"],
                # Prompt 3 fields
                "entry_zone_low": row["entry_zone_low"],
                "entry_zone_high": row["entry_zone_high"],
                "entry_zone_mid": row["entry_zone_mid"],
                "gap_pct": row["gap_pct"],
                "gap_atr_units": row["gap_atr_units"],
                "gap_direction": row["gap_direction"],
                "replan_type": row["replan_type"],
                "plan_valid": row["plan_valid"],
                "replan_reason": row["replan_reason"],
                "replanned_entry_zone_low": row["replanned_entry_zone_low"],
                "replanned_entry_zone_high": row["replanned_entry_zone_high"],
                "replanned_stop": row["replanned_stop"],
                "replanned_tp1": row["replanned_tp1"],
                "replanned_tp2": row["replanned_tp2"],
                "replanned_rr": row["replanned_rr"],
                "replanned_at": row["replanned_at"],
            }
            idea.update(update)

        if row["alert"] and row["replan_type"] in ("ADVERSE_GAP", "FAVOURABLE_GAP_REPLAN"):
            alert_row = {
                "ticker": ticker,
                "replan_type": row["replan_type"],
                "message": row["alert"],
            }
            if apply and send_alerts:
                alert_row.update(send_telegram(row["alert"]))
            else:
                alert_row["sent"] = False
                alert_row["reason"] = "dry-run or send-alerts disabled"
            alerts.append(alert_row)

        if row["replan_type"] == "ADVERSE_GAP":
            adverse.append(row)
        elif row["replan_type"] == "FAVOURABLE_GAP_REPLAN":
            replanned.append(row)
        elif row["replan_type"] == "NONE":
            in_zone.append(row)
        else:
            no_data.append(row)

    if apply:
        FUND_DB.write_text(json.dumps(db, indent=2), encoding="utf-8")

    # Summary telegram message
    if apply and send_alerts and (adverse or replanned or in_zone):
        summary_lines = ["🌅 PRE-MARKET GAP CHECK SUMMARY"]
        if replanned:
            summary_lines.append("\n⚡ REPLANNED:")
            for r in replanned:
                summary_lines.append(f"  {r['ticker']}: {r['gap_pct']}% ({r['gap_atr_units']}x ATR)")
        if in_zone:
            summary_lines.append("\n✅ IN ZONE:")
            for r in in_zone:
                summary_lines.append(f"  {r['ticker']}: {r['gap_pct']}% ({r['gap_atr_units']}x ATR)")
        if adverse:
            summary_lines.append("\n⚠️ WATCH ONLY:")
            for r in adverse:
                summary_lines.append(f"  {r['ticker']}: {r['gap_pct']}% ({r['gap_atr_units']}x ATR)")
        summary_lines.append(f"\nTotal: {len(open_ideas)} checked | {len(replanned)} replanned | {len(in_zone)} in zone | {len(adverse)} adverse | {len(no_data)} no data")
        send_telegram("\n".join(summary_lines))

    return {
        "ok": True,
        "apply": apply,
        "send_alerts": send_alerts,
        "checked_at": now.isoformat(),
        "open_ideas_checked": len(open_ideas),
        "fmp_call_count": len(open_ideas),
        "adverse_count": len(adverse),
        "replanned_count": len(replanned),
        "in_zone_count": len(in_zone),
        "no_data_count": len(no_data),
        "alerts_that_would_fire": alerts,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--send-alerts", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(apply=args.apply, send_alerts=args.send_alerts), indent=2))


if __name__ == "__main__":
    main()
