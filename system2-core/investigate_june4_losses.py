import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path


LOSERS = ["VST", "BWA", "SNAP", "AXON", "STM", "BKR", "NUE"]
MARKET = ["SPY", "QQQ", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY"]


def read_env(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return out


def fmp_key():
    env = read_env("/root/system2-core/.env")
    key = env.get("FMP_API_KEY") or os.environ.get("FMP_API_KEY")
    if not key:
        raise SystemExit("missing FMP_API_KEY")
    return key


def get_json(url):
    with urllib.request.urlopen(url, timeout=45) as r:
        return json.load(r)


def get_hist(symbol, key):
    url = (
        "https://financialmodelingprep.com/stable/historical-price-eod/full"
        f"?symbol={urllib.parse.quote(symbol)}&apikey={key}"
    )
    data = get_json(url)
    rows = data.get("historical") if isinstance(data, dict) else data
    return sorted(rows or [], key=lambda row: row.get("date", ""))


def num(value):
    try:
        return float(value)
    except Exception:
        return None


def pct(start, end):
    if start in (None, 0) or end is None:
        return None
    return round((end / start - 1) * 100, 2)


def by_date(rows):
    return {row["date"]: row for row in rows if row.get("date")}


def true_range(row, prev_close):
    high, low = num(row.get("high")), num(row.get("low"))
    if high is None or low is None:
        return None
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr14(rows, entry_date="2026-06-04"):
    idxs = [idx for idx, row in enumerate(rows) if row.get("date") == entry_date]
    if not idxs or idxs[0] < 14:
        return None
    idx = idxs[0]
    trs = []
    for pos in range(idx - 13, idx + 1):
        prev_close = num(rows[pos - 1].get("close")) if pos > 0 else None
        tr = true_range(rows[pos], prev_close)
        if tr is not None:
            trs.append(tr)
    return round(sum(trs) / len(trs), 4) if trs else None


def previous_close(rows, date):
    pos = next((idx for idx, row in enumerate(rows) if row.get("date") == date), None)
    if pos is None or pos <= 0:
        return None
    return num(rows[pos - 1].get("close"))


def load_admin_api(db):
    sessions = [
        row for row in db.get("sessions", [])
        if row.get("is_admin") and row.get("expires", 0) > time.time() * 1000
    ]
    if not sessions:
        return {"admin_token_available": False, "score_stats": None, "ideas_sample": None}
    token = sessions[-1]["token"]

    def local_get(path):
        sep = "&" if "?" in path else "?"
        return get_json(f"http://127.0.0.1:3210{path}{sep}token={urllib.parse.quote(token)}")

    return {
        "admin_token_available": True,
        "score_stats": local_get("/api/score/stats"),
        "ideas_sample": local_get("/api/ideas?limit=3"),
    }


def main():
    key = fmp_key()
    db = json.load(open("/root/fund-system/data/fund.json"))
    histories = {symbol: get_hist(symbol, key) for symbol in MARKET + LOSERS}

    market = []
    for symbol in MARKET:
        bd = by_date(histories[symbol])
        for date in ["2026-06-04", "2026-06-05"]:
            row = bd.get(date, {})
            prev = previous_close(histories[symbol], date)
            market.append({
                "symbol": symbol,
                "date": date,
                "open": num(row.get("open")),
                "close": num(row.get("close")),
                "open_to_close_pct": pct(num(row.get("open")), num(row.get("close"))),
                "prev_close_to_close_pct": pct(prev, num(row.get("close"))),
            })

    ideas = {
        row["ticker"]: row
        for row in db.get("ideas", [])
        if row.get("date") == "2026-06-04" and row.get("ticker") in LOSERS
    }
    trades = []
    for symbol in LOSERS:
        idea = ideas[symbol]
        rows = histories[symbol]
        bd = by_date(rows)
        entry, stop, target = num(idea.get("entry")), num(idea.get("stop")), num(idea.get("target"))
        risk = num(idea.get("risk_per_share")) or (entry - stop if entry is not None and stop is not None else None)
        atr = atr14(rows)
        stop_distance = entry - stop if entry is not None and stop is not None else None
        hit_any = []
        hit_after_logging = []
        for date in ["2026-06-04", "2026-06-05", "2026-06-06"]:
            low = num(bd.get(date, {}).get("low"))
            if low is not None and stop is not None and low <= stop:
                hit_any.append(date)
                if date > "2026-06-04":
                    hit_after_logging.append(date)
        px1 = num(idea.get("px_1d"))
        manual_r = round((px1 - entry) / risk, 3) if None not in [px1, entry, risk] and risk else None
        trades.append({
            "ticker": symbol,
            "sector": idea.get("sector"),
            "logged_at": idea.get("logged_at"),
            "entry": entry,
            "stop": stop,
            "target": target,
            "risk_per_share": risk,
            "px_1d": px1,
            "system_r_1d": idea.get("r_1d"),
            "manual_r_1d": manual_r,
            "jun4_open": num(bd.get("2026-06-04", {}).get("open")),
            "jun4_low": num(bd.get("2026-06-04", {}).get("low")),
            "jun4_close": num(bd.get("2026-06-04", {}).get("close")),
            "jun5_open": num(bd.get("2026-06-05", {}).get("open")),
            "jun5_low": num(bd.get("2026-06-05", {}).get("low")),
            "jun5_close": num(bd.get("2026-06-05", {}).get("close")),
            "jun6_open": num(bd.get("2026-06-06", {}).get("open")),
            "jun6_low": num(bd.get("2026-06-06", {}).get("low")),
            "jun6_close": num(bd.get("2026-06-06", {}).get("close")),
            "entry_vs_jun4_open_pct": pct(num(bd.get("2026-06-04", {}).get("open")), entry),
            "jun5_open_vs_entry_pct": pct(entry, num(bd.get("2026-06-05", {}).get("open"))),
            "stop_distance_pct": round(stop_distance / entry * 100, 2) if entry and stop_distance is not None else None,
            "atr14_calc": atr,
            "stop_atr_multiple": round(stop_distance / atr, 2) if atr and stop_distance is not None else None,
            "hit_any_dates": hit_any,
            "hit_after_logging_dates": hit_after_logging,
            "paper_hit": idea.get("hit"),
            "max_dd_pct": idea.get("max_dd_pct"),
        })

    out = {
        "market": market,
        "trades": trades,
        **load_admin_api(db),
    }
    Path("/tmp/june4_investigation.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
