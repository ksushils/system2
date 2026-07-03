"""
System 2 Options Positioning Engine.

Generates short-hold, expiry-tied paper ideas from existing local options
structure: max pain, call/put walls, expected move, and put/call ratio.
This subsystem is intentionally separate from swing finalists.
"""

import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_PATH = DATA_DIR / "options_positioning.json"
OUTCOMES_PATH = DATA_DIR / "options_positioning_outcomes.json"

env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

FMP_KEY = os.getenv("FMP_API_KEY", "")


def get_days_to_expiry(today=None):
    """Return calendar days to the next Friday expiry plus that expiry date."""
    today = today or date.today()
    days_ahead = 4 - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    expiry = today + timedelta(days=days_ahead)
    return days_ahead, expiry


def get_live_price(ticker):
    """Fetch current/last price from FMP, falling back to None on any issue."""
    if not FMP_KEY:
        return None
    urls = [
        ("https://financialmodelingprep.com/stable/quote", {"symbol": ticker, "apikey": FMP_KEY}),
        (f"https://financialmodelingprep.com/stable/quote/{ticker}", {"apikey": FMP_KEY}),
    ]
    for url, params in urls:
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if isinstance(data, list) and data:
                price = data[0].get("price") or data[0].get("lastPrice")
                if price:
                    return float(price)
        except Exception:
            continue
    return None


def _to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except Exception:
        return None


def _load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def load_options_dataset():
    fin_path = DATA_DIR / "finalist_options.json"
    if fin_path.exists():
        data = _load_json(fin_path, {})
        tickers = data.get("tickers", data if isinstance(data, dict) else {})
        if isinstance(tickers, dict):
            return tickers

    flow_path = ROOT / "options_flow.json"
    if flow_path.exists():
        data = _load_json(flow_path, {})
        rows = data.get("flows", data.get("data", data if isinstance(data, list) else []))
        if isinstance(rows, list):
            return {row.get("ticker"): row for row in rows if row.get("ticker")}
        if isinstance(rows, dict):
            return rows
    return {}


def get_options_structure(ticker):
    return load_options_dataset().get(ticker)


def _wall_strike(wall):
    if isinstance(wall, dict):
        return _to_float(wall.get("strike"))
    return _to_float(wall)


def _nearest_walls(walls, price, above=True):
    strikes = []
    for wall in walls or []:
        strike = _wall_strike(wall)
        if strike is None:
            continue
        if above and strike > price:
            strikes.append(strike)
        if not above and strike < price:
            strikes.append(strike)
    return sorted(strikes)[0] if above and strikes else (sorted(strikes, reverse=True)[0] if strikes else None)


def _risk_reward(direction, entry, target, stop):
    if not entry or not target or not stop:
        return 0
    if direction == "BULLISH":
        risk = entry - stop
        reward = target - entry
    else:
        risk = stop - entry
        reward = entry - target
    if risk <= 0:
        return 0
    return round(reward / risk, 2)


def compute_options_thesis(ticker, opts, dte, live_price=None):
    """Read options structure and produce one 1-2 day positioning thesis."""
    if not opts:
        return None

    price = live_price or get_live_price(ticker) or _to_float(opts.get("underlying_price"))
    if not price:
        return None

    max_pain = _to_float(opts.get("max_pain") or opts.get("maxPain"))
    pcr = _to_float(opts.get("put_call_ratio") if opts.get("put_call_ratio") is not None else opts.get("putCallRatio"))
    if max_pain is None or pcr is None:
        return None

    call_walls = opts.get("call_walls") or opts.get("callWalls") or []
    put_walls = opts.get("put_walls") or opts.get("putWalls") or []
    nearest_call = _nearest_walls(call_walls, price, above=True)
    nearest_put = _nearest_walls(put_walls, price, above=False)
    iv_rank = opts.get("iv_rank") if opts.get("iv_rank") is not None else opts.get("ivRank")
    if iv_rank is None:
        iv_rank = opts.get("iv_percentile")

    mp_dist_pct = (max_pain - price) / price * 100
    setup = None

    if nearest_call and nearest_put and abs(max_pain - price) / price < 0.01:
        return None

    if mp_dist_pct > 1.5 and pcr < 0.7:
        target = min(max_pain, nearest_call) if nearest_call else max_pain
        stop = nearest_put if nearest_put else price * 0.98
        rr = _risk_reward("BULLISH", price, target, stop)
        setup = {
            "direction": "BULLISH",
            "setup_type": "EXPIRY_PULL",
            "target": target,
            "stop": stop,
            "r_r": rr,
            "thesis": (
                f"Price ${price:.2f} is {mp_dist_pct:.1f}% below max pain ${max_pain:.2f}. "
                f"PCR {pcr:.2f} is call-heavy. Expiry pull toward ${max_pain:.2f}; "
                f"nearest call wall ${nearest_call:.2f} is ceiling/target." if nearest_call else
                f"Price ${price:.2f} is {mp_dist_pct:.1f}% below max pain ${max_pain:.2f}. "
                f"PCR {pcr:.2f} is call-heavy. Expiry pull toward ${max_pain:.2f}."
            ),
        }
    elif mp_dist_pct < -1.5 and pcr > 1.0:
        target = max(max_pain, nearest_put) if nearest_put else max_pain
        stop = nearest_call if nearest_call else price * 1.02
        rr = _risk_reward("BEARISH", price, target, stop)
        setup = {
            "direction": "BEARISH",
            "setup_type": "EXPIRY_PUSH",
            "target": target,
            "stop": stop,
            "r_r": rr,
            "thesis": (
                f"Price ${price:.2f} is {abs(mp_dist_pct):.1f}% above max pain ${max_pain:.2f}. "
                f"PCR {pcr:.2f} is put-heavy. Downward expiry push toward ${max_pain:.2f}; "
                f"nearest put wall ${nearest_put:.2f} is target/support." if nearest_put else
                f"Price ${price:.2f} is {abs(mp_dist_pct):.1f}% above max pain ${max_pain:.2f}. "
                f"PCR {pcr:.2f} is put-heavy. Downward expiry push toward ${max_pain:.2f}."
            ),
        }
    elif nearest_put and abs(price - nearest_put) / price < 0.005 and pcr < 1.0:
        stop = nearest_put * 0.99
        target = max_pain
        rr = _risk_reward("BULLISH", price, target, stop)
        setup = {
            "direction": "BULLISH",
            "setup_type": "PUT_WALL_BOUNCE",
            "target": target,
            "stop": stop,
            "r_r": rr,
            "thesis": (
                f"Price ${price:.2f} is testing put wall support at ${nearest_put:.2f}. "
                f"PCR {pcr:.2f} is not panic-heavy. Bounce target is max pain ${max_pain:.2f}."
            ),
        }

    if not setup or setup.get("r_r", 0) < 1.5 or dte > 3:
        return None

    return {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system": "options_positioning",
        "idea_stream": "options_positioning",
        "hold_period": "1-3 day",
        "timeframe": "1-3 day",
        "dte": dte,
        "price_at_generation": round(price, 2),
        "max_pain": round(max_pain, 2),
        "pcr": round(pcr, 3),
        "iv_rank": iv_rank,
        "expected_move": opts.get("expected_move"),
        "nearest_call_wall": round(nearest_call, 2) if nearest_call else None,
        "nearest_put_wall": round(nearest_put, 2) if nearest_put else None,
        "direction": setup["direction"],
        "setup_type": setup["setup_type"],
        "thesis": setup["thesis"],
        "entry": round(price, 2),
        "target": round(setup["target"], 2),
        "stop": round(setup["stop"], 2),
        "r_r": setup["r_r"],
        "confidence": "HIGH" if pcr < 0.5 or pcr > 1.3 else "MEDIUM",
        "data_source": opts.get("source", "barchart_per_ticker"),
        "warning": "Short-hold options setup. Max-pain effects are probabilistic. Use tight stops.",
    }


def _idea_key(idea):
    return "|".join([
        str(idea.get("ticker")),
        str(idea.get("setup_type")),
        str(idea.get("dte")),
        str(idea.get("entry")),
        str(idea.get("target")),
        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    ])


def update_outcomes(ideas):
    OUTCOMES_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_json(OUTCOMES_PATH, {"generated_at": None, "ideas": []})
    rows = existing.get("ideas", []) if isinstance(existing, dict) else []
    seen = {row.get("idea_key") for row in rows}
    for idea in ideas:
        key = _idea_key(idea)
        if key in seen:
            continue
        rows.append({
            "idea_key": key,
            "ticker": idea.get("ticker"),
            "idea_stream": "options_positioning",
            "hold_period": "1-3 day",
            "generated_at": idea.get("generated_at"),
            "dte": idea.get("dte"),
            "entry": idea.get("entry"),
            "target": idea.get("target"),
            "stop": idea.get("stop"),
            "r_r": idea.get("r_r"),
            "direction": idea.get("direction"),
            "setup_type": idea.get("setup_type"),
            "thesis": idea.get("thesis"),
            "price_at_expiry": None,
            "actual_r": None,
            "outcome": None,
        })
        seen.add(key)
    OUTCOMES_PATH.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "ideas": rows,
    }, indent=2))


def send_telegram(ideas, dte, expiry):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat or not ideas:
        return False
    msg = f"<b>OPTIONS POSITIONING - DTE {dte}</b>\nExpiry: {expiry}\n\n"
    for idea in ideas[:3]:
        msg += (
            f"<b>{idea['ticker']}</b> {idea['direction']} ({idea['setup_type']})\n"
            f"Entry: ${idea['entry']:.2f} -> Target: ${idea['target']:.2f} | Stop: ${idea['stop']:.2f}\n"
            f"R:R {idea['r_r']} | {idea['confidence']} confidence\n"
            f"<i>{idea['thesis'][:180]}...</i>\n\n"
        )
    msg += "<i>Paper-only options positioning. Short-hold, use tight stops.</i>"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def performance_summary():
    data = _load_json(OUTCOMES_PATH, {"ideas": []})
    rows = [r for r in data.get("ideas", []) if r.get("actual_r") is not None]
    if not rows:
        return {"resolved": 0}
    wins = [r for r in rows if float(r.get("actual_r") or 0) > 0]
    return {
        "resolved": len(rows),
        "win_rate": round(len(wins) / len(rows) * 100, 1),
        "avg_r": round(sum(float(r.get("actual_r") or 0) for r in rows) / len(rows), 3),
    }


def run(force=False, no_telegram=False):
    dte, expiry = get_days_to_expiry()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if dte > 3 and not force:
        output = {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dte": dte,
            "expiry": expiry.isoformat(),
            "active": False,
            "idea_stream": "options_positioning",
            "hold_period": "1-3 day",
            "reason": f"DTE {dte} > 3 - engine inactive",
            "idea_count": 0,
            "ideas": [],
            "performance": performance_summary(),
        }
        OUTPUT_PATH.write_text(json.dumps(output, indent=2))
        update_outcomes([])
        print(f"DTE {dte} - outside expiry window. Options engine sleeping.")
        return []

    print(f"Options engine ACTIVE - DTE {dte}, expiry {expiry}{' (forced)' if force else ''}")
    dataset = load_options_dataset()
    print(f"Checking {len(dataset)} tickers with options data...")
    ideas = []
    for ticker, opts in dataset.items():
        thesis = compute_options_thesis(ticker, opts, min(dte, 3) if force else dte)
        if thesis:
            ideas.append(thesis)
            print(f"  IDEA: {ticker} {thesis['direction']} ({thesis['setup_type']}) R:R {thesis['r_r']}")

    ideas.sort(key=lambda x: x["r_r"], reverse=True)
    ideas = ideas[:5]
    update_outcomes(ideas)
    telegram_sent = False if no_telegram else send_telegram(ideas, dte, expiry)
    output = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dte": dte,
        "expiry": expiry.isoformat(),
        "active": dte <= 3 or force,
        "forced": bool(force),
        "idea_stream": "options_positioning",
        "hold_period": "1-3 day",
        "idea_count": len(ideas),
        "ideas": ideas,
        "telegram_sent": telegram_sent,
        "performance": performance_summary(),
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"Options positioning: {len(ideas)} ideas generated")
    if ideas:
        print(f"Telegram alert: {'sent' if telegram_sent else 'not sent'}")
    return ideas


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Run even when outside 1-3 DTE window")
    parser.add_argument("--no-telegram", action="store_true", help="Do not send Telegram alerts")
    args = parser.parse_args()
    run(force=args.force, no_telegram=args.no_telegram)
