import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


MARKS = [
    {"days": 1, "field": "px_1d", "rfield": "r_1d", "stage": 1},
    {"days": 3, "field": "px_3d", "rfield": "r_3d", "stage": 3},
    {"days": 10, "field": "px_10d", "rfield": "r_10d", "stage": 10},
]


def read_env(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return out


def get_json(url):
    with urllib.request.urlopen(url, timeout=45) as r:
        return json.load(r)


def fetch_history(symbol, key):
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


def r_value(entry, risk, price):
    if not risk or risk <= 0 or price is None:
        return None
    return round((price - entry) / risk, 3)


def has_recorded_entry(row):
    return row.get("trade_entered") is True


def stamp_measurement_population(row):
    row["measurement_population"] = "ENTERED_TRADE" if has_recorded_entry(row) else "WATCHLIST_UNENTERED"
    if not has_recorded_entry(row):
        row["would_be_measurement"] = "DIRECTIONAL_MARKOUT"


def set_would_be_markout(row, days, price):
    if has_recorded_entry(row):
        return
    entry = num(row.get("entry"))
    risk = num(row.get("risk_per_share")) or (entry - num(row.get("stop")) if entry is not None and num(row.get("stop")) is not None else None)
    stamp_measurement_population(row)
    row[f"would_be_r_markout_{days}d"] = r_value(entry, risk, price)
    row[f"would_be_return_pct_{days}d"] = round((price - entry) / entry * 100, 2) if entry and price is not None else None


def simulated_long_exit(row, window, fallback_close, timeout_hit):
    stop = num(row.get("stop"))
    target = num(row.get("target"))
    for bar in window:
        open_px = num(bar.get("open"))
        high = num(bar.get("high"))
        low = num(bar.get("low"))
        if stop is not None and low is not None and low <= stop:
            return {
                "hit": "STOP",
                "exit_price": open_px if open_px is not None and open_px < stop else stop,
                "exit_date": bar.get("date"),
            }
        if target is not None and high is not None and high >= target:
            return {
                "hit": "TARGET",
                "exit_price": open_px if open_px is not None and open_px > target else target,
                "exit_date": bar.get("date"),
            }
    return {"hit": timeout_hit, "exit_price": fallback_close, "exit_date": window[-1].get("date") if window else None}


def apply_marks(row, hist, today):
    idea_date = datetime.fromisoformat(row["date"] + "T00:00:00+00:00")
    age_days = (today - idea_date).days
    after_entry = [bar for bar in hist if bar.get("date") > row["date"]]
    updated = 0
    for mark in MARKS:
        if age_days < mark["days"] or len(after_entry) < mark["days"]:
            continue
        bar = after_entry[mark["days"] - 1]
        close_px = num(bar.get("close"))
        row[mark["field"]] = close_px
        stamp_measurement_population(row)
        set_would_be_markout(row, mark["days"], close_px)
        if mark["days"] >= 10 and len(after_entry) >= 5 and row.get("would_be_r_markout_5d") is None:
            set_would_be_markout(row, 5, num(after_entry[4].get("close")))
        window = after_entry[:mark["days"]]
        highs = [num(bar.get("high")) for bar in window if num(bar.get("high")) is not None]
        lows = [num(bar.get("low")) for bar in window if num(bar.get("low")) is not None]
        entry = num(row.get("entry"))
        risk = num(row.get("risk_per_share")) or (entry - num(row.get("stop")) if entry is not None and num(row.get("stop")) is not None else None)
        if highs and entry:
            row["max_gain_pct"] = round((max(highs) - entry) / entry * 100, 2)
        if lows and entry:
            row["max_dd_pct"] = round((min(lows) - entry) / entry * 100, 2)
        exit_info = simulated_long_exit(row, window, close_px, "TIME" if mark["days"] >= 10 else "OPEN")
        row["hit"] = exit_info["hit"]
        row[mark["rfield"]] = r_value(entry, risk, exit_info["exit_price"])
        if exit_info["hit"] in {"STOP", "TARGET"}:
            row["paper_exit_reason"] = exit_info["hit"]
            row["paper_exit_at"] = exit_info["exit_date"]
            row["paper_exit_price"] = exit_info["exit_price"]
            row["paper_exit_r"] = row[mark["rfield"]]
            row["paper_status"] = "CLOSED"
            row["paper_outcome"] = "WIN" if exit_info["hit"] == "TARGET" else "LOSS"
        elif mark["days"] >= 10:
            row["paper_status"] = "CLOSED"
            row["paper_outcome"] = "WIN" if (row[mark["rfield"]] or 0) > 0 else ("LOSS" if (row[mark["rfield"]] or 0) < 0 else "TIMEOUT")
        else:
            row["paper_status"] = "OPEN"
            row["paper_outcome"] = None
        row["scored_stage"] = mark["stage"]
        row["scored_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        updated += 1
    return updated


def main():
    key = read_env("/root/system2-core/.env").get("FMP_API_KEY") or os.environ.get("FMP_API_KEY")
    if not key:
        raise SystemExit("missing FMP key")
    path = Path("/root/fund-system/data/fund.json")
    data = json.loads(path.read_text())
    today = datetime.now(timezone.utc)
    touched = []
    histories = {}
    for row in data.get("ideas", []):
        if not row.get("date") or not row.get("ticker") or not row.get("entry"):
            continue
        was_scored = row.get("scored_stage", 0) > 0 or row.get("r_1d") is not None or row.get("hit") in {"STOP", "TARGET", "TIME"}
        if not was_scored:
            continue
        before = {key: row.get(key) for key in ["ticker", "r_1d", "r_3d", "r_10d", "hit", "paper_exit_price", "paper_exit_r"]}
        for key_name in ["px_1d", "px_3d", "px_10d", "r_1d", "r_3d", "r_10d", "max_gain_pct", "max_dd_pct"]:
            row[key_name] = None
        row["hit"] = None
        row["paper_status"] = "OPEN"
        row["paper_outcome"] = None
        row["paper_exit_reason"] = None
        row["paper_exit_at"] = None
        row["paper_exit_price"] = None
        row["paper_exit_r"] = None
        row["scored_stage"] = 0
        symbol = row["ticker"]
        if symbol not in histories:
            histories[symbol] = fetch_history(symbol, key)
        apply_marks(row, histories[symbol], today)
        after = {key: row.get(key) for key in ["ticker", "r_1d", "r_3d", "r_10d", "hit", "paper_exit_price", "paper_exit_r"]}
        if before != after:
            touched.append({"before": before, "after": after})
    backup = path.with_suffix(".json.bak-recalc")
    backup.write_text(path.read_text())
    path.write_text(json.dumps(data, indent=2))
    print(json.dumps({"ok": True, "updated_records": len(touched), "changes": touched}, indent=2))


if __name__ == "__main__":
    main()
