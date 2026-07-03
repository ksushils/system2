#!/usr/bin/env python3
"""Danelfin API sync script for System 2.

Fetches the top 500 US stock rankings from the Danelfin REST API in batches,
cross-references them against the System 2 stage 1 survivor universe, and
writes a structured JSON file with AI scores and subscores.

Designed to be fail-open: API or environment failures result in an empty
scores object and metadata.status == "ERROR", allowing downstream stages to
continue without manual intervention.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_ENDPOINT: str = "https://apirest.danelfin.com/ranking"
TIMEOUT_SECONDS: int = 10
MAX_RETRIES: int = 3
BATCH_SIZE: int = 100
BATCH_OFFSETS: List[int] = [0, 100, 200, 300, 400]
UNIVERSE_PATH: Path = Path("/root/system2-core/stage1_survivors.json")
OUTPUT_PATH: Path = Path("/root/system2-core/data/danelfin_scores.json")
REQUESTED_FIELDS: str = (
    "aiscore,technical,fundamental,sentiment,low_risk,"
    "buy_track_record,sell_track_record,sector,industry,"
    "score_trend,score_upgraded_5d,score_change_5d"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(message: str) -> None:
    """Print a timestamped log line to stdout."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    print(f"[{ts}] {message}", flush=True)


def _load_survivors(path: Path) -> Set[str]:
    """Load the System 2 survivor universe and return a set of upper-cased tickers.

    The survivor file is expected to contain either a list of ticker strings or
    a list of dictionaries with a ``ticker``/``symbol`` key.  Any load failure
    returns an empty set and logs the error so the script can continue in a
    degraded state.
    """
    if not path.exists():
        _log(f"WARNING: Survivor universe not found at {path}")
        return set()

    try:
        with path.open("r", encoding="utf-8") as fh:
            raw: Any = json.load(fh)
    except Exception as exc:  # pragma: no cover - broad fail-open
        _log(f"ERROR: Unable to parse survivor universe: {exc}")
        return set()

    survivors: Set[str] = set()
    if not isinstance(raw, list):
        _log("WARNING: Survivor universe is not a JSON list; treating as empty.")
        return survivors

    for item in raw:
        if isinstance(item, str):
            survivors.add(item.upper().strip())
        elif isinstance(item, dict):
            ticker = item.get("ticker") or item.get("symbol")
            if isinstance(ticker, str) and ticker.strip():
                survivors.add(ticker.upper().strip())
        else:
            _log(f"WARNING: Unrecognised survivor entry type: {type(item)}")

    _log(f"Loaded {len(survivors)} survivors from {path}")
    return survivors


def _extract_ranking_data(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Normalise the Danelfin ranking response into a flat ticker->record map.

    The API returns rankings nested underneath a date key (e.g.
    ``{"2024-01-02": {"AAPL": {...}}}``).  Some responses may already be
    flat.  This helper handles both shapes and returns the inner ticker records.
    """
    if not isinstance(payload, dict):
        return {}

    # If the payload is a single-key dict whose only value is itself a dict of
    # ticker records, unwrap it.
    if len(payload) == 1:
        only_value = next(iter(payload.values()))
        if isinstance(only_value, dict) and any(
            isinstance(v, dict) for v in only_value.values()
        ):
            return only_value

    # Otherwise assume the payload is already ticker -> record.
    return payload


def _map_record(api_record: Dict[str, Any]) -> Dict[str, Any]:
    """Map a single Danelfin API record to the System 2 schema.

    Fields that are absent from the API response are preserved as ``None`` so
    that downstream consumers have a stable schema to rely on.
    """
    return {
        "ai_score": api_record.get("aiscore") or api_record.get("ai_score"),
        "technical": api_record.get("technical"),
        "fundamental": api_record.get("fundamental"),
        "sentiment": api_record.get("sentiment"),
        "low_risk": api_record.get("low_risk"),
        "buy_track_record": api_record.get("buy_track_record"),
        "sell_track_record": api_record.get("sell_track_record"),
        "sector": api_record.get("sector"),
        "industry": api_record.get("industry"),
        "score_trend": api_record.get("score_trend"),
        "score_upgraded_5d": api_record.get("score_upgraded_5d"),
        "score_change_5d": api_record.get("score_change_5d"),
    }


def fetch_batch(
    offset: int,
    api_key: str,
    run_date: str,
    session: requests.Session,
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Fetch one 100-ticker batch from the Danelfin ranking endpoint.

    Implements exponential backoff across ``MAX_RETRIES`` attempts.  Returns
    a flat ticker->record dictionary on success, or ``None`` if the batch
    could not be retrieved.
    """
    params = {
        "date": run_date,
        "asset": "stock",
        "offset": offset,
        "limit": BATCH_SIZE,
        "fields": REQUESTED_FIELDS,
    }
    headers = {"x-api-key": api_key, "Accept": "application/json"}

    for attempt in range(1, MAX_RETRIES + 1):
        _log(f"Fetching batch offset={offset} attempt={attempt}/{MAX_RETRIES}")
        try:
            response = session.get(
                API_ENDPOINT, params=params, headers=headers, timeout=TIMEOUT_SECONDS
            )
            response.raise_for_status()
            payload = response.json()
            return _extract_ranking_data(payload)
        except requests.exceptions.Timeout as exc:
            _log(f"TIMEOUT batch offset={offset} attempt={attempt}: {exc}")
        except requests.exceptions.HTTPError as exc:
            _log(f"HTTP batch offset={offset} attempt={attempt}: {exc}")
        except requests.exceptions.RequestException as exc:
            _log(f"NETWORK batch offset={offset} attempt={attempt}: {exc}")
        except Exception as exc:  # pragma: no cover - broad fail-open
            _log(f"UNEXPECTED batch offset={offset} attempt={attempt}: {exc}")

        if attempt < MAX_RETRIES:
            backoff = 2 ** (attempt - 1)
            _log(f"Retrying batch offset={offset} in {backoff}s")
            time.sleep(backoff)

    _log(f"ERROR: Batch offset={offset} failed after {MAX_RETRIES} attempts")
    return None


def write_output(
    scores: Dict[str, Dict[str, Any]],
    api_status: str,
    errors: List[str],
    output_path: Path,
) -> None:
    """Serialise the final scores payload to disk atomically."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "run_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metadata": {
            "count": len(scores),
            "api_status": api_status,
            "errors": errors,
        },
        "scores": scores,
    }
    tmp_path = output_path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    tmp_path.replace(output_path)
    _log(f"Wrote {len(scores)} records to {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Entry point for the Danelfin sync job.

    Returns 0 on completion (including graceful fail-open paths) and 1 only
    for unrecoverable local errors that prevent the output from being written.
    """
    _log("Danelfin sync started")

    api_key = os.environ.get("DANELFIN_API_KEY")
    if not api_key:
        _log("ERROR: DANELFIN_API_KEY environment variable is missing")
        write_output({}, "ERROR", ["DANELFIN_API_KEY not set"], OUTPUT_PATH)
        return 0

    survivors = _load_survivors(UNIVERSE_PATH)
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _log(f"Using run_date={run_date} | survivors={len(survivors)}")

    session = requests.Session()
    all_errors: List[str] = []
    raw_scores: Dict[str, Dict[str, Any]] = {}

    for offset in BATCH_OFFSETS:
        batch = fetch_batch(offset, api_key, run_date, session)
        if batch is None:
            all_errors.append(f"Failed to fetch batch offset={offset}")
            continue
        _log(f"Batch offset={offset} returned {len(batch)} records")
        for ticker, record in batch.items():
            if not isinstance(ticker, str) or not ticker.strip():
                continue
            upper_ticker = ticker.upper().strip()
            # Only keep the first occurrence across batches to avoid duplication
            # if the API ignores offset/limit.
            if upper_ticker not in raw_scores:
                raw_scores[upper_ticker] = record

    session.close()

    # Cross-reference with universe survivors.
    filtered_scores: Dict[str, Dict[str, Any]] = {}
    if survivors:
        for ticker in sorted(raw_scores):
            if ticker in survivors:
                filtered_scores[ticker] = _map_record(raw_scores[ticker])
        _log(f"Cross-reference complete: {len(filtered_scores)} survivors matched")
    else:
        # If the universe could not be loaded we still emit whatever we fetched
        # so the failure remains open, but log a warning.
        _log("WARNING: No survivor universe available; returning empty scores")

    api_status = "OK" if not all_errors and raw_scores else "ERROR" if all_errors else "OK"
    if not raw_scores and not all_errors:
        api_status = "ERROR"
        all_errors.append("No scores returned by API")

    write_output(filtered_scores, api_status, all_errors, OUTPUT_PATH)
    _log("Danelfin sync finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
