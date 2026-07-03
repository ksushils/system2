#!/usr/bin/env python3
"""Small shared cache for FMP data that should not change intraday."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "data" / "fmp_cache"


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _safe_name(value: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._=-]+", "_", value).strip("_")[:120]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{readable}-{digest}.json"


def cache_key(endpoint: str, params: dict[str, Any] | None = None) -> str:
    params = params or {}
    normalized = json.dumps({"endpoint": endpoint, "params": params}, sort_keys=True, separators=(",", ":"))
    return _safe_name(normalized)


def get_daily(endpoint: str, params: dict[str, Any] | None = None) -> Any | None:
    path = CACHE_DIR / _today() / cache_key(endpoint, params)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("date") != _today():
        return None
    return payload.get("data")


def set_daily(endpoint: str, data: Any, params: dict[str, Any] | None = None) -> None:
    day_dir = CACHE_DIR / _today()
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / cache_key(endpoint, params)
    payload = {
        "date": _today(),
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "params": params or {},
        "data": data,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
