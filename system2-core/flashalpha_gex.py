#!/usr/bin/env python3
"""FlashAlpha GEX fetcher for System 2.

Fetches GEX (gamma exposure) data for SPY and QQQ from the FlashAlpha lab API,
computes a consolidated market gamma regime, and persists the result to JSON.
Designed to stay within the free tier (5 requests/day) by using 2 calls for the
nightly broad-market scan and reserving the remaining 3 calls for individual
finalist symbols when needed.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parent


def load_dotenv(path=None):
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip(chr(34) + chr(39))
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ENDPOINT = "https://lab.flashalpha.com/v1/exposure/gex"
OUTPUT_DIR = Path("/root/system2-core/data")
OUTPUT_PATH = OUTPUT_DIR / "gex_regime.json"
DAILY_FREE_TIER_BUDGET = 5
MAIN_RUN_SYMBOLS = ["SPY", "QQQ"]
RESERVED_FINALIST_SLOTS = 3
TIMEOUT_SECONDS = 15.0
MAX_RETRIES = 3

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def fetch_symbol_gex(symbol: str, api_key: str) -> Optional[Dict[str, Any]]:
    """Fetch GEX data for a single symbol from the FlashAlpha API.

    Args:
        symbol: The ticker symbol to query (e.g. "SPY", "QQQ").
        api_key: The FlashAlpha API key.

    Returns:
        A normalized dictionary of GEX fields, or None if the request fails
        after all retries.
    """
    headers = {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{ENDPOINT}/{symbol.upper()}"

    session = requests.Session()
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("Fetching GEX for %s (attempt %d/%d)", symbol, attempt, MAX_RETRIES)
            response = session.get(
                url,
                headers=headers,
                timeout=TIMEOUT_SECONDS,
            )

            if response.status_code == 200:
                payload = response.json()
                data = payload.get("data", payload)
                strikes = data.get("strikes") or []

                normalized = {
                    "net_gex": _coerce_float(data.get("net_gex") or data.get("netGex")),
                    "gamma_flip": _coerce_float(data.get("gamma_flip") or data.get("gammaFlip")),
                    "put_wall": _coerce_float(
                        data.get("put_wall") or data.get("putWall") or _wall_from_strikes(strikes, "put")
                    ),
                    "call_wall": _coerce_float(
                        data.get("call_wall") or data.get("callWall") or _wall_from_strikes(strikes, "call")
                    ),
                    "volatility_regime": _coerce_regime(
                        data.get("volatility_regime")
                        or data.get("volatilityRegime")
                        or data.get("net_gex_label")
                        or data.get("netGexLabel")
                    ),
                }
                logger.info("GEX fetched for %s: %s", symbol, normalized["volatility_regime"])
                return normalized

            logger.warning(
                "FlashAlpha returned HTTP %s for %s: %s",
                response.status_code,
                symbol,
                response.text[:200],
            )
            if response.status_code in {401, 403, 429}:
                logger.error("FlashAlpha returned non-retryable HTTP %s for %s", response.status_code, symbol)
                return None
            if attempt < MAX_RETRIES:
                backoff = 2 ** attempt
                logger.info("Backing off %ss before retry", backoff)
                time.sleep(backoff)

        except requests.exceptions.Timeout as exc:
            last_error = exc
            logger.warning("Timeout fetching %s on attempt %d: %s", symbol, attempt, exc)
            if attempt < MAX_RETRIES:
                backoff = 2 ** attempt
                logger.info("Backing off %ss before retry", backoff)
                time.sleep(backoff)

        except requests.exceptions.RequestException as exc:
            last_error = exc
            logger.error("Request error fetching %s: %s", symbol, exc)
            return None

    logger.error("All retries exhausted for %s (last error: %s)", symbol, last_error)
    return None


def _coerce_float(value: Any) -> float:
    """Safely coerce a value to float; return 0.0 on failure."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _wall_from_strikes(strikes: Any, side: str) -> float:
    """Derive call/put wall strike from per-strike GEX rows when absent."""
    if not isinstance(strikes, list):
        return 0.0

    exposure_key = "call_gex" if side == "call" else "put_gex"
    camel_key = "callGex" if side == "call" else "putGex"
    best_strike = 0.0
    best_value = 0.0

    for row in strikes:
        if not isinstance(row, dict):
            continue
        strike = _coerce_float(row.get("strike"))
        exposure = _coerce_float(row.get(exposure_key) or row.get(camel_key))
        magnitude = abs(exposure)
        if strike and magnitude > best_value:
            best_strike = strike
            best_value = magnitude

    return best_strike


def _coerce_regime(value: Any) -> str:
    """Normalize a volatility regime string; default to UNKNOWN."""
    if not value:
        return "UNKNOWN"
    regime = str(value).strip().upper()
    if regime in ("DAMPENING", "AMPLIFYING"):
        return regime
    if regime in ("POSITIVE", "POSITIVE_GEX", "LONG_GAMMA"):
        return "DAMPENING"
    if regime in ("NEGATIVE", "NEGATIVE_GEX", "SHORT_GAMMA"):
        return "AMPLIFYING"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Regime logic
# ---------------------------------------------------------------------------
def compute_market_gex_regime(
    spy: Optional[Dict[str, Any]],
    qqq: Optional[Dict[str, Any]],
) -> str:
    """Compute the consolidated market GEX regime from SPY and QQQ.

    Rules:
      - Both DAMPENING  -> DAMPENING
      - Either AMPLIFYING -> AMPLIFYING
      - Mixed (one DAMPENING, one AMPLIFYING) -> NEUTRAL
      - Any UNKNOWN -> UNKNOWN
    """
    if not spy or not qqq:
        return "UNKNOWN"

    r1 = spy.get("volatility_regime", "UNKNOWN")
    r2 = qqq.get("volatility_regime", "UNKNOWN")

    if r1 == "UNKNOWN" or r2 == "UNKNOWN":
        return "UNKNOWN"
    if r1 == "DAMPENING" and r2 == "DAMPENING":
        return "DAMPENING"
    if r1 == "AMPLIFYING" or r2 == "AMPLIFYING":
        return "AMPLIFYING"
    if r1 != r2:
        return "NEUTRAL"
    return r1


def derive_regime_from_vix() -> tuple[str, float]:
    """Derive a structural regime from VIX when FlashAlpha is unavailable."""
    try:
        fmp_key = os.getenv("FMP_API_KEY", "")
        data: Any = []
        for symbol in ("%5EVIX", "VIX"):
            url = (
                "https://financialmodelingprep.com"
                f"/stable/quote?symbol={symbol}&apikey={fmp_key}"
            )
            response = requests.get(url, timeout=10)
            data = response.json()
            if isinstance(data, list) and data:
                break
        if not isinstance(data, list) or not data:
            return "UNKNOWN", 20.0

        vix = float(data[0].get("price", 20))

        if vix > 30:
            return "NEGATIVE_GAMMA", vix
        if vix > 22:
            return "BELOW_FLIP", vix
        if vix > 18:
            return "NEUTRAL", vix
        if vix < 15:
            return "ABOVE_FLIP", vix
        return "NEUTRAL", vix
    except Exception as exc:
        logger.warning("VIX fallback failed: %s", exc)
        return "UNKNOWN", 20.0


def _gex_values_are_empty(*records: Optional[Dict[str, Any]]) -> bool:
    """Return true when every GEX numeric level is zero/missing."""
    numeric_fields = ("net_gex", "gamma_flip", "put_wall", "call_wall")
    for record in records:
        if not record:
            continue
        if any(_coerce_float(record.get(field)) != 0.0 for field in numeric_fields):
            return False
    return True


def _vix_fallback_record(today: str, note: str) -> Dict[str, Any]:
    regime, vix_value = derive_regime_from_vix()
    symbol_record = {
        "net_gex": 0.0,
        "gamma_flip": 0.0,
        "put_wall": 0.0,
        "call_wall": 0.0,
        "volatility_regime": regime,
        "data_source": "vix_fallback",
    }
    return {
        "date": today,
        "spy": dict(symbol_record),
        "qqq": dict(symbol_record),
        "SPY": dict(symbol_record),
        "QQQ": dict(symbol_record),
        "market_gex_regime": regime,
        "regime_source": "vix_fallback",
        "vix_value": vix_value,
        "regime_note": note,
    }


def _empty_symbol_record() -> Dict[str, Any]:
    """Return a placeholder record when a symbol fetch fails."""
    return {
        "net_gex": 0.0,
        "gamma_flip": 0.0,
        "put_wall": 0.0,
        "call_wall": 0.0,
        "volatility_regime": "UNKNOWN",
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def write_regime(record: Dict[str, Any]) -> None:
    """Write the regime record atomically to the configured JSON path."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = OUTPUT_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(record, indent=2))
    temp_path.replace(OUTPUT_PATH)
    logger.info("Persisted GEX regime to %s", OUTPUT_PATH)


# ---------------------------------------------------------------------------
# Finalist helper (reserved calls)
# ---------------------------------------------------------------------------
def fetch_top_finalist_gex(
    finalists: List[str],
    api_key: str,
    max_symbols: int = RESERVED_FINALIST_SLOTS,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Fetch individual GEX for the top N finalist symbols.

    This function is intended to consume the remaining ~3 daily calls after the
    main SPY/QQQ run. It does NOT run automatically during the nightly main()
    execution; it should be invoked explicitly by downstream stages when a
    finalist needs symbol-level GEX.

    Args:
        finalists: Ordered list of finalist symbols (highest confluence first).
        api_key: The FlashAlpha API key.
        max_symbols: Maximum individual fetches to perform (default 3).

    Returns:
        Mapping of symbol -> GEX record (or None on failure).
    """
    results: Dict[str, Optional[Dict[str, Any]]] = {}
    for symbol in finalists[:max_symbols]:
        results[symbol] = fetch_symbol_gex(symbol, api_key)
    return results


# ---------------------------------------------------------------------------
# Main nightly run
# ---------------------------------------------------------------------------
def main() -> None:
    """Execute the nightly FlashAlpha GEX fetch for SPY and QQQ."""
    api_key = os.getenv("FLASHALPHA_API_KEY")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    record: Dict[str, Any] = {
        "date": today,
        "spy": _empty_symbol_record(),
        "qqq": _empty_symbol_record(),
        "market_gex_regime": "UNKNOWN",
        "regime_note": "",
    }

    if not api_key:
        logger.warning("FLASHALPHA_API_KEY not set; using VIX fallback regime.")
        record = _vix_fallback_record(today, "API key missing (FLASHALPHA_API_KEY); using VIX fallback")
        write_regime(record)
        return

    spy_data = fetch_symbol_gex("SPY", api_key)
    qqq_data = fetch_symbol_gex("QQQ", api_key)

    if spy_data:
        record["spy"] = spy_data
    if qqq_data:
        record["qqq"] = qqq_data

    if not spy_data or not qqq_data or _gex_values_are_empty(record["spy"], record["qqq"]):
        notes: List[str] = []
        if not spy_data:
            notes.append("SPY fetch failed")
        if not qqq_data:
            notes.append("QQQ fetch failed")
        if spy_data and qqq_data and _gex_values_are_empty(record["spy"], record["qqq"]):
            notes.append("FlashAlpha returned empty GEX values")
        notes.append("using VIX fallback")
        record = _vix_fallback_record(today, "; ".join(notes))
        write_regime(record)
        logger.info(
            "Nightly GEX complete via VIX fallback. Market regime: %s | VIX: %s",
            record["market_gex_regime"],
            record["vix_value"],
        )
        return

    record["market_gex_regime"] = compute_market_gex_regime(
        record["spy"], record["qqq"]
    )
    record["regime_source"] = "flashalpha"

    notes: List[str] = []
    if not spy_data:
        notes.append("SPY fetch failed")
    if not qqq_data:
        notes.append("QQQ fetch failed")
    if not notes:
        notes.append(
            f"SPY={record['spy']['volatility_regime']}, QQQ={record['qqq']['volatility_regime']}"
        )
    record["regime_note"] = "; ".join(notes)

    write_regime(record)
    logger.info(
        "Nightly GEX complete. Market regime: %s | Note: %s",
        record["market_gex_regime"],
        record["regime_note"],
    )


if __name__ == "__main__":
    main()
