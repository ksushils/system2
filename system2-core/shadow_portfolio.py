#!/usr/bin/env python3
"""Track rejected System 2 names and identify missed moves."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "system2_shadow.db"
BLOCKLIST_PATH = ROOT / "shadow_reentry_blocklist.json"
REPORT_PATH = ROOT / "shadow_portfolio_latest.json"
FMP_BASE = "https://financialmodelingprep.com"


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


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS shadow_portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            rejection_stage TEXT NOT NULL,
            rejection_reason TEXT NOT NULL,
            rejection_date TEXT NOT NULL,
            last_known_price REAL,
            current_price REAL,
            price_change_pct REAL,
            trading_days_since_rejection INTEGER DEFAULT 0,
            last_checked_at TEXT,
            missed_move_alerted_at TEXT,
            UNIQUE(symbol, rejection_stage, rejection_reason, rejection_date)
        )
    """)
    db.commit()
    return db


def load_json(name: str, fallback: Any) -> Any:
    path = ROOT / name
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def rejection_rows(run_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for filename, stage, reason_key, default_reason in [
        ("stage3_news_rejections.json", "stage3_news", "stage3RejectReason", "news_landmine"),
        ("stage7_cluster_rejections.json", "stage7_correlation", "clusterRejectReason", "cluster_cap"),
    ]:
        for row in load_json(filename, []):
            rows.append({
                "symbol": row.get("symbol") or row.get("ticker"),
                "rejection_stage": stage,
                "rejection_reason": row.get(reason_key) or default_reason,
                "rejection_date": run_date,
                "last_known_price": row.get("price"),
            })

    return [row for row in rows if row.get("symbol")]


def ingest_rejections(db: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    before = db.total_changes
    db.executemany("""
        INSERT OR IGNORE INTO shadow_portfolio (
            symbol, rejection_stage, rejection_reason, rejection_date, last_known_price
        ) VALUES (?, ?, ?, ?, ?)
    """, [
        (
            str(row["symbol"]).upper(),
            row["rejection_stage"],
            row["rejection_reason"],
            row["rejection_date"],
            row.get("last_known_price"),
        )
        for row in rows
    ])
    db.commit()
    return db.total_changes - before


def fetch_daily(symbol: str, from_date: str) -> list[dict[str, Any]]:
    key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_KEY")
    if not key:
        raise RuntimeError("FMP_API_KEY missing")
    endpoint = (
        f"{FMP_BASE}/stable/historical-price-eod/full?"
        + urllib.parse.urlencode({"symbol": symbol, "from": from_date, "apikey": key})
    )
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json", "User-Agent": "system2-shadow-portfolio/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8", "ignore"))
    return data if isinstance(data, list) else data.get("historical", [])


def send_telegram(text: str, *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"sent": False, "dry_run": True, "message": text}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return {"sent": False, "reason": "telegram credentials missing"}
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", "ignore"))


def refresh(db: sqlite3.Connection, *, dry_run: bool) -> dict[str, Any]:
    checked = alerts = errors = 0
    alert_results = []
    active_rows = db.execute("""
        SELECT * FROM shadow_portfolio
        WHERE trading_days_since_rejection <= 10
          AND missed_move_alerted_at IS NULL
        ORDER BY rejection_date, symbol
    """).fetchall()

    for row in active_rows:
        try:
            bars = sorted(
                fetch_daily(row["symbol"], row["rejection_date"]),
                key=lambda item: str(item.get("date") or ""),
            )
            bars = [bar for bar in bars if str(bar.get("date") or "") > row["rejection_date"]]
            trading_days = len(bars)
            current_price = float(bars[-1]["close"]) if bars else row["last_known_price"]
            start_price = float(row["last_known_price"] or 0)
            change_pct = (
                ((current_price - start_price) / start_price) * 100
                if start_price and current_price is not None
                else None
            )
            checked += 1
            missed = (
                change_pct is not None
                and change_pct > 10
                and 0 < trading_days <= 10
            )
            alert_at = row["missed_move_alerted_at"]
            if missed:
                text = (
                    "SYSTEM 2 MISSED MOVE\n"
                    f"{row['symbol']} rose {change_pct:.2f}% within {trading_days} trading days "
                    f"after rejection at {row['rejection_stage']}.\n"
                    f"Reason: {row['rejection_reason']}\n"
                    "Paper-mode shadow tracking only."
                )
                alert_results.append(send_telegram(text, dry_run=dry_run))
                alert_at = datetime.now(timezone.utc).isoformat()
                alerts += 1
            db.execute("""
                UPDATE shadow_portfolio
                SET current_price=?, price_change_pct=?,
                    trading_days_since_rejection=?, last_checked_at=?,
                    missed_move_alerted_at=?
                WHERE id=?
            """, (
                current_price,
                round(change_pct, 4) if change_pct is not None else None,
                trading_days,
                datetime.now(timezone.utc).isoformat(),
                alert_at,
                row["id"],
            ))
        except Exception as exc:
            errors += 1
            alert_results.append({"symbol": row["symbol"], "error": str(exc)})
    db.commit()
    return {
        "checked": checked,
        "missed_move_alerts": alerts,
        "errors": errors,
        "alert_results": alert_results,
    }


def write_blocklist(db: sqlite3.Connection, as_of: date | None = None) -> list[dict[str, Any]]:
    as_of = as_of or date.today()
    rows = db.execute("""
        SELECT symbol, rejection_stage, rejection_reason, rejection_date,
               trading_days_since_rejection, last_checked_at
        FROM shadow_portfolio
        WHERE rejection_reason != 'vix_regime_off'
        ORDER BY rejection_date DESC
    """).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["last_checked_at"]:
            trading_days = int(row["trading_days_since_rejection"] or 0)
        else:
            rejected = date.fromisoformat(row["rejection_date"])
            trading_days = sum(
                1
                for offset in range(1, (as_of - rejected).days + 1)
                if date.fromordinal(rejected.toordinal() + offset).weekday() < 5
            )
        if trading_days > 10 or row["symbol"] in latest:
            continue
        latest[row["symbol"]] = {
            "symbol": row["symbol"],
            "rejection_stage": row["rejection_stage"],
            "rejection_reason": row["rejection_reason"],
            "rejection_date": row["rejection_date"],
            "trading_days_ago": trading_days,
        }
    blocklist = sorted(latest.values(), key=lambda row: row["symbol"])
    BLOCKLIST_PATH.write_text(json.dumps(blocklist, indent=2), encoding="utf-8")
    return blocklist


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--dry-run-alerts", action="store_true")
    parser.add_argument("--ingest-only", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    db = connect(Path(args.db))
    inserted = ingest_rejections(db, rejection_rows(args.date))
    refresh_result = (
        {"checked": 0, "missed_move_alerts": 0, "errors": 0, "alert_results": []}
        if args.ingest_only
        else refresh(db, dry_run=args.dry_run_alerts)
    )
    blocklist = write_blocklist(db)
    total = db.execute("SELECT COUNT(*) FROM shadow_portfolio").fetchone()[0]
    report = {
        "table": "shadow_portfolio",
        "db": str(Path(args.db)),
        "inserted": inserted,
        "total_records": total,
        "reentry_blocked_symbols": len(blocklist),
        **refresh_result,
        "paper_only": True,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
