#!/usr/bin/env python3
"""Best-effort FMP bandwidth ledger.

This module observes responses that existing FMP callers already received. It
must never affect request, retry, parsing, caching, or pipeline behavior.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except Exception:  # pragma: no cover - Linux VPS has fcntl.
    fcntl = None


ROOT = Path(__file__).resolve().parent
LOG_ROOT = ROOT / "data" / "fmp_bandwidth_log"


def endpoint_type(endpoint_or_url: str) -> str:
    text = str(endpoint_or_url or "")
    text = text.split("financialmodelingprep.com/")[-1]
    text = text.split("apikey=")[0].split("?")[0].strip("/")
    text = re.sub(r"/[A-Z0-9.^-]{1,12}$", "/{symbol}", text)

    if text.startswith("stable/historical-price-eod"):
        return "historical-price-eod"
    if text.startswith("stable/historical-chart"):
        return "historical-chart"
    if text.startswith("stable/profile"):
        return "profile"
    if text.startswith("stable/batch-quote"):
        return "batch-quote"
    if text.startswith("stable/quote"):
        return "quote"
    if "news" in text or "press-releases" in text:
        return "news"
    if "earning" in text:
        return "earnings"
    if "insider" in text:
        return "insider"
    if "short" in text or "shares-float" in text:
        return "short-float"
    return text or "unknown"


def record(endpoint: str, byte_count: int | None, *, status: int | None = None, source: str | None = None) -> None:
    """Record one observed FMP response. Logging failures are swallowed."""
    try:
        now = datetime.now(timezone.utc)
        day = now.date().isoformat()
        LOG_ROOT.mkdir(parents=True, exist_ok=True)
        path = LOG_ROOT / f"{day}.json"
        lock_path = LOG_ROOT / f"{day}.lock"

        def update() -> None:
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            else:
                data = {}

            endpoint_name = endpoint_type(endpoint)
            bytes_seen = int(byte_count or 0)
            data.setdefault("date", day)
            data["updated_at"] = now.isoformat()
            data["total_bytes"] = int(data.get("total_bytes") or 0) + bytes_seen
            data["total_calls"] = int(data.get("total_calls") or 0) + 1

            by_endpoint = data.setdefault("by_endpoint_type", {})
            row = by_endpoint.setdefault(endpoint_name, {"bytes": 0, "calls": 0})
            row["bytes"] = int(row.get("bytes") or 0) + bytes_seen
            row["calls"] = int(row.get("calls") or 0) + 1
            if status is not None:
                statuses = row.setdefault("statuses", {})
                statuses[str(status)] = int(statuses.get(str(status), 0)) + 1

            if source:
                by_source = data.setdefault("by_source", {})
                source_row = by_source.setdefault(source, {"bytes": 0, "calls": 0})
                source_row["bytes"] = int(source_row.get("bytes") or 0) + bytes_seen
                source_row["calls"] = int(source_row.get("calls") or 0) + 1

            tmp_path = path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp_path, path)

        if fcntl is None:
            update()
            return

        with lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            update()
            fcntl.flock(lock, fcntl.LOCK_UN)
    except Exception:
        pass
