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
    # DB stores naive datetime in configured timezone.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def format_in_configured_timezone(dt: datetime, fmt: str) -> str:
    return to_configured_timezone(dt).strftime(fmt)


def now_in_configured_timezone_naive() -> datetime:
    return datetime.now(configured_timezone()).replace(tzinfo=None)
