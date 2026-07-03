#!/usr/bin/env python3
"""Scrape ImpliedOptions flow and ticker summaries for System 2.

The site does not expose a documented API. This collector deliberately uses
the rendered pages and stable visible labels/table headers instead of CSS
class names. Missing or inaccessible data is represented by null values and
never blocks the paper pipeline.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path("/root/system2-core")
UNIVERSE_PATH = ROOT / "universe.json"
DEFAULT_SUMMARY_INPUT = ROOT / "stage2_surgical_strike_top40.json"
OUTPUT_PATH = ROOT / "options_flow.json"
METADATA_PATH = ROOT / "options_flow.metadata.json"
LOG_PATH = ROOT / "logs" / "implied_options_scraper.log"
SESSION_PATH = ROOT / ".impliedoptions-session.json"

BASE_URL = "https://impliedoptions.com"
MIN_PREMIUM = 25_000.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    line = f"[{utc_now()}] {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def symbol_of(value: Any) -> str:
    if isinstance(value, str):
        raw = value
    elif isinstance(value, dict):
        raw = value.get("symbol") or value.get("ticker") or ""
    else:
        raw = ""
    symbol = str(raw).strip().upper()
    return symbol if re.fullmatch(r"[A-Z]{1,5}", symbol) else ""


def load_symbols(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("candidates") or raw.get("tickers") or raw.get("symbols") or []
    symbols = {symbol_of(row) for row in raw if symbol_of(row)}
    return sorted(symbols)


def parse_number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text in {"-", "—", "--", "N/A"}:
        return None
    multiplier = 1.0
    if text.upper().endswith("K"):
        multiplier, text = 1_000.0, text[:-1]
    elif text.upper().endswith("M"):
        multiplier, text = 1_000_000.0, text[:-1]
    elif text.upper().endswith("B"):
        multiplier, text = 1_000_000_000.0, text[:-1]
    cleaned = re.sub(r"[^0-9.+-]", "", text)
    try:
        return float(cleaned) * multiplier
    except (TypeError, ValueError):
        return None


def metric_after_label(text: str, label: str) -> float | None:
    match = re.search(
        rf"(?:^|\n){re.escape(label)}\s*\n\s*([$]?[0-9][0-9,]*(?:\.[0-9]+)?%?)",
        text,
        flags=re.IGNORECASE,
    )
    return parse_number(match.group(1)) if match else None


def dismiss_cookies(page: Page) -> None:
    button = page.get_by_role("button", name="Deny")
    try:
        if button.count() == 1 and button.is_visible():
            button.click(timeout=3_000)
    except Exception:
        pass


def maybe_login(context: BrowserContext, page: Page) -> bool:
    username = os.getenv("IMPLIEDOPTIONS_USER", "").strip()
    password = os.getenv("IMPLIEDOPTIONS_PASS", "").strip()
    if not username or not password:
        log("Account credentials absent; continuing with publicly visible data.")
        return False

    page.goto(f"{BASE_URL}/signup", wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(4_000)
    dismiss_cookies(page)
    sign_in = page.locator("[role=tab]").filter(has_text="Sign In")
    if sign_in.count() == 1:
        sign_in.click(timeout=5_000)
        page.wait_for_timeout(500)

    email = page.locator("input#signup-email:visible")
    password_field = page.locator("input#signup-password:visible")
    submit = page.locator("button[type=submit]:visible").filter(has_text="Sign In")
    if email.count() != 1 or password_field.count() != 1 or submit.count() != 1:
        raise RuntimeError("ImpliedOptions sign-in controls were not found")

    email.fill(username)
    password_field.fill(password)
    submit.click(timeout=10_000)
    try:
        page.wait_for_url(re.compile(r"^https://impliedoptions\.com/(?!signup)"), timeout=15_000)
    except PlaywrightTimeoutError:
        page.wait_for_timeout(2_000)
    if "/signup" in page.url:
        raise RuntimeError("ImpliedOptions login did not leave the sign-in page")

    context.storage_state(path=str(SESSION_PATH))
    SESSION_PATH.chmod(0o600)
    log(f"Authenticated ImpliedOptions session established at {page.url}")
    return True


def parse_expiry(value: str, today: date) -> date | None:
    text = value.strip()
    for pattern in ("%b %d, %Y", "%b %d %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    try:
        parsed = datetime.strptime(text, "%b %d").date().replace(year=today.year)
        if parsed < today.replace(month=1, day=1):
            parsed = parsed.replace(year=today.year + 1)
        return parsed
    except ValueError:
        return None


def moneyness(option_type: str, strike: float | None, underlying: float | None) -> str | None:
    if not strike or not underlying:
        return None
    distance = abs(strike - underlying) / underlying
    if distance <= 0.01:
        return "ATM"
    if option_type == "C":
        return "ITM" if strike < underlying else "OTM"
    if option_type == "P":
        return "ITM" if strike > underlying else "OTM"
    return None


def scrape_flow(page: Page, universe: set[str]) -> tuple[dict[str, list[dict]], dict[str, int]]:
    page.goto(f"{BASE_URL}/flow", wait_until="domcontentloaded", timeout=60_000)
    dismiss_cookies(page)
    page.wait_for_selector("table tbody tr", timeout=30_000)
    page.wait_for_timeout(2_000)

    raw_rows = page.locator("table").evaluate(
        """table => {
          const headers = Array.from(table.querySelectorAll('thead th'))
            .map(x => x.innerText.trim());
          const rows = Array.from(table.querySelectorAll('tbody tr')).map(row =>
            Array.from(row.querySelectorAll('td')).map(cell => cell.innerText.trim())
          );
          return {headers, rows};
        }"""
    )
    headers = [str(value).strip().lower() for value in raw_rows.get("headers", [])]
    index = {name: position for position, name in enumerate(headers)}
    required = ["time", "ticker", "expiry", "strike", "c/p", "side", "size", "oi", "premium", "type"]
    missing = [name for name in required if name not in index]
    if missing:
        raise RuntimeError(f"Flow table headers changed; missing {missing}; saw {headers}")

    result: dict[str, list[dict]] = {}
    repeat_counts: dict[str, int] = {}
    today = datetime.now(timezone.utc).date()
    for cells in raw_rows.get("rows", []):
        if len(cells) < len(headers):
            continue
        timestamp = cells[index["time"]].strip()
        symbol_match = re.match(r"([A-Z]{1,5})", cells[index["ticker"]].upper())
        symbol = symbol_match.group(1) if symbol_match else ""
        premium = parse_number(cells[index["premium"]])
        if not re.fullmatch(r"\d{1,2}:\d{2}", timestamp):
            continue
        if symbol not in universe:
            continue
        repeat_counts[symbol] = repeat_counts.get(symbol, 0) + 1
        if premium is None or premium <= MIN_PREMIUM:
            continue
        raw_type = cells[index["type"]].strip().lower()
        expiry = parse_expiry(cells[index["expiry"]], today)
        days_to_expiry = (expiry - today).days if expiry else None
        row_volume = parse_number(cells[index["size"]])
        row_oi = parse_number(cells[index["oi"]])
        result.setdefault(symbol, []).append(
            {
                "symbol": symbol,
                "strike": parse_number(cells[index["strike"]]),
                "expiry_date": expiry.isoformat() if expiry else None,
                "days_to_expiry": days_to_expiry,
                "dte_flag": "0DTE" if days_to_expiry is not None and days_to_expiry <= 1 else None,
                "type": cells[index["c/p"]].strip().upper() or None,
                "side": {"BUY": "A", "SELL": "B"}.get(cells[index["side"]].strip().upper()),
                "side_raw": cells[index["side"]].strip().upper() or None,
                "order_type": {"s": "sweep", "b": "block", "c": "split"}.get(raw_type, raw_type or None),
                "premium": premium,
                "volume": row_volume,
                "open_interest": row_oi,
                "vol_oi_ratio": round(row_volume / row_oi, 4) if row_volume is not None and row_oi else None,
                "timestamp": timestamp,
            }
        )
    return result, repeat_counts


def page_text(page: Page, url: str) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    dismiss_cookies(page)
    page.wait_for_timeout(1_000)
    return page.locator("body").inner_text(timeout=10_000)


def scrape_summary(page: Page, symbol: str) -> dict[str, Any]:
    iv_text = page_text(page, f"{BASE_URL}/insights/iv-rank/{symbol.lower()}")
    if "Page Not Found" in iv_text:
        return {}
    daily_text = page_text(page, f"{BASE_URL}/insights/daily-data/{symbol.lower()}")

    iv_rank = metric_after_label(iv_text, "Current IV Rank")
    atm_iv = metric_after_label(iv_text, "Current IV")
    iv_percentile = metric_after_label(iv_text, "IV Percentile")
    call_volume = metric_after_label(daily_text, "Call Volume")
    put_volume = metric_after_label(daily_text, "Put Volume")
    total_volume = metric_after_label(daily_text, "Total Volume")
    put_call_ratio = metric_after_label(daily_text, "P/C Ratio")

    if all(value is None for value in (iv_rank, atm_iv, call_volume, put_volume)):
        return {}
    return {
        "iv_rank": iv_rank,
        "iv_percentile": iv_percentile,
        "atm_iv": atm_iv,
        "total_call_volume": call_volume,
        "total_put_volume": put_volume,
        "total_volume": total_volume,
        "total_call_oi": None,
        "total_put_oi": None,
        "put_call_vol_ratio": put_call_ratio,
        "underlying_price": metric_after_label(daily_text, "Close Price"),
    }


def derived_flow(rows: list[dict], repeat_flow_count: int, underlying: float | None) -> dict[str, Any]:
    for row in rows:
        row["moneyness"] = moneyness(row.get("type"), row.get("strike"), underlying)
    directional = [row for row in rows if row.get("dte_flag") != "0DTE"]
    call_rows = [row for row in rows if row.get("type") == "C"]
    call_volume = sum(float(row.get("volume") or 0) for row in call_rows)
    call_oi = sum(float(row.get("open_interest") or 0) for row in call_rows)
    return {
        "call_vol_oi_ratio": round(call_volume / call_oi, 4) if call_oi > 0 else None,
        "call_vol_oi_ratio_basis": "today_uoa_rows",
        "uoa_rows_today": repeat_flow_count,
        "uoa_qualifying_rows": sum(row.get("dte_flag") != "0DTE" for row in rows),
        "repeat_flow_count": repeat_flow_count,
        "ask_side_sweep_count": sum(
            row.get("side") == "A" and row.get("order_type") == "sweep" for row in directional
        ),
        "bullish_premium_total": round(
            sum(float(row.get("premium") or 0) for row in directional if row.get("side") == "A" and row.get("type") == "C"),
            2,
        ),
        "bearish_premium_total": round(
            sum(float(row.get("premium") or 0) for row in directional if row.get("side") == "A" and row.get("type") == "P"),
            2,
        ),
        "0dte_rows_excluded": sum(row.get("dte_flag") == "0DTE" for row in rows),
    }


def fallback(reason: str, started: float, errors: list[str]) -> None:
    errors.append(reason)
    atomic_json(OUTPUT_PATH, {})
    atomic_json(
        METADATA_PATH,
        {
            "created_at": utc_now(),
            "options_source": "unavailable",
            "status": "FAILED_CLEAN",
            "symbols_written": 0,
            "errors": errors,
            "runtime_seconds": round(time.time() - started, 2),
            "paper_only": True,
        },
    )
    log(f"Clean fail-open: {reason}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default=str(UNIVERSE_PATH))
    parser.add_argument("--summary-input", default=str(DEFAULT_SUMMARY_INPUT))
    parser.add_argument("--symbols", help="Comma-separated summary symbols for testing")
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--flow-only", action="store_true")
    parser.add_argument("--summaries-only", action="store_true")
    args = parser.parse_args()

    started = time.time()
    errors: list[str] = []
    # load_dotenv(ROOT / ".env") # env already loaded by runner

    universe_symbols = load_symbols(Path(args.universe))
    if not universe_symbols:
        fallback("Universe is empty or unreadable", started, errors)
        return 0

    if args.symbols:
        summary_symbols = sorted({symbol_of(value) for value in args.symbols.split(",") if symbol_of(value)})
    else:
        summary_symbols = load_symbols(Path(args.summary_input))
    if args.max_symbols > 0:
        summary_symbols = summary_symbols[: args.max_symbols]

    existing: dict[str, dict] = {}
    if args.summaries_only and OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("tickers"), dict):
                existing = existing["tickers"]
        except Exception:
            existing = {}

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context_args: dict[str, Any] = {
                "viewport": {"width": 1440, "height": 1000},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            }
            if SESSION_PATH.exists():
                context_args["storage_state"] = str(SESSION_PATH)
            context = browser.new_context(**context_args)
            page = context.new_page()
            page.set_default_timeout(20_000)

            authenticated = False
            try:
                authenticated = maybe_login(context, page)
            except Exception as exc:
                errors.append(f"login: {exc}")
                log(f"Login unavailable; continuing with public data: {exc}")

            flow_by_symbol: dict[str, list[dict]] = {}
            repeat_counts: dict[str, int] = {}
            if not args.summaries_only:
                flow_by_symbol, repeat_counts = scrape_flow(page, set(universe_symbols))
                log(f"Flow rows retained for {len(flow_by_symbol)} universe symbols.")

            results = existing
            for symbol, rows in flow_by_symbol.items():
                results.setdefault(symbol, {}).update({"uoa": rows})

            summaries_written = 0
            if not args.flow_only:
                log(f"Collecting summaries for {len(summary_symbols)} symbols.")
                for position, symbol in enumerate(summary_symbols, 1):
                    try:
                        summary = scrape_summary(page, symbol)
                        if summary:
                            current = results.setdefault(symbol, {})
                            current.setdefault("uoa", flow_by_symbol.get(symbol, []))
                            current.update(summary)
                            current.update(
                                derived_flow(
                                    current["uoa"],
                                    repeat_counts.get(symbol, current.get("repeat_flow_count", 0)),
                                    current.get("underlying_price"),
                                )
                            )
                            summaries_written += 1
                    except Exception as exc:
                        errors.append(f"{symbol}: {exc}")
                    if position % 10 == 0:
                        log(f"Summary progress {position}/{len(summary_symbols)}")

            for symbol, current in results.items():
                current.update(
                    derived_flow(
                        current.get("uoa") or [],
                        repeat_counts.get(symbol, current.get("repeat_flow_count", 0)),
                        current.get("underlying_price"),
                    )
                )

            context.close()
            browser.close()

        wrapped_output = {
            "run_date": datetime.now(timezone.utc).date().isoformat(),
            "source": "impliedoptions_authenticated" if authenticated else "impliedoptions_unauthenticated",
            "tickers": results,
        }
        atomic_json(OUTPUT_PATH, wrapped_output)
        atomic_json(
            METADATA_PATH,
            {
                "created_at": utc_now(),
                "options_source": "impliedoptions",
                "status": "OK" if results else "NO_MATCHES",
                "authenticated": authenticated,
                "universe_count": len(universe_symbols),
                "summary_symbols_requested": len(summary_symbols),
                "summaries_written": summaries_written,
                "flow_symbols_written": len(flow_by_symbol),
                "symbols_written": len(results),
                "minimum_premium": MIN_PREMIUM,
                "errors": errors[:50],
                "runtime_seconds": round(time.time() - started, 2),
                "paper_only": True,
            },
        )
        log(f"Completed with {len(results)} symbols in {time.time() - started:.1f}s.")
        return 0
    except (PlaywrightTimeoutError, Exception) as exc:
        fallback(str(exc), started, errors)
        return 0


if __name__ == "__main__":
    sys.exit(main())
