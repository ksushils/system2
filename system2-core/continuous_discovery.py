#!/usr/bin/env python3
"""System 1 continuous discovery engine.

Runs as a paper-mode discovery layer only. It detects fast-source deltas,
alerts only when two independent sources agree, and feeds tomorrow's candidate
pool via data/universe_expansion.json. It never enters trades.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import schedule

try:
    from social_scraper import compute_upvote_velocity
except Exception:
    compute_upvote_velocity = None

try:
    from idea_lifecycle import record_stage
except Exception:
    def record_stage(*args, **kwargs):  # type: ignore
        return None


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BASELINE_PATH = DATA / "discovery_baseline.json"
FEED_PATH = DATA / "discovery_feed.json"
OUTCOMES_PATH = DATA / "discovery_outcomes.json"
EXPANSION_PATH = DATA / "universe_expansion.json"
FEEDBACK_PATH = DATA / "discovery_feedback.json"
LOG_DIR = ROOT / "logs"

MAX_ALERTS_PER_CYCLE = 3
MAX_QUIET_LOG = 200
MAX_ALERT_LOG = 100


def load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN") or ""
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID") or ""
GETXAPI_KEY = os.getenv("GETXAPI_KEY", "")
FMP_KEY = os.getenv("FMP_API_KEY") or os.getenv("FMP_KEY") or ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def today() -> str:
    return utc_now().date().isoformat()


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def symbol_of(row: Any) -> str:
    if isinstance(row, dict):
        value = row.get("ticker") or row.get("symbol") or row.get("baseSymbol")
    else:
        value = row
    return str(value or "").strip().upper()


def is_market_hours(force: bool = False) -> bool:
    if force:
        return True
    now = utc_now()
    if now.weekday() >= 5:
        return False
    hour = now.hour + now.minute / 60
    return 13.5 <= hour <= 21.0


def load_baseline() -> dict[str, Any]:
    return read_json(BASELINE_PATH, {})


def load_feed() -> dict[str, Any]:
    feed = read_json(FEED_PATH, {})
    return {
        "date": feed.get("date", today()),
        "generated_at": feed.get("generated_at"),
        "last_cycle": feed.get("last_cycle"),
        "cycles_today": feed.get("cycles_today", 0) if feed.get("date") == today() else 0,
        "alerts": feed.get("alerts", []),
        "quiet_log": feed.get("quiet_log", feed.get("events", [])),
        "stats": feed.get("stats", {}),
        "engine": feed.get("engine", {}),
    }


def ensure_artifacts() -> None:
    if not EXPANSION_PATH.exists():
        write_json(EXPANSION_PATH, {
            "date": today(),
            "generated_at": iso_now(),
            "source": "continuous_discovery",
            "paper_only": True,
            "expansion_count": 0,
            "additions": [],
        })
    if not OUTCOMES_PATH.exists():
        write_json(OUTCOMES_PATH, {
            "date": today(),
            "generated_at": iso_now(),
            "alert_count": 0,
            "alerts": [],
        })


def save_feed(feed: dict[str, Any]) -> None:
    feed["date"] = today()
    feed["generated_at"] = iso_now()
    write_json(FEED_PATH, feed)


def send_telegram(message: str) -> dict[str, Any]:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print(f"ALERT (telegram not configured): {message}")
        return {"sent": False, "reason": "missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"}
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        return {"sent": resp.ok, "status": resp.status_code}
    except Exception as exc:
        print(f"Telegram error: {exc}")
        return {"sent": False, "reason": str(exc)}


def current_price(ticker: str) -> float | None:
    if not FMP_KEY:
        return None
    try:
        url = "https://financialmodelingprep.com/stable/quote"
        resp = requests.get(url, params={"symbol": ticker, "apikey": FMP_KEY}, timeout=8)
        data = resp.json()
        if isinstance(data, list) and data:
            value = data[0].get("price")
            return float(value) if value is not None else None
    except Exception:
        return None
    return None


def poll_apewisdom(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    spikes: list[dict[str, Any]] = []
    try:
        resp = requests.get(
            "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1",
            headers={"User-Agent": "System2/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return spikes
        base_aw = baseline.get("apewisdom", {})
        for item in resp.json().get("results", []):
            ticker = str(item.get("ticker", "")).upper()
            if not ticker:
                continue
            rank_now = int(item.get("rank") or 999)
            mentions = int(item.get("mentions") or 0)
            mentions_24h = int(item.get("mentions_24h_ago") or 0)
            upvotes = int(item.get("upvotes") or 0)
            upvotes_24h = int(item.get("upvotes_24h_ago") or 0)
            base_rank = int(base_aw.get(ticker, {}).get("rank") or 999)
            rank_improvement = base_rank - rank_now

            velocity = {}
            if compute_upvote_velocity:
                velocity = compute_upvote_velocity(
                    current_mentions=mentions,
                    current_rank=rank_now,
                    baseline_mentions=mentions_24h,
                    baseline_rank=base_rank,
                    current_upvotes=upvotes,
                    baseline_upvotes=upvotes_24h,
                )

            # Alert on strong rank improvement OR fast velocity
            if rank_improvement > 100 or velocity.get("velocity_score", 0) >= 7:
                detail = f"Rank {base_rank}->{rank_now} (+{rank_improvement}), {mentions} mentions"
                if velocity.get("is_accelerating"):
                    detail += f"; velocity {velocity['velocity_score']}/10"
                score = min(rank_improvement // 50, 5)
                if velocity.get("velocity_score", 0) >= 7:
                    score = max(score, 4)
                spikes.append({
                    "ticker": ticker,
                    "source": "apewisdom",
                    "signal": "REDDIT_RANK_SPIKE",
                    "delta": rank_improvement,
                    "detail": detail,
                    "score": score,
                    "velocity_score": velocity.get("velocity_score", 0),
                    "mention_growth_pct": velocity.get("mention_growth_pct", 0),
                })
    except Exception as exc:
        print(f"ApeWisdom poll error: {exc}")
    return spikes


def stocktwits_tickers() -> list[str]:
    tickers: list[str] = []
    for path in [DATA / "barchart_uoa.json", DATA / "confluence_signals.json", DATA / "social_sentiment.json"]:
        data = read_json(path, {})
        if path.name == "barchart_uoa.json":
            tickers.extend(symbol_of(c) for c in data.get("candidates", [])[:30])
        elif path.name == "confluence_signals.json":
            tickers.extend(symbol_of(c) for c in data.get("signals", [])[:30])
        elif path.name == "social_sentiment.json":
            tickers.extend(list((data.get("tickers") or {}).keys())[:30])
    seen: set[str] = set()
    return [t for t in tickers if t and not (t in seen or seen.add(t))][:50]


def poll_stocktwits(baseline: dict[str, Any], tickers: list[str] | None = None) -> list[dict[str, Any]]:
    spikes: list[dict[str, Any]] = []
    tickers = tickers or stocktwits_tickers()
    base_st = baseline.get("stocktwits", {})
    for ticker in tickers[:30]:
        try:
            resp = requests.get(
                f"https://api.stocktwits.com/api/2/streams/symbol/{urllib.parse.quote(ticker)}.json",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            if resp.status_code != 200:
                continue
            messages = resp.json().get("messages") or []
            if len(messages) < 15:
                continue
            bull = sum(
                1
                for msg in messages
                if (msg.get("entities", {}).get("sentiment") or {}).get("basic") == "Bullish"
            )
            bull_pct = bull / len(messages) * 100
            base_pct = float(base_st.get(ticker, {}).get("bull_pct", 50))
            delta = bull_pct - base_pct
            if delta > 20 and bull_pct > 65:
                spikes.append({
                    "ticker": ticker,
                    "source": "stocktwits",
                    "signal": "SENTIMENT_SURGE",
                    "delta": round(delta, 1),
                    "detail": f"Bull {base_pct:.0f}%->{bull_pct:.0f}% (+{delta:.0f} pts), {len(messages)} msgs",
                    "score": 3 if delta > 30 else 2,
                })
            time.sleep(0.25)
        except Exception:
            continue
    return spikes


def poll_barchart_uoa(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    spikes: list[dict[str, Any]] = []
    data = read_json(DATA / "barchart_uoa.json", {})
    base_bc = baseline.get("barchart_uoa", {})
    for row in data.get("candidates", []):
        ticker = symbol_of(row)
        if not ticker:
            continue
        try:
            vol_oi = float(row.get("vol_oi_ratio") or row.get("volumeOpenInterestRatio") or 0)
        except Exception:
            vol_oi = 0.0
        base_voi = float(base_bc.get(ticker, {}).get("vol_oi_ratio", 0) or 0)
        if ticker not in base_bc and vol_oi > 10:
            spikes.append({
                "ticker": ticker,
                "source": "barchart_uoa",
                "signal": "NEW_UOA_SPIKE",
                "delta": round(vol_oi, 2),
                "detail": f"NEW: {vol_oi:.1f}x vol/OI ratio",
                "score": 4 if vol_oi > 20 else 3,
            })
        elif vol_oi > max(base_voi * 2, 5):
            spikes.append({
                "ticker": ticker,
                "source": "barchart_uoa",
                "signal": "UOA_ACCELERATING",
                "delta": round(vol_oi - base_voi, 2),
                "detail": f"Vol/OI {base_voi:.1f}x->{vol_oi:.1f}x",
                "score": 3,
            })
    return spikes


def poll_openinsider(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    spikes: list[dict[str, Any]] = []
    data = read_json(DATA / "insider_trades.json", {})
    known = set(baseline.get("openinsider", {}).get("known_tickers", []))
    for ticker, row in (data.get("tickers") or {}).items():
        if ticker in known:
            continue
        value = float(row.get("insider_buy_value") or 0)
        count = int(row.get("insider_buy_count") or 0)
        is_cluster = bool(row.get("is_cluster") or count >= 2)
        if value > 100000:
            spikes.append({
                "ticker": ticker.upper(),
                "source": "openinsider",
                "signal": "NEW_INSIDER_BUY",
                "delta": value,
                "detail": f"{'CLUSTER BUY' if is_cluster else 'Buy'} ${value:,.0f}",
                "score": 5 if is_cluster else 3,
            })
    return spikes


def poll_getxapi(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    if not GETXAPI_KEY:
        return []
    spikes: list[dict[str, Any]] = []
    try:
        query = "(from:unusual_whales OR from:Benzinga OR from:StockMKTNewz OR from:DeItaone) (breakout OR unusual OR upgrade OR catalyst OR watch) lang:en -is:retweet"
        url = "https://api.getxapi.com/twitter/tweet/advanced_search?" + urllib.parse.urlencode({"q": query, "product": "Latest"})
        resp = requests.get(url, headers={"Authorization": f"Bearer {GETXAPI_KEY}", "Accept": "application/json"}, timeout=20)
        if resp.status_code != 200:
            return []
        payload = resp.json()
        tweets = payload if isinstance(payload, list) else payload.get("tweets") or payload.get("data") or payload.get("results") or []
        grouped: dict[str, list[str]] = {}
        for tweet in tweets[:100]:
            text = str(tweet.get("text") or tweet.get("full_text") or "")
            for symbol in set(re.findall(r"\$([A-Z]{1,5})(?![A-Z])", text.upper())):
                grouped.setdefault(symbol, []).append(text[:180])
        base_x = baseline.get("getxapi", {})
        for ticker, posts in grouped.items():
            base_count = int(base_x.get(ticker, {}).get("mentions", 0) or 0)
            if len(posts) >= max(2, base_count + 2):
                spikes.append({
                    "ticker": ticker,
                    "source": "getxapi",
                    "signal": "TRUSTED_X_MENTION_SPIKE",
                    "delta": len(posts) - base_count,
                    "detail": f"{len(posts)} trusted X mentions; sample: {posts[0][:90]}",
                    "score": 2 if len(posts) < 4 else 3,
                })
    except Exception as exc:
        print(f"GetXAPI poll error: {exc}")
    return spikes


def detect_confluence(spikes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for spike in spikes:
        ticker = spike.get("ticker")
        if ticker:
            by_ticker.setdefault(ticker, []).append(spike)
    alerts: list[dict[str, Any]] = []
    quiet: list[dict[str, Any]] = []
    feedback = load_feedback_tuning()
    trust_sources = set(feedback.get("trust_sources", []))
    raise_bar_sources = set(feedback.get("raise_bar_sources", []))
    for ticker, rows in by_ticker.items():
        sources = sorted({row["source"] for row in rows})
        total_score = sum(int(row.get("score") or 0) for row in rows)
        payload = {
            "ticker": ticker,
            "sources": sources,
            "source_count": len(sources),
            "total_score": total_score,
            "signals": rows,
            "detected_at": iso_now(),
            "vetted": False,
            "action": "WATCH - pipeline will score tonight",
        }
        trusted_single = (
            len(sources) == 1
            and sources[0] in trust_sources
            and total_score >= 5
            and sources[0] not in raise_bar_sources
        )
        if len(sources) >= 2 or trusted_single:
            payload["alert_type"] = "CONFLUENCE"
            if trusted_single:
                payload["alert_type"] = "TRUSTED_SINGLE_SOURCE"
                payload["feedback_tuned"] = True
            alerts.append(payload)
        else:
            payload["alert_type"] = "SINGLE_SOURCE"
            quiet.append(payload)
    alerts.sort(key=lambda row: row["total_score"], reverse=True)
    quiet.sort(key=lambda row: row["total_score"], reverse=True)
    return alerts[:MAX_ALERTS_PER_CYCLE], quiet


def load_feedback_tuning() -> dict[str, Any]:
    feedback = read_json(FEEDBACK_PATH, {})
    total = int(feedback.get("total_discovered") or 0)
    if total < 30:
        print(f"Feedback dormant - only {total} discovered, need 30")
        return {"active": False, "trust_sources": [], "raise_bar_sources": [], "total_discovered": total}
    rec = feedback.get("recommendation", {})
    print("Feedback tuning active")
    return {
        "active": True,
        "trust_sources": rec.get("trust_sources", []),
        "raise_bar_sources": rec.get("raise_bar_sources", []),
        "total_discovered": total,
    }


def format_telegram_alert(alert: dict[str, Any]) -> str:
    signals = "\n".join(f"  - {s['source']}: {s['detail']}" for s in alert.get("signals", []))
    sources = ", ".join(alert.get("sources", []))
    return (
        f"<b>DISCOVERY ALERT - {alert['ticker']}</b>\n"
        f"Sources agreeing: <b>{sources}</b>\n\n"
        f"{signals}\n\n"
        f"<i>UNVETTED - not a buy signal.</i>\n"
        f"Pipeline will score tonight. Check Signal Lab for manual review."
    )


def add_to_pipeline_universe(alerts: list[dict[str, Any]]) -> int:
    existing = read_json(EXPANSION_PATH, {})
    additions = existing.get("additions", [])
    seen = {row.get("ticker") for row in additions}
    for alert in alerts:
        ticker = alert["ticker"]
        if ticker in seen:
            continue
        additions.append({
            "ticker": ticker,
            "symbol": ticker,
            "source": "continuous_discovery",
            "sub_type": "system1_confluence",
            "sources_detected": alert["sources"],
            "added_reason": f"{alert['source_count']} sources: {', '.join(alert['sources'])}",
            "catalyst_summary": f"System 1 confluence: {', '.join(alert['sources'])}",
            "detected_at": alert["detected_at"],
            "date": today(),
            "bypass_technical": False,
            "vetted": False,
        })
        seen.add(ticker)
    output = {
        "date": today(),
        "generated_at": iso_now(),
        "source": "continuous_discovery",
        "paper_only": True,
        "expansion_count": len(additions),
        "additions": additions[-200:],
    }
    write_json(EXPANSION_PATH, output)
    return len(output["additions"])


def append_outcomes(alerts: list[dict[str, Any]]) -> int:
    data = read_json(OUTCOMES_PATH, {"alerts": []})
    rows = data.get("alerts", [])
    existing = {(row.get("ticker"), row.get("alerted_at")) for row in rows}
    for alert in alerts:
        key = (alert["ticker"], alert["detected_at"])
        if key in existing:
            continue
        rows.append({
            "ticker": alert["ticker"],
            "alert_date": today(),
            "sources": alert["sources"],
            "signals": alert["signals"],
            "alerted_at": alert["detected_at"],
            "price_at_alert": current_price(alert["ticker"]),
            "price_3d_later": None,
            "price_5d_later": None,
            "move_3d_pct": None,
            "move_5d_pct": None,
            "outcome_r": None,
            "was_finalist": False,
            "was_entered": False,
            "vetted": False,
        })
    data = {
        "date": today(),
        "generated_at": iso_now(),
        "alert_count": len(rows),
        "alerts": rows[-500:],
    }
    write_json(OUTCOMES_PATH, data)
    return len(data["alerts"])


def update_outcomes() -> dict[str, int]:
    data = read_json(OUTCOMES_PATH, {"alerts": []})
    rows = data.get("alerts", [])
    updated = 0
    finalists = load_recent_finalists()
    entered = load_entered_tickers()
    now = utc_now()
    for row in rows:
        row["was_finalist"] = row.get("was_finalist") or row.get("ticker") in finalists
        row["was_entered"] = row.get("was_entered") or row.get("ticker") in entered
        alerted = parse_dt(row.get("alerted_at"))
        if not alerted:
            continue
        age_days = (now - alerted).days
        price0 = row.get("price_at_alert")
        if not price0:
            continue
        if age_days >= 3 and row.get("price_3d_later") is None:
            p = current_price(row["ticker"])
            if p is not None:
                row["price_3d_later"] = p
                row["move_3d_pct"] = round((p - price0) / price0 * 100, 2)
                updated += 1
        if age_days >= 5 and row.get("price_5d_later") is None:
            p = current_price(row["ticker"])
            if p is not None:
                row["price_5d_later"] = p
                row["move_5d_pct"] = round((p - price0) / price0 * 100, 2)
                updated += 1
    if rows:
        data["generated_at"] = iso_now()
        data["alert_count"] = len(rows)
        write_json(OUTCOMES_PATH, data)
    return {"updated": updated, "alerts": len(rows)}


def parse_dt(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def load_recent_finalists() -> set[str]:
    tickers: set[str] = set()
    for path in [ROOT / "stage2_surgical_strike_top40.json", ROOT / "stage2_confluence_ranked_top40.json", ROOT / "stage7_clustered_survivors.json"]:
        data = read_json(path, [])
        rows = data if isinstance(data, list) else data.get("ideas") or data.get("finalists") or data.get("candidates") or []
        tickers.update(symbol_of(row) for row in rows)
    return {t for t in tickers if t}


def load_entered_tickers() -> set[str]:
    fund = read_json(Path("/root/fund-system/data/fund.json"), {})
    rows = fund.get("ideas", [])
    return {symbol_of(row) for row in rows if row.get("actual_entry_price") or row.get("paper_entry_price") or row.get("entryRecorded")}


def recent_alert_tickers(feed: dict[str, Any], hours: int = 4) -> set[str]:
    cutoff = utc_now() - timedelta(hours=hours)
    out: set[str] = set()
    for alert in feed.get("alerts", []):
        detected = parse_dt(alert.get("detected_at"))
        if detected and detected >= cutoff:
            out.add(alert.get("ticker"))
    return out


def run_discovery_cycle(force: bool = False, send_alerts: bool = True) -> dict[str, Any]:
    if not is_market_hours(force=force):
        print("Outside market hours - skipping")
        feed = load_feed()
        feed["engine"] = {"running": True, "market_hours": False, "last_skip": iso_now()}
        save_feed(feed)
        return {"skipped": True, "reason": "outside_market_hours"}

    print(f"\n=== Discovery cycle {utc_now().strftime('%H:%M')} UTC ===")
    baseline = load_baseline()
    feed = load_feed()

    all_spikes: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    for name, fn in [
        ("apewisdom", poll_apewisdom),
        ("barchart_uoa", poll_barchart_uoa),
        ("openinsider", poll_openinsider),
        ("getxapi", poll_getxapi),
    ]:
        spikes = fn(baseline)
        all_spikes.extend(spikes)
        source_counts[name] = len(spikes)
        print(f"{name}: {len(spikes)} spikes")
        time.sleep(0.5)

    stocktwits_spikes = poll_stocktwits(baseline)
    all_spikes.extend(stocktwits_spikes)
    source_counts["stocktwits"] = len(stocktwits_spikes)
    print(f"stocktwits: {len(stocktwits_spikes)} spikes")
    print(f"Total spikes: {len(all_spikes)}")

    alerts, quiet = detect_confluence(all_spikes)
    recent = recent_alert_tickers(feed)
    new_alerts = [alert for alert in alerts if alert["ticker"] not in recent]
    print(f"Confluence alerts: {len(alerts)}, new: {len(new_alerts)}, quiet: {len(quiet)}")

    telegram_results = []
    if send_alerts:
        for alert in new_alerts:
            telegram_results.append(send_telegram(format_telegram_alert(alert)))
            print(f"ALERT: {alert['ticker']} ({', '.join(alert['sources'])})")

    for alert in new_alerts:
        try:
            record_stage(alert["ticker"], today(), "DISCOVERED", {
                "source": "continuous_discovery",
                "sources_agreeing": alert.get("sources", []),
                "detail": alert.get("action") or "WATCH - pipeline will score tonight",
                "signals": alert.get("signals", []),
                "alert_type": alert.get("alert_type"),
            })
        except Exception:
            pass

    expansion_count = add_to_pipeline_universe(new_alerts) if new_alerts else len(read_json(EXPANSION_PATH, {}).get("additions", []))
    outcome_count = append_outcomes(new_alerts) if new_alerts else len(read_json(OUTCOMES_PATH, {}).get("alerts", []))
    outcome_updates = update_outcomes()

    feed["alerts"] = (new_alerts + feed.get("alerts", []))[:MAX_ALERT_LOG]
    feed["quiet_log"] = (quiet + feed.get("quiet_log", []))[:MAX_QUIET_LOG]
    feed["last_cycle"] = iso_now()
    feed["cycles_today"] = int(feed.get("cycles_today", 0)) + 1
    feed["stats"] = {
        "spikes_detected": len(all_spikes),
        "source_counts": source_counts,
        "confluence_alerts_this_cycle": len(alerts),
        "new_alerts_this_cycle": len(new_alerts),
        "quiet_events_this_cycle": len(quiet),
        "stocks_added_to_universe": expansion_count,
        "outcomes_logged": outcome_count,
        "outcome_updates": outcome_updates.get("updated", 0),
        "telegram": telegram_results,
        "feedback": load_feedback_tuning(),
    }
    feed["engine"] = {
        "running": True,
        "market_hours": True,
        "interval_minutes": 15,
        "paper_only": True,
        "sources": {
            "apewisdom": True,
            "stocktwits": True,
            "barchart_uoa": True,
            "openinsider": True,
            "getxapi": bool(GETXAPI_KEY),
        },
    }
    save_feed(feed)
    return feed


def capture_stocktwits_baseline(tickers: list[str]) -> dict[str, dict[str, Any]]:
    baseline: dict[str, dict[str, Any]] = {}
    for ticker in tickers[:30]:
        try:
            resp = requests.get(
                f"https://api.stocktwits.com/api/2/streams/symbol/{urllib.parse.quote(ticker)}.json",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            if resp.status_code != 200:
                continue
            messages = resp.json().get("messages") or []
            if not messages:
                continue
            bull = sum(
                1
                for msg in messages
                if (msg.get("entities", {}).get("sentiment") or {}).get("basic") == "Bullish"
            )
            baseline[ticker] = {"bull_pct": round(bull / len(messages) * 100, 1), "message_count": len(messages)}
            time.sleep(0.25)
        except Exception:
            continue
    return baseline


def update_baseline() -> dict[str, Any]:
    print("Updating discovery baseline...")
    baseline = {
        "date": today(),
        "updated_at": iso_now(),
        "apewisdom": {},
        "stocktwits": {},
        "barchart_uoa": {},
        "openinsider": {"last_checked_at": iso_now(), "known_tickers": []},
        "getxapi": {},
    }
    try:
        resp = requests.get(
            "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1",
            headers={"User-Agent": "System2/1.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            for item in resp.json().get("results", []):
                ticker = str(item.get("ticker", "")).upper()
                if ticker:
                    baseline["apewisdom"][ticker] = {
                        "rank": item.get("rank", 999),
                        "mentions": item.get("mentions", 0),
                    }
    except Exception as exc:
        print(f"Baseline ApeWisdom error: {exc}")

    insider = read_json(DATA / "insider_trades.json", {})
    baseline["openinsider"]["known_tickers"] = sorted((insider.get("tickers") or {}).keys())

    barchart = read_json(DATA / "barchart_uoa.json", {})
    for row in barchart.get("candidates", []):
        ticker = symbol_of(row)
        if ticker:
            baseline["barchart_uoa"][ticker] = {"vol_oi_ratio": float(row.get("vol_oi_ratio") or 0)}

    tickers = list(baseline["barchart_uoa"].keys()) or stocktwits_tickers()
    baseline["stocktwits"] = capture_stocktwits_baseline(tickers)

    x_data = read_json(ROOT / "x_candidates.json", {})
    x_rows = x_data.get("candidates", x_data if isinstance(x_data, list) else [])
    for row in x_rows:
        ticker = symbol_of(row)
        if ticker:
            baseline["getxapi"][ticker] = {"mentions": row.get("x_mentions") or row.get("post_count") or 1}

    write_json(BASELINE_PATH, baseline)
    print(
        "Baseline updated: "
        f"{len(baseline['apewisdom'])} ApeWisdom, "
        f"{len(baseline['stocktwits'])} StockTwits, "
        f"{len(baseline['barchart_uoa'])} Barchart, "
        f"{len(baseline['openinsider']['known_tickers'])} insider tickers"
    )
    return baseline


def write_test_alert(send: bool = False) -> dict[str, Any]:
    alert = {
        "ticker": "TEST",
        "sources": ["apewisdom", "barchart_uoa"],
        "source_count": 2,
        "total_score": 7,
        "signals": [
            {"source": "apewisdom", "detail": "Test rank spike", "signal": "TEST", "score": 3},
            {"source": "barchart_uoa", "detail": "Test UOA spike", "signal": "TEST", "score": 4},
        ],
        "detected_at": iso_now(),
        "alert_type": "CONFLUENCE",
        "vetted": False,
        "action": "WATCH - pipeline will score tonight",
        "test": True,
    }
    feed = load_feed()
    feed["alerts"] = [alert] + feed.get("alerts", [])
    feed["last_cycle"] = feed.get("last_cycle") or iso_now()
    feed["stats"] = {**feed.get("stats", {}), "test_alert_written": True}
    save_feed(feed)
    result = send_telegram(format_telegram_alert(alert)) if send else {"sent": False, "reason": "not requested"}
    print(json.dumps({"test_alert": alert, "telegram": result}, indent=2))
    return alert


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one discovery cycle then exit")
    parser.add_argument("--force", action="store_true", help="Run even outside market hours")
    parser.add_argument("--no-telegram", action="store_true", help="Do not send Telegram alerts")
    parser.add_argument("--update-baseline", action="store_true", help="Refresh baseline and exit")
    parser.add_argument("--test-alert", action="store_true", help="Write a dashboard test alert and exit")
    parser.add_argument("--send-test-telegram", action="store_true", help="Also send the test alert to Telegram")
    args = parser.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ensure_artifacts()

    if args.test_alert:
        write_test_alert(send=args.send_test_telegram)
        return
    if args.update_baseline:
        update_baseline()
        return

    print("Continuous Discovery Engine starting...")
    if not BASELINE_PATH.exists() or read_json(BASELINE_PATH, {}).get("date") != today():
        update_baseline()

    run_discovery_cycle(force=args.force, send_alerts=not args.no_telegram)
    if args.once:
        return

    schedule.every().day.at("13:30").do(update_baseline)
    schedule.every(15).minutes.do(run_discovery_cycle)
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
