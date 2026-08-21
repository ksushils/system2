#!/usr/bin/env python3
"""Alert when finalist persistence or the PMF gap heartbeat is stale."""

import json
import os
import argparse
import urllib.parse
import urllib.request
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/root/system2-core")
FUND = Path("/root/fund-system/data/fund.json")
GAP_LOG = ROOT / "logs/cron_gap_check.log"
STATE = ROOT / "logs/system2_heartbeat_state.json"
DISK_ALERT_THRESHOLD_PCT = 85
DISK_CRITICAL_THRESHOLD_PCT = 92


def load_env():
    for line in (ROOT / ".env").read_text(errors="ignore").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def json_objects(text):
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(text):
        start = text.find("{", pos)
        if start < 0:
            break
        try:
            value, pos = decoder.raw_decode(text, start)
            if isinstance(value, dict):
                yield value
        except ValueError:
            pos = start + 1


def trading_days_back(day, count):
    current = day
    seen = 0
    while seen < count:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            seen += 1
    return current


def send_alert(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return {"sent": False, "reason": "missing Telegram credentials"}
    body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=30) as response:
        return {"sent": response.status == 200}


def disk_issue(used_pct):
    if used_pct >= DISK_CRITICAL_THRESHOLD_PCT:
        return "disk_critical_92", f"CRITICAL disk usage {used_pct}% >= {DISK_CRITICAL_THRESHOLD_PCT}% threshold"
    if used_pct >= DISK_ALERT_THRESHOLD_PCT:
        return "disk_warning_85", f"disk usage {used_pct}% >= {DISK_ALERT_THRESHOLD_PCT}% threshold"
    return None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    load_env()
    if args.self_test:
        result = send_alert("[TEST] SYSTEM2 HEARTBEAT FAILURE: simulated no-finalists-recorded condition")
        print(json.dumps({"ok": result.get("sent") is True, "self_test": True, "telegram": result}))
        return
    now = datetime.now(timezone.utc)
    cutoff = trading_days_back(now.date(), 2).isoformat()
    fund = json.loads(FUND.read_text())
    # The canonical ideas ledger contains persisted Stage-7 finalists. Older
    # rows may carry trade_quality_finalist, while current rows do not.
    finalist_dates = [
        str(row.get("date"))
        for row in fund.get("ideas", [])
        if row.get("date") and row.get("ticker") and row.get("entry") is not None
    ]
    latest_finalist = max(finalist_dates, default=None)
    gap_dates = []
    for row in json_objects(GAP_LOG.read_text(errors="ignore")):
        if row.get("ok") is not True:
            continue
        results = row.get("results") or []
        stamp = results[0].get("pre_market_checked_at") if results else None
        if stamp:
            gap_dates.append(str(stamp)[:10])
    latest_gap = max(gap_dates, default=None)
    stale = []
    issue_codes = []
    if not latest_finalist or latest_finalist < cutoff:
        stale.append(f"no finalists recorded for 2 trading days (latest={latest_finalist})")
        issue_codes.append("finalists_stale")
    if not latest_gap or latest_gap < cutoff:
        stale.append(f"no successful PMF gap check for 2 trading days (latest={latest_gap})")
        issue_codes.append("gap_check_stale")
    disk = shutil.disk_usage("/")
    disk_used_pct = round((disk.used / disk.total) * 100, 1) if disk.total else 0.0
    disk_code, disk_message = disk_issue(disk_used_pct)
    if disk_code:
        stale.append(disk_message)
        issue_codes.append(disk_code)
    fingerprint = "|".join(issue_codes)
    previous = json.loads(STATE.read_text()) if STATE.exists() else {}
    result = {"ok": not stale, "checked_at": now.isoformat(), "latest_finalist": latest_finalist, "latest_gap": latest_gap, "cutoff": cutoff, "disk_used_pct": disk_used_pct, "disk_alert_threshold_pct": DISK_ALERT_THRESHOLD_PCT, "disk_critical_threshold_pct": DISK_CRITICAL_THRESHOLD_PCT, "issues": stale}
    if stale and previous.get("fingerprint") != fingerprint:
        heading = "🚨 CRITICAL SYSTEM2 DISK USAGE" if "disk_critical_92" in issue_codes else "⚠ SYSTEM2 HEALTH WARNING"
        result["telegram"] = send_alert(heading + "\n" + "\n".join(stale))
    STATE.write_text(json.dumps({**result, "fingerprint": fingerprint}, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
