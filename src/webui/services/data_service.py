from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from werkzeug.datastructures import ImmutableMultiDict, MultiDict

from src.webui.data_labels import (
    RU_ANALOG_LABELS,
    RU_DISCRETE_LABELS,
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

# Пустой лимит в форме = «максимум записей по умолчанию» (производительность)
DEFAULT_ROW_LIMIT = 10000
MAX_ROW_LIMIT = 50000


@dataclass(frozen=True)
class DataFilter:
    active_tab: str  # analog | discrete | alarms
    date_from: datetime | None
    date_to: datetime | None
    sort_desc: bool
    analog_columns: list[str]
    discrete_columns: list[str]
    limit: int


def _parse_dt(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _parse_limit(raw: str | None) -> int:
    if raw is None or str(raw).strip() == "":
        return DEFAULT_ROW_LIMIT
    try:
        n = int(str(raw).strip())
    except ValueError:
        return DEFAULT_ROW_LIMIT
    if n < 1:
        return DEFAULT_ROW_LIMIT
    return min(n, MAX_ROW_LIMIT)


def parse_data_filter(source: ImmutableMultiDict | MultiDict) -> DataFilter:
    tab = (source.get("active_tab") or "analog").strip().lower()
    if tab not in ("analog", "discrete", "alarms"):
        tab = "analog"
    date_from = _parse_dt(source.get("date_from"))
    date_to = _parse_dt(source.get("date_to"))
    sort_desc = (source.get("sort") or "desc").lower() != "asc"
    analog_req = source.getlist("analog_col")
    discrete_req = source.getlist("discrete_col")
    analog_columns = filter_valid_analog(analog_req if analog_req else None)
    discrete_columns = filter_valid_discrete(discrete_req if discrete_req else None)
    limit = _parse_limit(source.get("limit"))
    return DataFilter(
        active_tab=tab,
        date_from=date_from,
        date_to=date_to,
        sort_desc=sort_desc,
        analog_columns=analog_columns,
        discrete_columns=discrete_columns,
        limit=limit,
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
            }

        if tab == "analog":
            rows_db = self._repo.list_analogs(
                created_from=flt.date_from,
                created_to=flt.date_to,
                sort_desc=flt.sort_desc,
                limit=flt.limit,
            )
            keys = flt.analog_columns
            col_labels = analog_labels_for(keys)
            out_rows = []
            for item in rows_db:
                processed = decode_to_processed(item.date)
                analog, _ = analog_discrete_for_csv(processed)
                out_rows.append(
                    {
                        "time": item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "cells": [analog.get(k, "") for k in keys],
                    }
                )
            return {"tab": "analog", "columns": col_labels, "rows": out_rows, "alarms_disabled": False}

        if tab == "discrete":
            rows_db = self._repo.list_discretes(
                created_from=flt.date_from,
                created_to=flt.date_to,
                sort_desc=flt.sort_desc,
                limit=flt.limit,
            )
            keys = flt.discrete_columns
            col_labels = discrete_labels_for(keys)
            out_rows = []
            for item in rows_db:
                processed = decode_to_processed(item.date)
                _, discrete = analog_discrete_for_csv(processed)
                out_rows.append(
                    {
                        "time": item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "cells": [1 if bool(discrete.get(k, False)) else 0 for k in keys],
                    }
                )
            return {"tab": "discrete", "columns": col_labels, "rows": out_rows, "alarms_disabled": False}

        rows_db = self._repo.list_alarms(
            created_from=flt.date_from,
            created_to=flt.date_to,
            sort_desc=flt.sort_desc,
            limit=flt.limit,
        )
        out_rows = [
            {
                "time": item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "name": item.name,
            }
            for item in rows_db
        ]
        return {"tab": "alarms", "columns": [], "rows": out_rows, "alarms_disabled": False}

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
                limit=flt.limit,
            )
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["№", "Дата", "Время", "Название"])
                for i, item in enumerate(rows_db, start=1):
                    dt = item.created_at
                    w.writerow([i, dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S"), item.name])
            return path

        if flt.active_tab == "analog":
            rows_db = self._repo.list_analogs(
                created_from=flt.date_from,
                created_to=flt.date_to,
                sort_desc=flt.sort_desc,
                limit=flt.limit,
            )
            keys = flt.analog_columns
            headers = ["№", "Дата", "Время", *[RU_ANALOG_LABELS.get(k, k) for k in keys]]
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(headers)
                for i, row in enumerate(rows_db, start=1):
                    processed = decode_to_processed(row.date)
                    analog, _ = analog_discrete_for_csv(processed)
                    dt = row.created_at
                    w.writerow(
                        [i, dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S"), *[analog.get(k, "") for k in keys]]
                    )
            return path

        if flt.active_tab == "discrete":
            rows_db = self._repo.list_discretes(
                created_from=flt.date_from,
                created_to=flt.date_to,
                sort_desc=flt.sort_desc,
                limit=flt.limit,
            )
            keys = flt.discrete_columns
            headers = ["№", "Дата", "Время", *[RU_DISCRETE_LABELS.get(k, k) for k in keys]]
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(headers)
                for i, row in enumerate(rows_db, start=1):
                    processed = decode_to_processed(row.date)
                    _, discrete = analog_discrete_for_csv(processed)
                    dt = row.created_at
                    w.writerow(
                        [
                            i,
                            dt.strftime("%Y-%m-%d"),
                            dt.strftime("%H:%M:%S"),
                            *[1 if bool(discrete.get(k, False)) else 0 for k in keys],
                        ]
                    )
            return path

        raise ValueError("unknown_tab")
