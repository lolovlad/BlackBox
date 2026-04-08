from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

from werkzeug.datastructures import ImmutableMultiDict, MultiDict

from src.webui.data_labels import (
    analog_labels_for,
    discrete_labels_for,
    filter_valid_analog,
    filter_valid_discrete,
)
from src.webui.modbus_service import (
    analog_discrete_for_csv,
    decode_to_processed,
)
from src.webui.repositories.data_repository import DataRepository
from src.webui.timezone_utils import format_in_configured_timezone

# Таблица на экране: фиксированный размер страницы (экспорт без лимита строк)
TABLE_PAGE_SIZE = 1000
DATETIME_UI_FORMAT = "%d.%m.%Y %H:%M:%S"


@dataclass(frozen=True)
class DataFilter:
    active_tab: str  # analog | discrete | alarms
    date_from: datetime | None
    date_to: datetime | None
    sort_desc: bool
    analog_columns: list[str]
    discrete_columns: list[str]
    page: int  # 1-based, для таблицы


def _parse_dt(raw: str | None) -> datetime | None:
    """Дата/время: русский дд.мм.гггг [чч:мм[:сс]] или ISO / Z (для совместимости)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace(",", ".")
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # Дд.мм.ггггчч:мм без пробела (опечатка)
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})(\d{2}):(\d{2})$", s)
    if m:
        d, mo, y, hh, mm = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), int(hh), int(mm), 0)
        except ValueError:
            pass
    try:
        iso = s
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _normalize_date_range(date_from: datetime | None, date_to: datetime | None) -> tuple[datetime | None, datetime | None]:
    """Верхняя граница «по дате» при времени 00:00 включает весь календарный день (ввод только даты или 00:00)."""
    df, dt = date_from, date_to
    if dt is not None and dt.time() == time(0, 0, 0):
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    if df is not None and dt is not None and df > dt:
        df, dt = dt, df
    return df, dt


def _parse_page(raw: str | None) -> int:
    if raw is None or str(raw).strip() == "":
        return 1
    try:
        p = int(str(raw).strip())
    except ValueError:
        return 1
    return max(1, p)


def parse_data_filter(source: ImmutableMultiDict | MultiDict) -> DataFilter:
    tab = (source.get("active_tab") or "analog").strip().lower()
    if tab not in ("analog", "discrete", "alarms"):
        tab = "analog"
    date_from, date_to = _normalize_date_range(
        _parse_dt(source.get("date_from")),
        _parse_dt(source.get("date_to")),
    )
    sort_desc = (source.get("sort") or "desc").lower() != "asc"
    analog_req = source.getlist("analog_col")
    discrete_req = source.getlist("discrete_col")
    analog_columns = filter_valid_analog(analog_req if analog_req else None)
    discrete_columns = filter_valid_discrete(discrete_req if discrete_req else None)
    page = _parse_page(source.get("page"))
    return DataFilter(
        active_tab=tab,
        date_from=date_from,
        date_to=date_to,
        sort_desc=sort_desc,
        analog_columns=analog_columns,
        discrete_columns=discrete_columns,
        page=page,
    )


class DataService:
    def __init__(self, repository: DataRepository, alarms_enabled: bool) -> None:
        self._repo = repository
        self._alarms_enabled = alarms_enabled

    def collect_tab(self, flt: DataFilter) -> dict[str, Any]:
        tab = flt.active_tab
        if tab == "alarms" and not self._alarms_enabled:
            return {
                "tab": "alarms",
                "alarms_disabled": True,
                "columns": [],
                "rows": [],
                "page": 1,
                "total_pages": 1,
                "total_rows": 0,
                "page_size": TABLE_PAGE_SIZE,
            }

        if tab == "analog":
            total = self._repo.count_analogs(created_from=flt.date_from, created_to=flt.date_to)
            total_pages = max(1, (total + TABLE_PAGE_SIZE - 1) // TABLE_PAGE_SIZE) if total else 1
            page_eff = min(max(1, flt.page), total_pages)
            offset = (page_eff - 1) * TABLE_PAGE_SIZE
            rows_db = self._repo.list_analogs(
                created_from=flt.date_from,
                created_to=flt.date_to,
                sort_desc=flt.sort_desc,
                offset=offset,
                limit=TABLE_PAGE_SIZE,
            )
            keys = flt.analog_columns
            col_labels = analog_labels_for(keys)
            out_rows = []
            for item in rows_db:
                processed = decode_to_processed(item.date)
                analog, _ = analog_discrete_for_csv(processed)
                out_rows.append(
                    {
                        "time": format_in_configured_timezone(item.created_at, DATETIME_UI_FORMAT),
                        "cells": [analog.get(k, "") for k in keys],
                    }
                )
            return {
                "tab": "analog",
                "columns": col_labels,
                "rows": out_rows,
                "alarms_disabled": False,
                "page": page_eff,
                "total_pages": total_pages,
                "total_rows": total,
                "page_size": TABLE_PAGE_SIZE,
            }

        if tab == "discrete":
            total = self._repo.count_discretes(created_from=flt.date_from, created_to=flt.date_to)
            total_pages = max(1, (total + TABLE_PAGE_SIZE - 1) // TABLE_PAGE_SIZE) if total else 1
            page_eff = min(max(1, flt.page), total_pages)
            offset = (page_eff - 1) * TABLE_PAGE_SIZE
            rows_db = self._repo.list_discretes(
                created_from=flt.date_from,
                created_to=flt.date_to,
                sort_desc=flt.sort_desc,
                offset=offset,
                limit=TABLE_PAGE_SIZE,
            )
            keys = flt.discrete_columns
            col_labels = discrete_labels_for(keys)
            out_rows = []
            for item in rows_db:
                processed = decode_to_processed(item.date)
                _, discrete = analog_discrete_for_csv(processed)
                out_rows.append(
                    {
                        "time": format_in_configured_timezone(item.created_at, DATETIME_UI_FORMAT),
                        "cells": [1 if bool(discrete.get(k, False)) else 0 for k in keys],
                    }
                )
            return {
                "tab": "discrete",
                "columns": col_labels,
                "rows": out_rows,
                "alarms_disabled": False,
                "page": page_eff,
                "total_pages": total_pages,
                "total_rows": total,
                "page_size": TABLE_PAGE_SIZE,
            }

        total = self._repo.count_alarms(created_from=flt.date_from, created_to=flt.date_to)
        total_pages = max(1, (total + TABLE_PAGE_SIZE - 1) // TABLE_PAGE_SIZE) if total else 1
        page_eff = min(max(1, flt.page), total_pages)
        offset = (page_eff - 1) * TABLE_PAGE_SIZE
        rows_db = self._repo.list_alarms(
            created_from=flt.date_from,
            created_to=flt.date_to,
            sort_desc=flt.sort_desc,
            offset=offset,
            limit=TABLE_PAGE_SIZE,
        )
        out_rows = [
            {
                "time": format_in_configured_timezone(item.created_at, DATETIME_UI_FORMAT),
                "name": item.name,
                "state": getattr(item, "state", "active"),
            }
            for item in rows_db
        ]
        return {
            "tab": "alarms",
            "columns": [],
            "rows": out_rows,
            "alarms_disabled": False,
            "page": page_eff,
            "total_pages": total_pages,
            "total_rows": total,
            "page_size": TABLE_PAGE_SIZE,
        }

    def build_export(self, flt: DataFilter, static_csv_dir: Path) -> Path:
        static_csv_dir.mkdir(parents=True, exist_ok=True)
        path = static_csv_dir / f"export_{datetime.now():%Y%m%d_%H%M%S}.csv"

        if flt.active_tab == "alarms":
            if not self._alarms_enabled:
                raise ValueError("alarms_disabled")
            rows_db = self._repo.list_alarms(
                created_from=flt.date_from,
                created_to=flt.date_to,
                sort_desc=flt.sort_desc,
                offset=0,
                limit=None,
            )
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["№", "Дата", "Время", "Название", "Состояние"])
                for i, item in enumerate(rows_db, start=1):
                    dt = item.created_at
                    dt_display = format_in_configured_timezone(dt, "%Y-%m-%d %H:%M:%S")
                    w.writerow([i, dt_display[:10], dt_display[11:], item.name, getattr(item, "state", "active")])
            return path

        if flt.active_tab == "analog":
            rows_db = self._repo.list_analogs(
                created_from=flt.date_from,
                created_to=flt.date_to,
                sort_desc=flt.sort_desc,
                offset=0,
                limit=None,
            )
            keys = flt.analog_columns
            headers = ["№", "Дата", "Время", *keys]
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(headers)
                for i, row in enumerate(rows_db, start=1):
                    processed = decode_to_processed(row.date)
                    analog, _ = analog_discrete_for_csv(processed)
                    dt = row.created_at
                    dt_display = format_in_configured_timezone(dt, "%Y-%m-%d %H:%M:%S")
                    w.writerow(
                        [i, dt_display[:10], dt_display[11:], *[analog.get(k, "") for k in keys]]
                    )
            return path

        if flt.active_tab == "discrete":
            rows_db = self._repo.list_discretes(
                created_from=flt.date_from,
                created_to=flt.date_to,
                sort_desc=flt.sort_desc,
                offset=0,
                limit=None,
            )
            keys = flt.discrete_columns
            headers = ["№", "Дата", "Время", *keys]
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(headers)
                for i, row in enumerate(rows_db, start=1):
                    processed = decode_to_processed(row.date)
                    _, discrete = analog_discrete_for_csv(processed)
                    dt = row.created_at
                    dt_display = format_in_configured_timezone(dt, "%Y-%m-%d %H:%M:%S")
                    w.writerow(
                        [
                            i,
                            dt_display[:10],
                            dt_display[11:],
                            *[1 if bool(discrete.get(k, False)) else 0 for k in keys],
                        ]
                    )
            return path

        raise ValueError("unknown_tab")
