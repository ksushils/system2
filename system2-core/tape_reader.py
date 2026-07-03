#!/usr/bin/env python3
"""
System 2 — Alpaca websocket tape reader.

Runs during market hours (09:30–16:00 ET Mon-Fri) via PM2.
Subscribes to trade + quote streams for today's actionable finalists.
Maintains rolling per-ticker tape state and writes to disk every 30s.

Paper trading websocket endpoint:
  wss://stream.data.alpaca.markets/v2/iex

PM2:
  pm2 start tape_reader.py --name tape_reader --interpreter python3
Crontab:
  25 13 * * 1-5  pm2 start tape_reader
  05 20 * * 1-5  pm2 stop tape_reader
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

TAPE_STATE_PATH = DATA_DIR / "tape_state.json"
TAPE_ALERTS_PATH = DATA_DIR / "tape_alerts.json"
SYSTEM2_ROOT = Path(os.environ.get("SYSTEM2_CORE_DIR", str(ROOT)))

# Alpaca credentials (paper or live data key)
ALPACA_KEY = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID") or ""
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY") or ""

WS_URL = "wss://stream.data.alpaca.markets/v2/iex"
MAX_RECONNECT_ATTEMPTS = 3
RECONNECT_DELAY_SECONDS = 5
STATE_WRITE_INTERVAL_SECONDS = 30
LARGE_PRINT_THRESHOLD = 10_000
TAPE_SIGNAL_MIN_BULLISH_MINUTES = 3
TAPE_ALERT_COOLDOWN_SECONDS = 60 * 60  # 60 minutes per ticker

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID") or ""

# ═══════════════════════════════════════════════════════════════════
# MARKET HOURS UTILITIES
# ═══════════════════════════════════════════════════════════════════

NY_TZ_OFFSET = -4 if datetime.now(timezone.utc).astimezone().strftime("%Z") in ("EDT",) else -5


def et_now() -> datetime:
    """Return current time in America/New_York (naive for simplicity)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        # Fallback: assume UTC-4 (EDT) — most market hours are in EDT
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(hours=4)).replace(tzinfo=None)  # caller should treat as ET context


def is_market_open() -> bool:
    """Return True if between 09:30 and 16:00 ET, Mon-Fri."""
    now = et_now()
    weekday = now.weekday()
    if weekday >= 5:  # Sat/Sun
        return False
    minutes = now.hour * 60 + now.minute
    return 570 <= minutes < 960  # 09:30 = 570, 16:00 = 960


# ═══════════════════════════════════════════════════════════════════
# FINALISTS LOADER
# ═══════════════════════════════════════════════════════════════════

def load_todays_finalists() -> list[dict]:
    """Read actionable tickers from local JSON artifacts."""
    candidates: list[dict] = []
    sources = [
        SYSTEM2_ROOT / "fund.json",
        SYSTEM2_ROOT / "stage7_clustered_survivors.json",
        SYSTEM2_ROOT / "stage5_news_safe_finalists.json",
    ]
    for src in sources:
        if not src.exists():
            continue
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "tickers" in data:
                candidates = data["tickers"]
                break
            if isinstance(data, list):
                candidates = data
                break
        except Exception:
            continue

    today = datetime.now(timezone.utc).date().isoformat()
    actionable = []
    for row in candidates:
        date = row.get("date") or row.get("catalyst_date") or ""
        if date and not str(date).startswith(today):
            continue
        status = str(row.get("paper_status") or "").upper()
        adverse = row.get("pre_market_gap_adverse")
        if status == "OPEN" or adverse is False:
            actionable.append(row)

    # Deduplicate by symbol, cap at 20
    seen = set()
    deduped = []
    for row in actionable:
        sym = str(row.get("symbol") or row.get("ticker") or "").upper()
        if sym and sym not in seen:
            seen.add(sym)
            deduped.append(row)
        if len(deduped) >= 20:
            break
    return deduped


# ═══════════════════════════════════════════════════════════════════
# PER-TICKER ROLLING STATE
# ═══════════════════════════════════════════════════════════════════

class TickerState:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.last_price: float | None = None
        self.last_size: int = 0
        self.last_bid: float | None = None
        self.last_ask: float | None = None
        self.trades: deque[dict] = deque()  # rolling 60s
        self.cumulative_delta = 0
        self.cumulative_volume = 0
        self.cumulative_pv = 0.0  # price * volume for VWAP
        self.vwap: float | None = None
        self.large_prints_today = 0
        self.last_large_print: dict | None = None
        self.buy_volume_60s = 0
        self.sell_volume_60s = 0
        self.aggressor_ratio = 0.5
        self.delta_direction = "NEUTRAL"
        self.price_vs_vwap = "AT"
        self.tape_signal = "NEUTRAL"
        self.tape_signal_since: float | None = None
        self.delta_history: deque[tuple[float, float]] = deque()  # (timestamp, delta)
        self.updated_at: str = ""

    def on_quote(self, bid: float, ask: float) -> None:
        self.last_bid = bid
        self.last_ask = ask

    def on_trade(self, price: float, size: int, ts: float) -> None:
        self.last_price = price
        self.last_size = size
        self.cumulative_volume += size
        self.cumulative_pv += price * size
        self.vwap = self.cumulative_pv / self.cumulative_volume if self.cumulative_volume > 0 else None

        # Classify side
        side = "NEUTRAL"
        if self.last_ask is not None and price >= self.last_ask:
            side = "BUY"
            self.cumulative_delta += size
        elif self.last_bid is not None and price <= self.last_bid:
            side = "SELL"
            self.cumulative_delta -= size
        else:
            # Neutral — do not affect delta
            pass

        # Large print detection
        if size >= LARGE_PRINT_THRESHOLD:
            self.large_prints_today += 1
            self.last_large_print = {
                "price": round(price, 2),
                "size": size,
                "side": side,
                "time": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S"),
            }

        # Rolling window
        self.trades.append({"price": price, "size": size, "side": side, "ts": ts})
        self._trim_window(ts)
        self._recompute(ts)

    def _trim_window(self, now_ts: float) -> None:
        cutoff = now_ts - 60
        while self.trades and self.trades[0]["ts"] < cutoff:
            self.trades.popleft()

    def _recompute(self, now_ts: float) -> None:
        buy_vol = sum(t["size"] for t in self.trades if t["side"] == "BUY")
        sell_vol = sum(t["size"] for t in self.trades if t["side"] == "SELL")
        neutral_vol = sum(t["size"] for t in self.trades if t["side"] == "NEUTRAL")
        total = buy_vol + sell_vol + neutral_vol
        self.buy_volume_60s = buy_vol
        self.sell_volume_60s = sell_vol
        self.aggressor_ratio = buy_vol / total if total > 0 else 0.5

        self.delta_direction = (
            "BULLISH" if self.cumulative_delta > 0 else
            "BEARISH" if self.cumulative_delta < 0 else
            "NEUTRAL"
        )

        if self.vwap is not None and self.last_price is not None:
            diff = self.last_price - self.vwap
            self.price_vs_vwap = "ABOVE" if diff > 0.001 else "BELOW" if diff < -0.001 else "AT"
        else:
            self.price_vs_vwap = "AT"

        # Delta history for 5-min trend
        self.delta_history.append((now_ts, self.cumulative_delta))
        while self.delta_history and self.delta_history[0][0] < now_ts - 300:
            self.delta_history.popleft()

        # Tape signal
        prev_signal = self.tape_signal
        delta_trending_negative = False
        if len(self.delta_history) >= 2:
            oldest = self.delta_history[0][1]
            delta_trending_negative = self.cumulative_delta < oldest

        if self.aggressor_ratio > 0.65 and self.cumulative_delta > 0 and self.price_vs_vwap == "ABOVE":
            new_signal = "BULLISH"
        elif self.aggressor_ratio < 0.35 or delta_trending_negative:
            new_signal = "BEARISH"
        else:
            new_signal = "NEUTRAL"

        if new_signal == prev_signal:
            pass  # keep existing since timestamp
        else:
            self.tape_signal = new_signal
            self.tape_signal_since = now_ts

        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "last_price": round(self.last_price, 2) if self.last_price is not None else None,
            "last_size": self.last_size,
            "trades_last_60s": len(self.trades),
            "volume_last_60s": sum(t["size"] for t in self.trades),
            "buy_volume_60s": self.buy_volume_60s,
            "sell_volume_60s": self.sell_volume_60s,
            "cumulative_delta": self.cumulative_delta,
            "delta_direction": self.delta_direction,
            "vwap": round(self.vwap, 4) if self.vwap is not None else None,
            "price_vs_vwap": self.price_vs_vwap,
            "large_prints_today": self.large_prints_today,
            "last_large_print": self.last_large_print,
            "aggressor_ratio": round(self.aggressor_ratio, 2),
            "tape_signal": self.tape_signal,
            "tape_signal_since": self.tape_signal_since,
            "updated_at": self.updated_at,
        }


# ═══════════════════════════════════════════════════════════════════
# TELEGRAM ALERTS (Part 4)
# ═══════════════════════════════════════════════════════════════════

def send_telegram(text: str) -> dict:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"sent": False, "reason": "missing telegram credentials"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"sent": True, "status": resp.status}
    except urllib.error.HTTPError as e:
        return {"sent": False, "reason": f"HTTP {e.code}"}
    except Exception as e:
        return {"sent": False, "reason": str(e)}


# ═══════════════════════════════════════════════════════════════════
# WEBSOCKET CLIENT
# ═══════════════════════════════════════════════════════════════════

try:
    import websocket
except ImportError as _exc:
    print("ERROR: websocket-client is required. Install: pip install websocket-client")
    sys.exit(1)


class TapeReader:
    def __init__(self, tickers: list[str]) -> None:
        self.tickers = [t.upper() for t in tickers]
        self.states: dict[str, TickerState] = {t: TickerState(t) for t in self.tickers}
        self.ws: websocket.WebSocketApp | None = None
        self.reconnect_count = 0
        self.running = False
        self.write_thread: threading.Thread | None = None
        self.alert_thread: threading.Thread | None = None
        self.last_alert_time: dict[str, float] = {}
        self.finalist_lookup: dict[str, dict] = {}

    def load_finalist_meta(self) -> None:
        rows = load_todays_finalists()
        self.finalist_lookup = {}
        for r in rows:
            sym = str(r.get("symbol") or r.get("ticker") or "").upper()
            if sym:
                self.finalist_lookup[sym] = r

    def on_open(self, ws) -> None:
        print(f"[{datetime.now(timezone.utc).isoformat()}] Websocket opened")
        self.reconnect_count = 0
        # Authenticate
        ws.send(json.dumps({"action": "auth", "key": ALPACA_KEY, "secret": ALPACA_SECRET}))

    def on_message(self, ws, message: str) -> None:
        try:
            for msg in json.loads(message):
                self._handle_msg(msg)
        except Exception as e:
            print(f"[{datetime.now(timezone.utc).isoformat()}] Message parse error: {e}")

    def _handle_msg(self, msg: dict) -> None:
        msg_type = msg.get("T")
        if msg_type == "success":
            print(f"[{datetime.now(timezone.utc).isoformat()}] Auth success: {msg.get('msg')}")
            # Subscribe after auth success
            sub = {"action": "subscribe", "trades": self.tickers, "quotes": self.tickers}
            self.ws.send(json.dumps(sub))
            print(f"[{datetime.now(timezone.utc).isoformat()}] Subscribed to {len(self.tickers)} tickers")
        elif msg_type == "subscription":
            print(f"[{datetime.now(timezone.utc).isoformat()}] Subscription ack: trades={msg.get('trades')}, quotes={msg.get('quotes')}")
        elif msg_type == "error":
            print(f"[{datetime.now(timezone.utc).isoformat()}] Stream error: {msg}")
        elif msg_type == "t":
            sym = msg.get("S", "").upper()
            if sym in self.states:
                ts = self._parse_ts(msg.get("t", ""))
                self.states[sym].on_trade(float(msg["p"]), int(msg["s"]), ts)
        elif msg_type == "q":
            sym = msg.get("S", "").upper()
            if sym in self.states:
                self.states[sym].on_quote(float(msg.get("bp", 0)), float(msg.get("ap", 0)))

    def _parse_ts(self, ts_str: str) -> float:
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            return time.time()

    def on_error(self, ws, error) -> None:
        print(f"[{datetime.now(timezone.utc).isoformat()}] Websocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg) -> None:
        print(f"[{datetime.now(timezone.utc).isoformat()}] Websocket closed: {close_status_code} {close_msg}")
        if self.running and self.reconnect_count < MAX_RECONNECT_ATTEMPTS:
            self.reconnect_count += 1
            print(f"[{datetime.now(timezone.utc).isoformat()}] Reconnecting in {RECONNECT_DELAY_SECONDS}s (attempt {self.reconnect_count}/{MAX_RECONNECT_ATTEMPTS})...")
            time.sleep(RECONNECT_DELAY_SECONDS)
            self._connect()
        else:
            print(f"[{datetime.now(timezone.utc).isoformat()}] Max reconnects reached or stopped. Exiting.")
            self.running = False

    def _connect(self) -> None:
        self.ws = websocket.WebSocketApp(
            WS_URL,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        self.ws.run_forever()

    def _write_state_loop(self) -> None:
        while self.running:
            time.sleep(STATE_WRITE_INTERVAL_SECONDS)
            self._write_state()

    def _write_state(self) -> None:
        payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tickers": self.tickers,
            "states": {sym: st.to_dict() for sym, st in self.states.items()},
        }
        try:
            TAPE_STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[{datetime.now(timezone.utc).isoformat()}] State write error: {e}")

    def _alert_loop(self) -> None:
        """Check every 30s for BULLISH tape signals that warrant Telegram alerts."""
        while self.running:
            time.sleep(30)
            self._check_alerts()

    def _check_alerts(self) -> None:
        now = time.time()
        for sym, st in self.states.items():
            if st.tape_signal != "BULLISH":
                continue
            if st.tape_signal_since is None:
                continue
            bullish_duration = now - st.tape_signal_since
            if bullish_duration < TAPE_SIGNAL_MIN_BULLISH_MINUTES * 60:
                continue

            # Cooldown check
            last_alert = self.last_alert_time.get(sym, 0)
            if now - last_alert < TAPE_ALERT_COOLDOWN_SECONDS:
                continue

            meta = self.finalist_lookup.get(sym)
            if not meta:
                continue
            if meta.get("pre_market_gap_adverse") is True:
                continue

            # Price in entry zone?
            entry_zone = meta.get("entryZone") or []
            if not isinstance(entry_zone, list) or len(entry_zone) < 2:
                continue
            try:
                low, high = float(entry_zone[0]), float(entry_zone[1])
            except (TypeError, ValueError):
                continue
            price = st.last_price
            if price is None:
                continue
            if not (low <= price <= high):
                continue

            # All conditions met — send alert
            self.last_alert_time[sym] = now
            setup_score = meta.get("setup_score") or meta.get("setupQualityScore") or "-"
            confluence = meta.get("confluence_score") or "-"
            stop = meta.get("stop") or meta.get("stopLoss") or "-"
            tp1 = meta.get("target") or meta.get("tp1") or "-"
            text = (
                f"🟢 TAPE ALERT: {sym}\n"
                f"Price: ${price:.2f} (IN ZONE)\n"
                f"Tape: BULLISH — {int(st.aggressor_ratio * 100)}% buy aggression\n"
                f"Delta: +{st.cumulative_delta:,} shares net buying\n"
                f"{st.large_prints_today} large blocks printed today\n"
                f"Setup: {setup_score} | Conf: {confluence}\n"
                f"Entry zone: ${low:.2f}–${high:.2f}\n"
                f"Stop: {stop} | TP1: {tp1}"
            )
            result = send_telegram(text)
            print(f"[{datetime.now(timezone.utc).isoformat()}] Telegram alert {sym}: {result}")
            self._append_alert_log(sym, text, result)

    def _append_alert_log(self, sym: str, text: str, result: dict) -> None:
        try:
            alerts = json.loads(TAPE_ALERTS_PATH.read_text(encoding="utf-8")) if TAPE_ALERTS_PATH.exists() else []
        except Exception:
            alerts = []
        alerts.append({
            "symbol": sym,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "text_preview": text[:200],
            "result": result,
        })
        try:
            TAPE_ALERTS_PATH.write_text(json.dumps(alerts[-500:], indent=2), encoding="utf-8")
        except Exception:
            pass

    def run(self) -> None:
        if not is_market_open():
            print("Market is closed. Exiting cleanly.")
            sys.exit(0)

        if not ALPACA_KEY or not ALPACA_SECRET:
            print("ALPACA_API_KEY / ALPACA_SECRET_KEY not set. Exiting.")
            sys.exit(1)

        self.load_finalist_meta()
        if not self.tickers:
            print("No tickers to watch. Exiting.")
            sys.exit(0)

        print(f"[{datetime.now(timezone.utc).isoformat()}] TapeReader starting for {len(self.tickers)} tickers: {', '.join(self.tickers)}")
        self.running = True

        self.write_thread = threading.Thread(target=self._write_state_loop, daemon=True)
        self.write_thread.start()

        self.alert_thread = threading.Thread(target=self._alert_loop, daemon=True)
        self.alert_thread.start()

        self._connect()

        # If we get here, websocket closed and we either stopped or max reconnects reached
        self.running = False
        self._write_state()
        print(f"[{datetime.now(timezone.utc).isoformat()}] TapeReader exited.")


def main() -> None:
    finalists = load_todays_finalists()
    symbols = [str(r.get("symbol") or r.get("ticker") or "").upper() for r in finalists]
    symbols = [s for s in symbols if s]
    reader = TapeReader(symbols)
    reader.run()


if __name__ == "__main__":
    main()
