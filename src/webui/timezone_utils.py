from __future__ import annotations

import os
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def configured_timezone_name() -> str:
    return (os.getenv("APP_TIMEZONE", "UTC") or "UTC").strip()


def configured_timezone() -> tzinfo:
    raw = configured_timezone_name()
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        return timezone.utc


def to_configured_timezone(dt: datetime) -> datetime:
    tz = configured_timezone()
    # DB stores naive datetime. Treat it as UTC for deterministic conversion.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def format_in_configured_timezone(dt: datetime, fmt: str) -> str:
    return to_configured_timezone(dt).strftime(fmt)
