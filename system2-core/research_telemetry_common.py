#!/usr/bin/env python3
"""Shared helpers for additive, non-trading System2 research telemetry."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RESEARCH_ROOT = ROOT / "data" / "research_telemetry"
NY = ZoneInfo("America/New_York")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _observed_holiday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    day = date(year, month, 1)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    day = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    while day.weekday() != weekday:
        day -= timedelta(days=1)
    return day


def _easter(year: int) -> date:
    a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30
    i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31; day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def market_holidays(year: int) -> set[date]:
    holidays = {
        _observed_holiday(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_holiday(date(year, 6, 19)),
        _observed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_holiday(date(year, 12, 25)),
    }
    return holidays


def is_market_session(day: date) -> bool:
    return day.weekday() < 5 and day not in market_holidays(day.year)


def next_market_session(after: datetime | None = None) -> dict[str, Any]:
    moment = (after or utc_now()).astimezone(NY)
    day = moment.date()
    open_time = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY)
    if moment >= open_time or not is_market_session(day):
        day += timedelta(days=1)
        while not is_market_session(day):
            day += timedelta(days=1)
        open_time = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY)
    prior = day - timedelta(days=1)
    weekend = prior.weekday() >= 5
    holiday = not weekend and not is_market_session(prior)
    return {
        "trading_session": day.isoformat(),
        "next_session_open": open_time.isoformat(),
        "hours_to_open": round((open_time.astimezone(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds() / 3600, 4),
        "weekend_carry": weekend,
        "holiday_carry": holiday,
    }


def run_id() -> str:
    return os.environ.get("SYSTEM2_RUN_ID") or utc_now().strftime("%Y%m%dT%H%M%SZ") + "-research"


def run_directory(session: str | None = None, identifier: str | None = None) -> Path:
    session = session or next_market_session()["trading_session"]
    path = RESEARCH_ROOT / session / (identifier or run_id())
    path.mkdir(parents=True, exist_ok=True)
    return path


def content_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def write_immutable(path: Path, payload: dict[str, Any]) -> Path:
    body = dict(payload)
    body["artifact_hash"] = content_hash(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(body, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return default

