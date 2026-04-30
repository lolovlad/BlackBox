from __future__ import annotations

from datetime import datetime

from src.webui.modbus_service import parse_fields, try_parse_controller_datetime


def test_controller_datetime_iso_parsing_from_fields() -> None:
    cfg = {
        "requests": [{"name": "ctl_time", "fc": 3, "address": 19000, "count": 7}],
        "fields": [
            {"name": "ctl_year", "source": "ctl_time", "address": 0, "type": "uint16", "system": True},
            {"name": "ctl_month", "source": "ctl_time", "address": 1, "type": "uint16", "system": True},
            {"name": "ctl_day", "source": "ctl_time", "address": 2, "type": "uint16", "system": True},
            # 19003 пропускаем — регистр не используется для даты/времени.
            {"name": "ctl_hour", "source": "ctl_time", "address": 4, "type": "uint16", "system": True},
            {"name": "ctl_minute", "source": "ctl_time", "address": 5, "type": "uint16", "system": True},
            {"name": "ctl_second", "source": "ctl_time", "address": 6, "type": "uint16", "system": True},
            {
                "name": "controller_datetime_iso",
                "type": "expr",
                "system": True,
                    "round": False,
                "expr": "'{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}'.format(ctl_year, ctl_month, ctl_day, ctl_hour, ctl_minute, ctl_second)",
            },
        ],
    }
    processed = parse_fields(cfg, {"ctl_time": [2026, 4, 29, 0, 12, 34, 56]})
    dt = try_parse_controller_datetime(processed)
    assert dt == datetime(2026, 4, 29, 12, 34, 56)


def test_controller_datetime_invalid_returns_none() -> None:
    cfg = {
        "requests": [{"name": "ctl_time", "fc": 3, "address": 19000, "count": 7}],
        "fields": [
            {"name": "ctl_year", "source": "ctl_time", "address": 0, "type": "uint16", "system": True},
            {"name": "ctl_month", "source": "ctl_time", "address": 1, "type": "uint16", "system": True},
            {"name": "ctl_day", "source": "ctl_time", "address": 2, "type": "uint16", "system": True},
            {"name": "ctl_hour", "source": "ctl_time", "address": 4, "type": "uint16", "system": True},
            {"name": "ctl_minute", "source": "ctl_time", "address": 5, "type": "uint16", "system": True},
            {"name": "ctl_second", "source": "ctl_time", "address": 6, "type": "uint16", "system": True},
            {
                "name": "controller_datetime_iso",
                "type": "expr",
                "system": True,
                "expr": "'{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}'.format(ctl_year, ctl_month, ctl_day, ctl_hour, ctl_minute, ctl_second)",
            },
        ],
    }
    processed = parse_fields(cfg, {"ctl_time": [0, 0, 0, 0, 0, 0, 0]})
    dt = try_parse_controller_datetime(processed)
    assert dt is None

